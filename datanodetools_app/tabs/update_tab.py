"""
tabs/update_tab.py — Datanodes.to file-update tab for DataNodeTools.

Flow
────
1. User clicks "Browse Remote" → RemoteFileBrowserDialog (mirrors
   FolderBrowserDialog style) to pick an existing file by navigating folders.
2. User clicks "Browse Local" → QFileDialog to pick the replacement file.
3. Click "Update File":
   a. GET /api/upload/server  →  sess_id + upload URL
   b. POST multipart to upload URL (streamed with progress)
   c. At 98 % progress  →  GET /api/file/rename  (remote file → local filename)
   d. Upload completes  →  new file_code lands with the same name; remote replaced.
"""

import os
import threading
import requests as _req

from PyQt6.QtCore import Qt, QSize, QThread, QTimer, pyqtSignal, QObject, QPoint
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QSizeGrip,
    QSizePolicy, QVBoxLayout, QWidget, QDialog,
)

from ..ui.icons import lucide_icon
from ..theme import get_accent, accent_qcolor, get_font, get_background_palette
from ..dialogs import DataNodeDialog, _gold_btn, _grey_btn, _OUTSTANDING_FETCH_WORKERS

_BASE = "https://datanodes.to"


# ── Background worker: fetches one folder's contents (folders + files) ─────────

