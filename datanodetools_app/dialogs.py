import requests

from PyQt6.QtCore import Qt, QThread, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QFrame,
    QSizeGrip,
)

from .ui import lucide_icon


# Keep references to in-flight fetch threads here so dialogs can be destroyed
# without the QThread objects being garbage-collected while still running.
_OUTSTANDING_FETCH_WORKERS = []

# ── Shared: DataNode-styled frameless dialog base ────────────────────────────────
class DataNodeDialog(QDialog):
    """
    Frameless dialog base that draws the same dark titlebar as the main window.
    Subclasses call super().__init__(...) then build their content inside
    self.content_layout (a QVBoxLayout already added below the titlebar).
    """

    def __init__(self, title: str, parent=None, min_size=(420, 380)):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMinimumSize(*min_size)
        self.setStyleSheet(parent.styleSheet() if parent else "")

        # Track drag
        self._drag_pos: QPoint | None = None

        # ── Root layout ───────────────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Titlebar ──────────────────────────────────────────────────────────
        tb = QFrame()
        tb.setObjectName("titlebar")
        tb.setFixedHeight(42)
        tb_lay = QHBoxLayout(tb)
        tb_lay.setContentsMargins(12, 0, 8, 0)
        tb_lay.setSpacing(6)

        try:
            from .theme import get_accent, get_font
            _dot_color = get_accent()
            _fs = int(get_font()[1])
        except Exception:
            _dot_color = "#c8a96e"
            _fs = 13
        dot = QLabel("◆")
        dot.setStyleSheet(f"color:{_dot_color}; font-size:{max(8, _fs-3)}px; background:transparent;")
        tb_lay.addWidget(dot)

        if title:
            title_lbl = QLabel()
            title_lbl.setStyleSheet(
                f"color:{_dot_color}; font-size:{max(9, _fs-2)}px; font-weight:600;"
                f" background:transparent; margin-left:6px;"
            )
            metrics = title_lbl.fontMetrics()
            title_lbl.setText(metrics.elidedText(title, Qt.TextElideMode.ElideRight, 320))
            title_lbl.setToolTip(title)
            tb_lay.addWidget(title_lbl)
            self._title_lbl = title_lbl
        else:
            self._title_lbl = None

        tb_lay.addStretch()

        close_btn = QPushButton()
        try:
            from .theme import get_accent
            _xcol = get_accent()
        except Exception:
            _xcol = "#c8a96e"
        close_btn.setIcon(lucide_icon("x", _xcol, 12))
        close_btn.setObjectName("tb_close")
        close_btn.setFixedSize(32, 28)
        close_btn.clicked.connect(self.reject)
        tb_lay.addWidget(close_btn)

        root.addWidget(tb)

        div = QFrame()
        div.setObjectName("divider")
        div.setFixedHeight(1)
        root.addWidget(div)

        try:
            from .theme import get_background_palette
            _pal = get_background_palette()
        except Exception:
            _pal = {"bg1": "#181614", "text": "#f0ece6"}
        content_widget = QFrame()
        content_widget.setStyleSheet(f"background:{_pal['bg1']};")
        self.content_layout = QVBoxLayout(content_widget)
        self.content_layout.setContentsMargins(14, 14, 14, 14)
        self.content_layout.setSpacing(10)
        root.addWidget(content_widget)

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background:transparent;")
        grip_row.addWidget(grip)
        self.content_layout.addLayout(grip_row)

        tb.mousePressEvent   = self._tb_press
        tb.mouseMoveEvent    = self._tb_move
        tb.mouseReleaseEvent = self._tb_release

    def _tb_press(self, ev: QMouseEvent):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _tb_move(self, ev: QMouseEvent):
        if self._drag_pos and ev.buttons() == Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)

    def _tb_release(self, ev: QMouseEvent):
        self._drag_pos = None


# ── Shared: styled button helpers ─────────────────────────────────────────────
def _gold_btn(text: str, width=160) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("upload_btn")
    btn.setFixedSize(width, 36)
    return btn


