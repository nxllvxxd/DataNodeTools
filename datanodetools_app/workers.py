import os
import threading
import time

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from .logging_utils import write_debug_log

# datanodes.to has no S3/multipart concept and no folder-path-on-upload —
# uploads are a single multipart POST to a per-request upload server, and
# folders are referenced by numeric fld_id rather than path strings. This
# file talks to https://datanodes.to/api/* per the published API reference.
DATANODES_BASE_URL = "https://datanodes.to"


# ── Progress Tracker ─────────────────────────────────────────────────────────
class ProgressTracker:
    """Thread-safe byte counter for a single in-flight upload.

    feed(n) is called as bytes leave the socket. Accumulates totals and
    fires progress/speed callbacks at most once every EMIT_INTERVAL seconds
    so the UI isn't flooded.
    """
    EMIT_INTERVAL = 0.25   # seconds between UI updates

    def __init__(self, total_bytes, on_progress, on_speed, on_bytes_progress=None):
        self._total     = total_bytes
        self._sent      = 0             # bytes confirmed sent
        self._lock      = threading.Lock()
        self._start     = time.monotonic()
        self._last_emit = 0.0
        self._on_prog   = on_progress   # callable(int pct)
        self._on_speed  = on_speed      # callable(float bps)
        self._on_bytes  = on_bytes_progress  # callable(int done, int total) or None

    def feed(self, n_bytes):
        """Called as bytes leave the socket."""
        with self._lock:
            self._sent = min(self._sent + n_bytes, self._total)
            now     = time.monotonic()
            elapsed = max(now - self._start, 0.001)
            if now - self._last_emit >= self.EMIT_INTERVAL:
                self._last_emit = now
                pct = min(self._sent / self._total * 100, 99.999) if self._total else 0
                bps = self._sent / elapsed
                self._on_prog(pct)
                self._on_speed(bps)
                if self._on_bytes:
                    self._on_bytes(self._sent, self._total)

    def finish(self):
        """Call once the upload completes to snap to 100%."""
        with self._lock:
            elapsed = max(time.monotonic() - self._start, 0.001)
            bps     = self._sent / elapsed
            total   = self._total
        self._on_prog(100)
        self._on_speed(bps)
        if self._on_bytes:
            self._on_bytes(total, total)

    def make_streaming_body(self, file_obj, total_size, read_size=65536):
        """Wrap a file object so reads are reported to the tracker as they
        happen, for use as a `requests` streaming upload body."""
        tracker = self

        class _TrackedReader:
            def __init__(self, fh, size, block_size):
                self.fh = fh
                self.len = size       # `requests`/urllib3 reads this for Content-Length
                self.block_size = block_size

            def read(self, size=-1):
                if size is None or size < 0:
                    size = self.block_size
                chunk = self.fh.read(size)
                if chunk:
                    tracker.feed(len(chunk))
                return chunk

            def __len__(self):
                return self.len

        return _TrackedReader(file_obj, total_size, read_size)