class _FileFetchWorker(QThread):
    done = pyqtSignal(int, object)   # (fld_id, data_dict | Exception)

    _session = None

    @classmethod
    def _get_session(cls):
        if cls._session is None:
            adapter = _req.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=4, max_retries=0)
            cls._session = _req.Session()
            cls._session.mount("https://", adapter)
            cls._session.mount("http://",  adapter)
        return cls._session

    def __init__(self, api_key, fld_id, cancel_token):
        super().__init__()
        self.api_key      = api_key
        self.fld_id       = fld_id
        self._cancel      = cancel_token

    def run(self):
        try:
            session = self._get_session()
            resp = session.get(
                f"{_BASE}/api/folder/list",
                params={"fld_id": self.fld_id, "key": self.api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not self._cancel[0]:
                self.done.emit(self.fld_id, data)
        except Exception as e:
            if not self._cancel[0]:
                self.done.emit(self.fld_id, e)


# ── Remote file browser dialog ─────────────────────────────────────────────────

class RemoteFileBrowserDialog(DataNodeDialog):
    """
    Mirrors FolderBrowserDialog exactly but lists both folders and files.
    Double-click a folder to navigate into it; single-click a file to select it.
    .selected_file is set to {"file_code": ..., "name": ...} on accept.
    """

    _fld_cache: dict = {}

    def __init__(self, api_key, parent=None):
        super().__init__("Browse remote files", parent, min_size=(460, 480))
        self.api_key        = api_key
        self.current        = 0
        self.selected_file  = None
        self._breadcrumb: list[tuple[int, str]] = [(0, "/")]
        self._worker        = None
        self._cancel_token  = [False]
        self._dead_workers  = []
        self._navigating    = False

        lay = self.content_layout
        grip_item = lay.takeAt(lay.count() - 1)

        # ── Breadcrumb bar ───────────────────────────────────────────────────
        path_row = QHBoxLayout()
        path_row.setSpacing(6)

        path_icon = QLabel()
        path_icon.setFixedSize(18, 18)
        path_icon.setScaledContents(True)
        path_icon.setStyleSheet("background:transparent;")
        try:
            path_icon.setPixmap(lucide_icon("folder", get_accent(), 18).pixmap(18, 18))
        except Exception:
            path_icon.setText("📂")
        path_row.addWidget(path_icon)

        try:
            from ..theme import notifier as _notifier
            _notifier().accent_changed.connect(
                lambda _old, new: path_icon.setPixmap(lucide_icon("folder", new, 18).pixmap(18, 18))
            )
        except Exception:
            pass

        self.path_edit = QLineEdit("/")
        self.path_edit.setReadOnly(True)
        path_row.addWidget(self.path_edit)
        lay.addLayout(path_row)

        # ── List ─────────────────────────────────────────────────────────────
        self.list = QListWidget()
        try:
            acc     = get_accent()
            acc_hov = acc + "33"
            pal     = get_background_palette()
            list_bg  = pal["bg7"]
            list_bd  = pal["border"]
            list_fg  = pal["text"]
            list_hov = pal["bg6"]
        except Exception:
            acc      = "#c8a96e"
            acc_hov  = "#c8a96e33"
            list_bg  = "#141210"
            list_bd  = "#2e2b27"
            list_fg  = "#f0ece6"
            list_hov = "#1e1c19"
        self.list.setStyleSheet(f"""
            QListWidget {{ background:{list_bg}; border:1px solid {list_bd};
                          border-radius:8px; color:{list_fg}; font-size:13px; }}
            QListWidget::item {{ padding:6px 10px; }}
            QListWidget::item:selected {{ background:{acc_hov}; color:{list_fg}; }}
            QListWidget::item:hover {{ background:{list_hov}; }}
        """)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.currentItemChanged.connect(self._on_selection_changed)
        lay.addWidget(self.list)

        # ── Status ────────────────────────────────────────────────────────────
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(
            f"color:{accent_qcolor().name()}; font-size:11px; background:transparent;"
        )
        lay.addWidget(self.status_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel_btn = _grey_btn("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._ok_btn = _gold_btn("Select file")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self._ok_btn)

        lay.addLayout(btn_row)

        if grip_item:
            lay.addItem(grip_item)

        self._navigate(0)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _navigate(self, fld_id, push_crumb=None, pop_to=None):
        self._cancel_token[0] = True
        self._cancel_token = [False]

        if self._worker is not None:
            try:
                self._worker.done.disconnect(self._on_fetch_done)
            except RuntimeError:
                pass
            self._dead_workers = [w for w in self._dead_workers if not w.isFinished()]
            self._dead_workers.append(self._worker)
            self._worker = None

        if push_crumb is not None:
            self._breadcrumb.append(push_crumb)
        elif pop_to is not None:
            self._breadcrumb = self._breadcrumb[:pop_to + 1]

        self.current = fld_id
        self.path_edit.setText(self._breadcrumb_text())
        self._ok_btn.setEnabled(False)
        self.selected_file = None

        if fld_id in RemoteFileBrowserDialog._fld_cache:
            self._render(fld_id, RemoteFileBrowserDialog._fld_cache[fld_id])
        else:
            self.list.clear()
            self.status_lbl.setText("Loading…")

        w = _FileFetchWorker(self.api_key, fld_id, self._cancel_token)
        w.done.connect(self._on_fetch_done)
        self._worker = w
        try:
            _OUTSTANDING_FETCH_WORKERS.append(w)
        except Exception:
            pass
        w.start()

    def _breadcrumb_text(self, names=None):
        if names is None:
            names = [name for _fid, name in self._breadcrumb]
        joined = "/".join(n for n in names if n != "/")
        return "/" + joined if joined else "/"

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _on_fetch_done(self, fld_id, result):
        if fld_id != self.current:
            return
        self._worker = None

        if isinstance(result, Exception):
            e = result
            if hasattr(e, "response") and e.response is not None:
                msg = f"Error {e.response.status_code}: {e.response.text[:200]}"
            else:
                first_line = str(e).splitlines()[0].strip()
                if "):" in first_line:
                    first_line = first_line.split("):", 1)[-1].strip()
                msg = f"Connection error: {first_line[:120]}"
            self.status_lbl.setText(msg)
            return

        RemoteFileBrowserDialog._fld_cache[fld_id] = result
        self._render(fld_id, result)

    # ── Render ────────────────────────────────────────────────────────────────

    def _render(self, fld_id, data):
        self._navigating = True
        self.list.blockSignals(True)
        self.list.clear()

        result = data.get("result") or data if isinstance(data, dict) else {}
        if isinstance(result, dict):
            folder_entries = result.get("folders") or []
            file_entries   = result.get("files")   or []
        else:
            folder_entries = []
            file_entries   = []

        # ".. go up" entry
        if len(self._breadcrumb) > 1:
            parent_idx = len(self._breadcrumb) - 2
            parent_fld_id, _ = self._breadcrumb[parent_idx]
            item = QListWidgetItem(".. (go up)")
            item.setData(Qt.ItemDataRole.UserRole, ("up", parent_fld_id, parent_idx))
            item.setForeground(accent_qcolor())
            try:
                item.setIcon(lucide_icon("folder", get_accent(), 12))
            except Exception:
                pass
            self.list.addItem(item)

        # Folders
        folders = []
        for entry in folder_entries:
            if not isinstance(entry, dict):
                continue
            name     = entry.get("name") or ""
            child_id = entry.get("fld_id")
            if name and child_id is not None:
                folders.append((name, int(child_id)))
        folders.sort(key=lambda x: x[0].lower())

        for name, child_id in folders:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, ("dir", child_id, name))
            try:
                item.setIcon(lucide_icon("folder", get_accent(), 12))
            except Exception:
                pass
            self.list.addItem(item)

        # Files
        files = []
        for entry in file_entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or ""
            code = entry.get("file_code") or ""
            if name and code:
                files.append((name, code))
        files.sort(key=lambda x: x[0].lower())

        for name, code in files:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, ("file", code, name))
            try:
                item.setIcon(lucide_icon("upload-cloud", get_accent(), 12))
            except Exception:
                pass
            self.list.addItem(item)

        self.list.blockSignals(False)
        self.path_edit.setText(self._breadcrumb_text())
        n_files = len(files)
        n_dirs  = len(folders)
        self.status_lbl.setText(
            f"{n_dirs} folder{'s' if n_dirs != 1 else ''}, "
            f"{n_files} file{'s' if n_files != 1 else ''}"
        )
        self._navigating = False

    # ── Selection / interaction ───────────────────────────────────────────────

    def _on_selection_changed(self, current, _previous):
        if self._navigating or not current:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        kind = data[0]
        if kind == "file":
            _, code, name = data
            self.selected_file = {"file_code": code, "name": name}
            self._ok_btn.setEnabled(True)
        else:
            self.selected_file = None
            self._ok_btn.setEnabled(False)

    def _on_double_click(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        kind = data[0]
        if kind == "dir":
            _, fld_id, name = data
            self._navigate(fld_id, push_crumb=(fld_id, name))
        elif kind == "up":
            _, fld_id, idx = data
            self._navigate(fld_id, pop_to=idx)
        elif kind == "file":
            self._on_accept()

    def _on_accept(self):
        if self.selected_file:
            self.accept()

    def closeEvent(self, event):
        self._cancel_token[0] = True
        if self._worker is not None:
            try:
                self._worker.done.disconnect(self._on_fetch_done)
            except RuntimeError:
                pass
        super().closeEvent(event)


# ── Upload worker ──────────────────────────────────────────────────────────────

class _UploadWorker(QObject):
    progress       = pyqtSignal(int)
    rename_trigger = pyqtSignal()
    done           = pyqtSignal(str)
    error          = pyqtSignal(str)

    def __init__(self, api_key, local_path):
        super().__init__()
        self._api_key    = api_key
        self._local_path = local_path

    def run(self):
        try:
            r = _req.get(
                f"{_BASE}/api/upload/server",
                params={"key": self._api_key},
                timeout=15,
            )
            r.raise_for_status()
            j = r.json()
            if j.get("status") != 200:
                self.error.emit(f"upload/server: {j.get('msg', 'unknown error')}")
                return
            sess_id    = j["sess_id"]
            upload_url = j["result"]

            total = os.path.getsize(self._local_path)
            fname = os.path.basename(self._local_path)
            CHUNK = 256 * 1024

            progress_sig   = self.progress
            rename_trigger = self.rename_trigger
            rename_ref     = [False]

            def _file_gen():
                sent = 0
                with open(self._local_path, "rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        sent += len(chunk)
                        pct = min(int(sent * 100 / total), 100) if total else 100
                        progress_sig.emit(pct)
                        if pct >= 98 and not rename_ref[0]:
                            rename_ref[0] = True
                            rename_trigger.emit()
                        yield chunk

            try:
                from requests_toolbelt import MultipartEncoder
                encoder = MultipartEncoder(fields={
                    "sess_id": sess_id,
                    "utype":   "prem",
                    "file_0":  (fname, _file_gen(), "application/octet-stream"),
                })
                resp = _req.post(
                    upload_url,
                    data=encoder,
                    headers={"Content-Type": encoder.content_type},
                    timeout=None,
                )
            except ImportError:
                with open(self._local_path, "rb") as fh:
                    data = fh.read()
                if not rename_ref[0]:
                    rename_ref[0] = True
                    rename_trigger.emit()
                resp = _req.post(
                    upload_url,
                    files={"file_0": (fname, data, "application/octet-stream")},
                    data={"sess_id": sess_id, "utype": "prem"},
                    timeout=None,
                )

            resp.raise_for_status()
            result = resp.json()
            if not result or result[0].get("file_status") != "OK":
                self.error.emit(f"Upload failed: {result}")
                return

            self.progress.emit(100)
            self.done.emit(result[0]["file_code"])

        except Exception as exc:
            self.error.emit(str(exc))


# ── Tab ────────────────────────────────────────────────────────────────────────

class UpdateTab(QWidget):
    """Replace a datanodes.to file by uploading a new version under its name."""

    def __init__(self, get_api_key, parent=None):
        super().__init__(parent)
        self.get_api_key    = get_api_key
        self._remote_file   = None   # {"file_code": ..., "name": ...}
        self._local_path    = ""
        self._thread        = None
        self._worker        = None
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)
        self._build_remote_row(outer)
        self._build_local_row(outer)
        self._build_action_row(outer)
        self._build_progress(outer)
        self._build_log(outer)
        outer.addStretch()

    def _build_remote_row(self, lay):
        lbl = QLabel("Remote file to replace:")
        lbl.setStyleSheet("color:#9ca3af; font-size:11px;")
        lay.addWidget(lbl)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.remote_edit = QLineEdit()
        self.remote_edit.setPlaceholderText("No file selected…")
        self.remote_edit.setReadOnly(True)
        self.remote_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.remote_edit)

        self.remote_btn = self._tb("  Browse Remote", "folder-open", get_accent(), self._browse_remote)
        row.addWidget(self.remote_btn)
        lay.addLayout(row)

    def _build_local_row(self, lay):
        lbl = QLabel("Local file to upload:")
        lbl.setStyleSheet("color:#9ca3af; font-size:11px;")
        lay.addWidget(lbl)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.local_edit = QLineEdit()
        self.local_edit.setPlaceholderText("No file selected…")
        self.local_edit.setReadOnly(True)
        self.local_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.local_edit)

        self.local_btn = self._tb("  Browse Local", "folder-open", get_accent(), self._browse_local)
        row.addWidget(self.local_btn)
        lay.addLayout(row)

    def _build_action_row(self, lay):
        row = QHBoxLayout()
        self.update_btn = QPushButton("  Update File")
        self.update_btn.setObjectName("tb_btn")
        self.update_btn.setIcon(lucide_icon("upload-cloud", get_accent(), 13))
        self.update_btn.setIconSize(QSize(13, 13))
        self.update_btn.clicked.connect(self._start_update)
        self.update_btn.setEnabled(False)
        row.addWidget(self.update_btn)
        row.addStretch()

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(
            f"color:{accent_qcolor().name()}; font-size:{int(get_font()[1])}px; background:transparent;"
        )
        row.addWidget(self.status_lbl)
        lay.addLayout(row)

        try:
            from ..theme import notifier
            notifier().accent_changed.connect(lambda _old, _new: self._on_accent_changed(_new))
        except Exception:
            pass

    def _build_progress(self, lay):
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(16)
        self.progress.hide()
        lay.addWidget(self.progress)

    def _build_log(self, lay):
        self.log_lbl = QLabel("")
        self.log_lbl.setObjectName("log_console")
        self.log_lbl.setWordWrap(True)
        self.log_lbl.hide()
        lay.addWidget(self.log_lbl)

    def _tb(self, label, icon_name, color, slot):
        btn = QPushButton(label)
        btn.setObjectName("tb_btn")
        btn.setIcon(lucide_icon(icon_name, color, 13))
        btn.setIconSize(QSize(13, 13))
        btn.clicked.connect(slot)
        return btn

    # ── Accent refresh ───────────────────────────────────────────────────────

    def _on_accent_changed(self, new: str):
        try:
            self.remote_btn.setIcon(lucide_icon("folder-open", new, 13))
            self.local_btn.setIcon(lucide_icon("folder-open", new, 13))
            self.update_btn.setIcon(lucide_icon("upload-cloud", new, 13))
            self.status_lbl.setStyleSheet(
                f"color:{new}; font-size:{int(get_font()[1])}px; background:transparent;"
            )
        except Exception:
            pass

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse_remote(self):
        api_key = self.get_api_key()
        if not api_key:
            self._status("⚠ Enter your API key in Settings first.")
            return
        dlg = RemoteFileBrowserDialog(api_key, parent=self)
        if not dlg.exec():
            return
        self._remote_file = dlg.selected_file
        self.remote_edit.setText(self._remote_file["name"])
        self._check_ready()

    def _browse_local(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select file to upload")
        if path:
            self._local_path = path
            self.local_edit.setText(path)
        self._check_ready()

    def _check_ready(self):
        self.update_btn.setEnabled(bool(self._remote_file) and bool(self._local_path))

    # ── Upload flow ───────────────────────────────────────────────────────────

    def _start_update(self):
        api_key = self.get_api_key()
        if not api_key or not self._remote_file or not self._local_path:
            return

        local_name  = os.path.basename(self._local_path)
        remote_name = self._remote_file["name"]
        remote_code = self._remote_file["file_code"]

        if QMessageBox.question(
            self, "Confirm Update",
            f"Replace \"{remote_name}\" with \"{local_name}\"?\n\n"
            "The remote file will be renamed to match the upload at 98 %, "
            "then overwritten when the upload finishes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self._log(f"Starting upload of {local_name}…")

        self._worker = _UploadWorker(api_key, self._local_path)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.rename_trigger.connect(
            lambda: self._do_rename(api_key, remote_code, local_name)
        )
        self._worker.done.connect(self._on_upload_done)
        self._worker.error.connect(self._on_upload_error)
        self._worker.done.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _do_rename(self, api_key, file_code, new_name):
        self._log(f"⟳ Renaming remote file to \"{new_name}\"…")
        try:
            r = _req.get(
                f"{_BASE}/api/file/rename",
                params={"file_code": file_code, "name": new_name, "key": api_key},
                timeout=15,
            )
            r.raise_for_status()
            j = r.json()
            if j.get("status") == 200:
                self._log(f"✓ Remote file renamed to \"{new_name}\".")
            else:
                self._log(f"⚠ Rename response: {j.get('msg', 'unknown')}")
        except Exception as exc:
            self._log(f"⚠ Rename failed (upload continues): {exc}")

    def _on_progress(self, pct):
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct} %")

    def _on_upload_done(self, file_code):
        self._log(f"✓ Upload complete. New file code: {file_code}")
        self._status("Done.")
        self._set_busy(False)
        self.progress.setValue(100)

    def _on_upload_error(self, msg):
        self._log(f"✗ {msg}")
        self._status("✗ Error")
        self._set_busy(False)
        QMessageBox.warning(self, "Upload Error", msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_busy(self, busy):
        self.update_btn.setEnabled(not busy)
        self.remote_btn.setEnabled(not busy)
        self.local_btn.setEnabled(not busy)
        if busy:
            self.progress.setValue(0)
            self.progress.show()

    def _log(self, msg):
        self.log_lbl.setText(msg)
        self.log_lbl.show()

    def _status(self, msg):
        self.status_lbl.setText(msg)