def _grey_btn(text: str, width=160) -> QPushButton:
    btn = QPushButton(text)
    btn.setFixedSize(width, 36)
    try:
        from .theme import get_font, get_background_palette
        fsz = int(get_font()[1])
        pal = get_background_palette()
        bg  = pal["bg5"]
        fg  = pal["text"]
        bd  = pal["border2"]
    except Exception:
        fsz = 13
        bg  = "#1e1c19"
        fg  = "#f0ece6"
        bd  = "#3d3a35"
    btn.setStyleSheet(
        f"min-height:0px; padding:0px 16px; font-size:{fsz}px; font-weight:600;"
        f"background:{bg}; color:{fg}; border:1px solid {bd}; border-radius:7px;"
    )
    return btn


# ── Background worker for folder listings ─────────────────────────────────────
class _FolderFetchWorker(QThread):
    """
    Fetches one folder listing off the main thread.

    Uses a class-level persistent requests.Session so the underlying
    TCP+TLS connection is reused across navigations and dialog openings.
    Without this, every folder navigation pays the full TLS handshake cost
    (200-400 ms extra on a typical connection) on top of the API round-trip.

    A cancel token (single-element list) lets the caller suppress the result
    if the user has already navigated elsewhere before the response arrives.
    """
    done = pyqtSignal(int, object)   # (fld_id, data_dict | Exception)

    # Persistent session shared across all workers — keeps the TCP+TLS
    # connection alive so subsequent fetches skip the handshake entirely.
    _session: "requests.Session | None" = None

    @classmethod
    def _get_session(cls) -> "requests.Session":
        if cls._session is None:
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1,
                pool_maxsize=4,
                max_retries=0,
            )
            cls._session = requests.Session()
            cls._session.mount("https://", adapter)
            cls._session.mount("http://",  adapter)
        return cls._session

    def __init__(self, api_key: str, base_url: str, fld_id: int, cancel_token: list):
        super().__init__()
        self.api_key      = api_key
        self.base_url     = base_url.rstrip("/")
        self.fld_id       = fld_id
        self._cancel      = cancel_token

    def run(self):
        try:
            session = self._get_session()
            resp = session.get(
                f"{self.base_url}/api/folder/list",
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


# ── Remote Folder Browser ─────────────────────────────────────────────────────
class FolderBrowserDialog(DataNodeDialog):
    """Fetches folders from the DataNode API and lets the user navigate & pick one.

    Datanodes folders are identified by numeric fld_id (root = 0); there is
    no path-string addressing, so navigation is tracked as a breadcrumb
    stack of (fld_id, name) rather than a typed path.
    """

    # Class-level cache shared across all dialog instances in this session.
    # Maps fld_id -> folder list data so re-visiting a folder is instant.
    _fld_cache: dict = {}

    def __init__(self, api_key, base_url, current_fld_id=0, parent=None):
        super().__init__("Browse remote folders", parent, min_size=(460, 440))
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self.current  = int(current_fld_id or 0)
        self.selected = self.current
        self.selected_path = "/"
        # Breadcrumb stack of (fld_id, name) from root down to current.
        # If we're not starting at root, we don't know the ancestor chain
        # (the API gives no parent pointer), so start the trail at current
        # itself; "go up" will simply be unavailable until the user
        # navigates further down from here.
        self._breadcrumb: list[tuple[int, str]] = [(0, "/")] if self.current == 0 \
            else [(self.current, "…")]
        self._worker: _FolderFetchWorker | None = None
        self._cancel_token: list = [False]
        self._dead_workers: list[_FolderFetchWorker] = []
        self._navigating: bool = False   # suppresses auto-highlight during render

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
            from .theme import get_accent as _ga
            path_icon.setPixmap(lucide_icon("folder", _ga(), 18).pixmap(18, 18))
        except Exception:
            path_icon.setText("📂")
        path_row.addWidget(path_icon)

        # Keep breadcrumb icon in sync with accent changes
        try:
            from .theme import notifier as _notifier
            _notifier().accent_changed.connect(
                lambda _old, new: path_icon.setPixmap(lucide_icon("folder", new, 18).pixmap(18, 18))
            )
        except Exception:
            pass

        # Read-only breadcrumb — there's no typed-path addressing scheme
        # under fld_id, so this just reflects where navigation has taken us.
        self.path_edit = QLineEdit("/")
        self.path_edit.setReadOnly(True)
        path_row.addWidget(self.path_edit)

        lay.addLayout(path_row)

        # ── Folder list ───────────────────────────────────────────────────────
        self.list = QListWidget()
        try:
            acc = get_accent()
            acc_hov = acc + "33"
        except Exception:
            from .theme import DEFAULT_ACCENT
            acc = DEFAULT_ACCENT
            acc_hov = DEFAULT_ACCENT + "33"
        try:
            from .theme import get_background_palette
            _lpal = get_background_palette()
            _list_bg  = _lpal["bg7"]
            _list_bd  = _lpal["border"]
            _list_fg  = _lpal["text"]
            _list_hov = _lpal["bg6"]
        except Exception:
            _list_bg  = "#141210"
            _list_bd  = "#2e2b27"
            _list_fg  = "#f0ece6"
            _list_hov = "#1e1c19"
        self.list.setStyleSheet(f"""
            QListWidget {{ background:{_list_bg}; border:1px solid {_list_bd};
                          border-radius:8px; color:{_list_fg}; font-size:13px; }}
            QListWidget::item {{ padding:6px 10px; }}
            QListWidget::item:selected {{ background:{acc_hov}; color:{_list_fg}; }}
            QListWidget::item:hover {{ background:{_list_hov}; }}
        """)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.currentItemChanged.connect(self._on_selection_changed)
        lay.addWidget(self.list)

        # Ensure folder icons update if the accent changes at runtime
        try:
            from .theme import notifier
            def _refresh_list(old, new):
                for i in range(self.list.count()):
                    it = self.list.item(i)
                    try:
                        data = it.data(Qt.ItemDataRole.UserRole)
                    except Exception:
                        data = None
                    if data and isinstance(data, tuple) and data[0] == "dir":
                        try:
                            it.setIcon(lucide_icon("folder", new, 12))
                        except Exception:
                            pass
            notifier().accent_changed.connect(_refresh_list)
        except Exception:
            pass

        # ── Status ────────────────────────────────────────────────────────────
        self.status_lbl = QLabel("")
        from .theme import accent_qcolor, notifier
        self.status_lbl.setStyleSheet(f"color:{accent_qcolor().name()}; font-size:11px; background:transparent;")
        lay.addWidget(self.status_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel_btn = _grey_btn("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._ok_btn = _gold_btn("Select this folder")
        self._ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self._ok_btn)

        lay.addLayout(btn_row)

        if grip_item:
            lay.addItem(grip_item)

        self._navigate(self.current)

    # ── Navigate ──────────────────────────────────────────────────────────────
    def _navigate(self, fld_id: int, push_crumb: tuple[int, str] | None = None,
                  pop_to: int | None = None):
        """
        Navigate to fld_id.
        - push_crumb=(fld_id, name): descending into a child folder.
        - pop_to=index: going up — truncate the breadcrumb back to that index.
        Neither is given on the initial navigation (root).
        """
        # Cancel any in-flight worker
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

        self.current  = fld_id
        self.selected = fld_id
        self.path_edit.setText(self._breadcrumb_text())
        self._ok_btn.setEnabled(True)
        # Serve cached data instantly if available — identical render path as
        # the original synchronous version
        if fld_id in FolderBrowserDialog._fld_cache:
            self._render(fld_id, FolderBrowserDialog._fld_cache[fld_id])
            self.status_lbl.setText(
                self.status_lbl.text().rstrip("…") + "  (refreshing…)"
                if self.status_lbl.text() else "Refreshing…"
            )
        else:
            self.list.clear()
            self.status_lbl.setText("Loading…")

        # Always fetch fresh in the background
        w = _FolderFetchWorker(self.api_key, self.base_url, fld_id, self._cancel_token)
        w.done.connect(self._on_fetch_done)
        self._worker = w
        # retain a module-level reference to avoid QThread: Destroyed while running
        try:
            _OUTSTANDING_FETCH_WORKERS.append(w)
        except Exception:
            pass
        w.start()

    def _breadcrumb_text(self, names: list[str] | None = None) -> str:
        if names is None:
            names = [name for _fid, name in self._breadcrumb]
        joined = "/".join(n for n in names if n != "/")
        return "/" + joined if joined else "/"

    # ── Fetch result ──────────────────────────────────────────────────────────
    def _on_fetch_done(self, fld_id: int, result):
        if fld_id != self.current:
            return

        self._worker = None
        self._ok_btn.setEnabled(True)

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

        # Cache the result for instant re-render next time
        FolderBrowserDialog._fld_cache[fld_id] = result
        self._render(fld_id, result)
        # Remove finished worker references so list doesn't grow unbounded
        try:
            global _OUTSTANDING_FETCH_WORKERS
            _OUTSTANDING_FETCH_WORKERS = [w for w in _OUTSTANDING_FETCH_WORKERS if not getattr(w, 'isFinished', lambda: True)()]
        except Exception:
            pass

    # ── Render folder list ─────────────────────────────────────────────────────
    def _render(self, fld_id: int, data):
        self._navigating = True
        self.list.blockSignals(True)
        self.list.clear()

        # "▲ .." entry — only available if we have a known parent in the
        # breadcrumb trail (we may not, if we started mid-tree).
        if len(self._breadcrumb) > 1:
            parent_idx = len(self._breadcrumb) - 2
            parent_fld_id, _name = self._breadcrumb[parent_idx]
            item = QListWidgetItem(".. (go up)")
            item.setData(Qt.ItemDataRole.UserRole, ("up", parent_fld_id, parent_idx))
            from .theme import accent_qcolor
            item.setForeground(accent_qcolor())
            try:
                item.setIcon(lucide_icon("folder", get_accent(), 12))
            except Exception:
                pass
            self.list.addItem(item)

        folder_entries = []
        if isinstance(data, dict):
            result = data.get("result") or data
            if isinstance(result, dict):
                folder_entries = result.get("folders") or []
            elif isinstance(result, list):
                folder_entries = result
        elif isinstance(data, list):
            folder_entries = data

        folders = []
        for entry in (folder_entries or []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or ""
            child_fld_id = entry.get("fld_id")
            if name and child_fld_id is not None:
                folders.append((name, int(child_fld_id)))

        folders.sort(key=lambda x: x[0].lower())
        for name, child_fld_id in folders:
            item = QListWidgetItem(f"{name}")
            item.setData(Qt.ItemDataRole.UserRole, ("dir", child_fld_id, name))
            try:
                item.setIcon(lucide_icon("folder", get_accent(), 12))
            except Exception:
                pass
            self.list.addItem(item)

        self.list.blockSignals(False)

        # Restore breadcrumb display to the navigated folder, not the first child
        self.path_edit.setText(self._breadcrumb_text())
        self.selected = fld_id
        count = len(folders)
        self.status_lbl.setText(f"{count} folder{'s' if count != 1 else ''}")
        self._navigating = False

    # ── Selection / interaction ────────────────────────────────────────────────
    def _on_selection_changed(self, current, _previous):
        if self._navigating:
            return
        if current:
            data = current.data(Qt.ItemDataRole.UserRole)
            kind = data[0]
            if kind == "dir":
                _kind, fld_id, name = data
                self.selected = fld_id
                names = [n for _fid, n in self._breadcrumb] + [name]
                self.path_edit.setText(self._breadcrumb_text(names))
            elif kind == "up":
                _kind, fld_id, idx = data
                self.selected = fld_id
                names = [n for _fid, n in self._breadcrumb[:idx + 1]]
                self.path_edit.setText(self._breadcrumb_text(names))

    def _on_double_click(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        kind = data[0]
        if kind == "dir":
            _kind, fld_id, name = data
            self._navigate(fld_id, push_crumb=(fld_id, name))
        elif kind == "up":
            _kind, fld_id, idx = data
            self._navigate(fld_id, pop_to=idx)

    def _on_accept(self):
        # self.selected is either:
        #   - the folder the user explicitly single-clicked in the list, or
        #   - self.current (set in _render / _navigate) if no explicit click happened.
        # Either way it's the right answer — no need to override with self.current.
        #
        # self.selected_path mirrors the same choice as a '/'-joined path
        # string (matching path_edit's breadcrumb), for callers that need a
        # human-readable destination (e.g. UploadWorker's path-based
        # folder resolution) rather than the raw fld_id.
        self.selected_path = self.path_edit.text() or "/"
        self.accept()

    def closeEvent(self, event):
        self._cancel_token[0] = True
        if self._worker is not None:
            try:
                self._worker.done.disconnect(self._on_fetch_done)
            except RuntimeError:
                pass
        # also mark any outstanding workers for cancellation
        try:
            for w in list(_OUTSTANDING_FETCH_WORKERS):
                try:
                    w._cancel[0] = True
                except Exception:
                    pass
        except Exception:
            pass
        super().closeEvent(event)


# ── Local path dialog (used in mass-upload file picker) ──────────────────────
class LocalPathDialog(DataNodeDialog):
    """
    Lets the user type a local destination path.
    If the path doesn't exist it offers to create it.
    Returns the chosen (and possibly created) path via .chosen_path.
    """

    def __init__(self, initial_path: str = "", parent=None):
        super().__init__("Set destination path", parent, min_size=(480, 200))
        self.chosen_path = initial_path

        lay = self.content_layout
        grip_item = lay.takeAt(lay.count() - 1)

        hint = QLabel("Type the destination folder path:")
        try:
            from .theme import accent_qcolor
            hint.setStyleSheet(f"color:{accent_qcolor().name()}; font-size:12px; background:transparent;")
        except Exception:
            hint.setStyleSheet("color:#9c9484; font-size:12px; background:transparent;")
        lay.addWidget(hint)

        self.path_edit = QLineEdit(initial_path)
        self.path_edit.setPlaceholderText("e.g. /remote/my-project  or  uploads/photos")
        self.path_edit.returnPressed.connect(self._on_accept)
        lay.addWidget(self.path_edit)

        self.status_lbl = QLabel("")
        try:
            from .theme import accent_qcolor
            self.status_lbl.setStyleSheet(f"color:{accent_qcolor().name()}; font-size:11px; background:transparent;")
        except Exception:
            self.status_lbl.setStyleSheet("color:#f87171; font-size:11px; background:transparent;")
        lay.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = _grey_btn("Cancel", width=120)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = _gold_btn("Set path", width=120)
        ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        if grip_item:
            lay.addItem(grip_item)

    def _on_accept(self):
        import os
        path = self.path_edit.text().strip()
        if not path:
            self.status_lbl.setText("Path cannot be empty.")
            return
        if not path.startswith("/") or os.name == "nt":
            abs_path = os.path.abspath(path)
        else:
            abs_path = path

        if not os.path.exists(abs_path):
            reply = QMessageBox.question(
                self,
                "Create folder?",
                f'The path "{abs_path}" does not exist.\nCreate it now?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    os.makedirs(abs_path, exist_ok=True)
                except Exception as e:
                    self.status_lbl.setText(f"Could not create: {e}")
                    return
            else:
                return

        self.chosen_path = abs_path
        self.accept()