# ── Upload Worker ────────────────────────────────────────────────────────────
class UploadWorker(QThread):
    progress        = pyqtSignal(float)                  # 0.0-100.0
    speed           = pyqtSignal(float)                  # bytes/sec
    bytes_progress  = pyqtSignal('qint64', 'qint64')     # (bytes_done, bytes_total) — 64-bit to handle files > 2 GB
    status          = pyqtSignal(str)          # log message
    finished        = pyqtSignal(dict)         # result dict
    error           = pyqtSignal(str)

    # (connect_timeout, read_timeout) for plain API calls. The actual file
    # POST uses a longer read timeout since it covers the whole transfer.
    _TIMEOUT = (5, 60)
    _UPLOAD_TIMEOUT = (5, 3600)

    def __init__(self, api_key, base_url=None, file_pairs=None, create_share=False,
                 share_expiry=None, share_max_downloads=None,
                 chunk_size_mb=None, max_chunks=None, **_ignored_legacy_kwargs):
        """
        file_pairs: list of (local_abs_path, remote_dest_path) tuples.
        remote_dest_path is the full path the file should end up at on
        datanodes.to, e.g. '/Music/Album/CD1/track.flac' — the folder
        portion is created/resolved via /api/folder/* after upload.

        create_share: if True, the plain datanodes.to/<file_code> link for
        the last uploaded file is reported in the `finished` signal.
        (datanodes.to has no expiring/limited share links — just the file's
        own page — so there is no expiry/max-downloads equivalent.)

        base_url, share_expiry, share_max_downloads, chunk_size_mb, and
        max_chunks are accepted for backwards compatibility with existing
        call sites but are not used: datanodes.to has a fixed base URL,
        uploads are a single POST (no chunking), and share links have no
        expiry/download-limit concept.
        """
        super().__init__()
        self.api_key      = api_key
        self.file_pairs    = file_pairs or []          # [(local, dest), ...]
        self.create_share = create_share
        self._cancel      = False

        # path -> fld_id cache so repeated dest folders aren't re-resolved
        self._folder_id_cache = {"/": 0}

    def cancel(self):
        self._cancel = True

    def _params(self, **extra):
        """Build the query-string params every datanodes.to GET call needs."""
        params = {"key": self.api_key}
        params.update(extra)
        return params

    # ── helpers ──────────────────────────────────────────────────────────────

    def run(self):
        total_files    = len(self.file_pairs)
        last_file_code = None
        last_share_url = None

        file_sizes = []
        for local_path, _ in self.file_pairs:
            try:
                sz = os.path.getsize(local_path)
            except OSError:
                sz = 0
            file_sizes.append(sz)
        grand_total        = sum(file_sizes)
        bytes_done_offset  = 0

        for idx, (local_path, dest_path) in enumerate(self.file_pairs, 1):
            if self._cancel:
                return

            file_name = os.path.basename(local_path)
            prefix    = f"[{idx}/{total_files}] " if total_files > 1 else ""
            file_size = file_sizes[idx - 1]

            try:
                if file_size == 0:
                    self.status.emit(f"{prefix}{file_name}  ⊘ Skipped (empty file)")
                    continue

                self.status.emit(f"{prefix}{file_name}  ({self._fmt_size(file_size)})")
                self.status.emit(f"[DEBUG] Remote dest: {dest_path}")

                _offset = bytes_done_offset
                _grand  = grand_total

                def _on_bytes(done_this_file, _total_this_file,
                              offset=_offset, grand=_grand):
                    self.bytes_progress.emit(
                        min(offset + done_this_file, grand), grand
                    )

                self.status.emit("[DEBUG] Strategy: single POST upload (datanodes.to)")
                file_code = self._upload_single_file(
                    file_size, local_path, dest_path,
                    bytes_progress_cb=_on_bytes,
                )

                if self._cancel or file_code is None:
                    return

                # Move into the right folder, if dest_path implies one.
                dest_dir = "/".join(dest_path.rstrip("/").split("/")[:-1]) or "/"
                if dest_dir != "/":
                    try:
                        fld_id = self._ensure_folder_id(dest_dir)
                        self._set_folder(file_code, fld_id)
                    except Exception as e:
                        # Upload itself succeeded — don't fail the whole job
                        # just because the folder move failed.
                        self.status.emit(f"[DEBUG] Folder move failed for {file_name!r}: {e}")

                bytes_done_offset += file_size
                last_file_code = file_code

                if self.create_share and idx == total_files:
                    last_share_url = f"{DATANODES_BASE_URL}/{file_code}"
                    self.status.emit(f"Share: {last_share_url}")

            except Exception as e:
                self.error.emit(f"{prefix}{file_name}: {e}")
                return

        self.finished.emit({"file_code": last_file_code, "share_url": last_share_url})

    # ── single-file upload (datanodes.to has no multipart upload) ───────────
    def _upload_single_file(self, file_size, local_path, dest_path,
                             bytes_progress_cb=None):
        file_name = os.path.basename(local_path)

        # Step 1: get an upload server + session id.
        server_url, sess_id = self._get_upload_server()

        # Step 2: POST the file as multipart/form-data to that server.
        _bytes_cb = bytes_progress_cb or (
            lambda done, total: self.bytes_progress.emit(done, total)
        )
        tracker = ProgressTracker(
            file_size,
            on_progress=lambda pct: self.progress.emit(pct),
            on_speed=lambda bps: self.speed.emit(bps),
            on_bytes_progress=_bytes_cb,
        )

        try:
            guard_fh = open(local_path, "rb")
        except OSError as e:
            raise RuntimeError(f"Couldn't open {file_name!r} for upload: {e}") from e

        try:
            tracked_body = tracker.make_streaming_body(guard_fh, file_size)
            # `requests` needs an actual filename/content for multipart
            # encoding — wrap the tracked reader as the file's stream while
            # still presenting a normal (name, stream) tuple to `files=`.
            files = {
                "file_0": (file_name, tracked_body, "application/octet-stream"),
            }
            data = {
                "sess_id": sess_id,
                "utype": "prem",
            }

            self.status.emit(f"[DEBUG] Upload URL: {server_url}")
            self.status.emit(f"[DEBUG] sess_id: {sess_id}")

            resp = requests.post(
                server_url,
                data=data,
                files=files,
                timeout=self._UPLOAD_TIMEOUT,
            )
        finally:
            guard_fh.close()

        self.status.emit(f"[DEBUG] Upload response status: {resp.status_code}")
        self.status.emit(f"[DEBUG] Upload response body: {resp.text[:500]!r}")

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"Upload server returned {resp.status_code}: {resp.text[:200]!r}"
            ) from e

        try:
            result = resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"Upload server returned non-JSON response "
                f"(status {resp.status_code}): {resp.text[:200]!r}"
            ) from e

        # Response is a list: [{"file_code": "...", "file_status": "OK"}]
        if isinstance(result, list) and result:
            entry = result[0]
        elif isinstance(result, dict):
            entry = result
        else:
            raise RuntimeError(f"Unexpected upload response shape: {result!r}")

        file_status = entry.get("file_status")
        file_code   = entry.get("file_code")
        if file_status != "OK" or not file_code:
            raise RuntimeError(f"Upload failed for {file_name!r}: {entry!r}")

        tracker.finish()
        self.status.emit(f"[DEBUG] Uploaded. file_code: {file_code}")
        return file_code

    def _get_upload_server(self):
        url = f"{DATANODES_BASE_URL}/api/upload/server"
        last_error = None
        for attempt in range(1, 4):
            if self._cancel:
                return None, None
            try:
                resp = requests.get(url, params=self._params(), timeout=self._TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                self.status.emit(f"[DEBUG] Upload server response: {data}")
                if data.get("msg") != "OK" or "result" not in data or "sess_id" not in data:
                    raise RuntimeError(f"Unexpected /api/upload/server response: {data}")
                return data["result"], data["sess_id"]
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                self.status.emit(f"[DEBUG] /api/upload/server HTTPError (attempt {attempt}/3): {e}")
                self.status.emit(f"[DEBUG] Response content: {getattr(e.response, 'text', None)}")
                last_error = e
                if status not in (429, 500, 502, 503, 504):
                    raise
            except ValueError as e:
                # Non-JSON response
                self.status.emit(f"[DEBUG] /api/upload/server returned non-JSON (attempt {attempt}/3): {e}")
                last_error = RuntimeError(f"/api/upload/server returned non-JSON response: {e}")
            except Exception as e:
                self.status.emit(f"[DEBUG] /api/upload/server exception (attempt {attempt}/3): {e}")
                last_error = e
            time.sleep(min(2 ** (attempt - 1), 10))
        raise last_error or RuntimeError("Failed to get an upload server")

    # ── folder resolution (datanodes.to addresses folders by fld_id) ────────
    def _ensure_folder_id(self, path):
        """Walk `path` (e.g. '/Music/Album') one segment at a time, creating
        any folder that doesn't already exist, and return the fld_id of the
        final segment. Results are cached on self._folder_id_cache."""
        path = path.strip("/")
        if not path:
            return 0

        accumulated = ""
        parent_id = 0
        for segment in path.split("/"):
            accumulated = f"{accumulated}/{segment}"
            if accumulated in self._folder_id_cache:
                parent_id = self._folder_id_cache[accumulated]
                continue

            fld_id = self._find_child_folder(parent_id, segment)
            if fld_id is None:
                self.status.emit(f"[DEBUG] Creating folder: {accumulated}")
                fld_id = self._create_folder(parent_id, segment)
            else:
                self.status.emit(f"[DEBUG] Folder already exists: {accumulated}")

            self._folder_id_cache[accumulated] = fld_id
            parent_id = fld_id

        return parent_id

    def _find_child_folder(self, parent_id, name):
        url = f"{DATANODES_BASE_URL}/api/folder/list"
        resp = requests.get(url, params=self._params(fld_id=parent_id), timeout=self._TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        folders = (data.get("result") or {}).get("folders", [])
        for folder in folders:
            if folder.get("name") == name:
                return folder.get("fld_id")
        return None

    def _create_folder(self, parent_id, name):
        url = f"{DATANODES_BASE_URL}/api/folder/create"
        resp = requests.get(
            url,
            params=self._params(parent_id=parent_id, name=name),
            timeout=self._TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        fld_id = (data.get("result") or {}).get("fld_id")
        if fld_id is None:
            raise RuntimeError(f"Folder create did not return fld_id: {data}")
        return fld_id

    def _set_folder(self, file_code, fld_id):
        url = f"{DATANODES_BASE_URL}/api/file/set_folder"
        resp = requests.get(
            url,
            params=self._params(file_code=file_code, fld_id=fld_id),
            timeout=self._TIMEOUT,
        )
        resp.raise_for_status()
        self.status.emit(f"[DEBUG] Moved {file_code} to folder {fld_id}")

    @staticmethod
    def _fmt_size(b):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.3f} {unit}"
            b /= 1024
        return f"{b:.3f} PB"


# ── Files API Worker ─────────────────────────────────────────────────────────
class FilesWorker(QThread):
    """Generic background worker for Files-tab operations against
    datanodes.to. Folders are addressed by fld_id, files by file_code."""
    done    = pyqtSignal(object)   # result payload (varies by op)
    error   = pyqtSignal(str)
    _TIMEOUT = (5, 60)

    def __init__(self, op, api_key, **kwargs):
        super().__init__()
        self.op      = op          # 'list' | 'rename' | 'set_folder' | 'mkdir' | 'rename_folder' | 'clone' | 'deleted' | 'direct_link'
        self.api_key = api_key
        self.kwargs  = kwargs

    def _params(self, **extra):
        params = {"key": self.api_key}
        params.update(extra)
        return params

    def run(self):
        try:
            if self.op == "list":
                self._list()
            elif self.op == "rename":
                self._rename()
            elif self.op == "set_folder":
                self._set_folder()
            elif self.op == "mkdir":
                self._mkdir()
            elif self.op == "rename_folder":
                self._rename_folder()
            elif self.op == "clone":
                self._clone()
            elif self.op == "deleted":
                self._deleted()
            elif self.op == "direct_link":
                self._direct_link()
            else:
                raise ValueError(f"Unknown FilesWorker op: {self.op!r}")
        except Exception as e:
            self.error.emit(str(e))

    def _get(self, path, **params):
        resp = requests.get(
            f"{DATANODES_BASE_URL}{path}",
            params=self._params(**params),
            timeout=self._TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _list(self):
        fld_id   = self.kwargs.get("fld_id", 0)
        page     = self.kwargs.get("page", 1)
        per_page = self.kwargs.get("per_page", 20)
        data = self._get("/api/file/list", fld_id=fld_id, page=page, per_page=per_page)
        self.done.emit({"op": "list", "fld_id": fld_id, "data": data})

    def _rename(self):
        file_code = self.kwargs["file_code"]
        new_name  = self.kwargs["new_name"]
        data = self._get("/api/file/rename", file_code=file_code, name=new_name)
        self.done.emit({"op": "rename", "file_code": file_code, "data": data})

    def _set_folder(self):
        file_code = self.kwargs["file_code"]
        fld_id    = self.kwargs["fld_id"]
        data = self._get("/api/file/set_folder", file_code=file_code, fld_id=fld_id)
        self.done.emit({"op": "set_folder", "file_code": file_code, "fld_id": fld_id, "data": data})

    def _mkdir(self):
        parent_id = self.kwargs.get("parent_id", 0)
        name      = self.kwargs["name"]
        data = self._get("/api/folder/create", parent_id=parent_id, name=name)
        self.done.emit({"op": "mkdir", "name": name, "data": data})

    def _rename_folder(self):
        fld_id   = self.kwargs["fld_id"]
        new_name = self.kwargs["new_name"]
        data = self._get("/api/folder/rename", fld_id=fld_id, name=new_name)
        self.done.emit({"op": "rename_folder", "fld_id": fld_id, "data": data})

    def _clone(self):
        file_code = self.kwargs["file_code"]
        data = self._get("/api/file/clone", file_code=file_code)
        self.done.emit({"op": "clone", "file_code": file_code, "data": data})

    def _deleted(self):
        data = self._get("/api/files/deleted")
        self.done.emit({"op": "deleted", "data": data})

    def _direct_link(self):
        file_code = self.kwargs["file_code"]
        data = self._get("/api/file/direct_link", file_code=file_code)
        self.done.emit({"op": "direct_link", "file_code": file_code, "data": data})


# ── Remote Ingest Worker ─────────────────────────────────────────────────────
class RemoteWorker(QThread):
    """Queues/checks remote-URL uploads via the (overloaded) /api/upload/url
    endpoint. datanodes.to has no documented job-cancel endpoint, so `cancel`
    is not supported here and will emit an error if attempted."""
    done  = pyqtSignal(object)
    error = pyqtSignal(str)
    _TIMEOUT = (5, 60)

    def __init__(self, op, api_key, **kwargs):
        super().__init__()
        self.op      = op        # 'ingest' | 'status'
        self.api_key = api_key
        self.kwargs  = kwargs

    def _params(self, **extra):
        params = {"key": self.api_key}
        params.update(extra)
        return params

    def run(self):
        try:
            if self.op == "ingest":
                self._ingest()
            elif self.op == "status":
                self._status()
            elif self.op == "cancel":
                raise NotImplementedError(
                    "datanodes.to has no documented endpoint to cancel a "
                    "queued remote upload."
                )
            else:
                raise ValueError(f"Unknown RemoteWorker op: {self.op!r}")
        except Exception as e:
            self.error.emit(str(e))

    def _ingest(self):
        url = f"{DATANODES_BASE_URL}/api/upload/url"
        resp = requests.get(
            url,
            params=self._params(
                url=self.kwargs["source_url"],
                fld_id=self.kwargs.get("fld_id", 0),
            ),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.done.emit({"op": "ingest", "data": data})

    def _status(self):
        url = f"{DATANODES_BASE_URL}/api/upload/url"
        resp = requests.get(
            url,
            params=self._params(file_code=self.kwargs["file_code"]),
            timeout=self._TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        self.done.emit({"op": "status", "data": data})


# ── Account / Storage Worker ─────────────────────────────────────────────────
class StorageWorker(QThread):
    """Fetches account/storage info for the titlebar indicator via
    /api/account/info (storage_used / storage_left)."""
    done  = pyqtSignal(object)   # dict: storage_used, storage_left, balance, premium_expire
    error = pyqtSignal(str)
    _TIMEOUT = (5, 60)

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        try:
            resp = requests.get(
                f"{DATANODES_BASE_URL}/api/account/info",
                params={"key": self.api_key},
                timeout=self._TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            self.done.emit(data.get("result", data))
        except Exception as e:
            self.error.emit(str(e))


# ── Direct Download Worker ────────────────────────────────────────────────────
class DownloadWorker(QThread):
    """Downloads a file from a datanodes.to direct link (see
    /api/file/direct_link) directly to a local path."""
    progress = pyqtSignal(float)    # 0.0-100.0
    speed    = pyqtSignal(float)    # bytes/sec
    done     = pyqtSignal(str)      # local file path on success
    error    = pyqtSignal(str)

    def __init__(self, url: str, dest_path: str, parent=None):
        super().__init__(parent)
        self.url       = url
        self.dest_path = dest_path
        self._cancel   = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            resp = requests.get(self.url, stream=True, timeout=60)
            resp.raise_for_status()
            total   = int(resp.headers.get("content-length", 0))
            fetched = 0
            start   = time.monotonic()
            with open(self.dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._cancel:
                        return
                    if chunk:
                        fh.write(chunk)
                        fetched += len(chunk)
                        elapsed  = max(time.monotonic() - start, 0.001)
                        self.speed.emit(fetched / elapsed)
                        if total:
                            self.progress.emit(min(fetched / total * 100, 99.999))
            self.progress.emit(100.0)
            self.done.emit(self.dest_path)
        except Exception as e:
            self.error.emit(str(e))