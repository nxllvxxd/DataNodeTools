"""
tabs/remote_tab.py — Server-side remote download / ingest tab for DataNodeTools.

Starts server-side remote downloads and displays transfer jobs.
"""

import os
from urllib.parse import urlparse, unquote

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QAbstractScrollArea, QSizePolicy,
)

from ..constants import HARDCODED_BASE_URL
from ..dialogs import FolderBrowserDialog
from ..workers import RemoteWorker
from ..ui.icons import lucide_icon


class RemoteTab(QWidget):
    """Starts server-side remote downloads and displays transfer jobs."""

    def __init__(self, get_api_key, on_ingest_done=None, on_share_created=None, parent=None):
        super().__init__(parent)
        self.get_api_key      = get_api_key
        self.base_url         = HARDCODED_BASE_URL
        self._workers         = []
        self._is_active       = False
        # Callbacks wired by app.py for cache invalidation
        self._on_ingest_done_cb    = on_ingest_done   # (dest_folder: str) -> None
        self._on_share_created_cb  = on_share_created # () -> None
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Ingest card lives inside a scroll area — same pattern as the Upload tab
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        self._inner_lay = QVBoxLayout(inner)
        self._inner_lay.setContentsMargins(16, 16, 16, 16)
        self._inner_lay.setSpacing(10)
        scroll.setWidget(inner)

        self._build_ingest_card(self._inner_lay)
        self._inner_lay.addStretch()

        # Put the toolbar and jobs tree inside the same scroll area so the
        # whole remote tab content scrolls as a single region.
        # Add a small separator spacing and then build the jobs UI into the
        # same inner layout used by the QScrollArea.
        sep = QWidget()
        sep.setFixedHeight(6)
        self._inner_lay.addWidget(sep)
        # toolbar
        self._build_jobs_toolbar(self._inner_lay)
        # tree
        self._build_jobs_tree(self._inner_lay)

        outer.addWidget(scroll)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh_jobs)

    def _build_ingest_card(self, parent_lay: QVBoxLayout):
        card = self._make_card()
        lay  = QVBoxLayout(card)
        lay.setSpacing(8)

        url_row = QHBoxLayout()
        url_lbl = QLabel("URL")
        url_lbl.setObjectName("field_label")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/big-file.zip")
        url_row.addWidget(url_lbl)
        url_row.addWidget(self.url_edit, 1)
        lay.addLayout(url_row)

        name_row = QHBoxLayout()
        name_lbl = QLabel("Filename")
        name_lbl.setObjectName("field_label")
        self.file_name_edit = QLineEdit()
        self.file_name_edit.setPlaceholderText("Leave blank to use the URL filename")
        name_row.addWidget(name_lbl)
        name_row.addWidget(self.file_name_edit, 1)
        lay.addLayout(name_row)

        dest_row = QHBoxLayout()
        dest_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        dest_lbl = QLabel("Folder")
        dest_lbl.setObjectName("field_label")
        self.path_edit = QLineEdit()
        self.path_edit.setText("/")
        browse_btn = QPushButton("Browse…")
        browse_btn.setObjectName("browse_btn")
        browse_btn.setFixedSize(80, 34)
        browse_btn.clicked.connect(self._browse_dest)
        dest_row.addWidget(dest_lbl)
        dest_row.addWidget(self.path_edit, 1)
        dest_row.addWidget(browse_btn)
        lay.addLayout(dest_row)

        from ..theme import notifier
        self.ingest_btn = QPushButton("  Remote ingest")
        self.ingest_btn.setObjectName("upload_btn")
        # Icon should be dark (match button text) so it remains visible on the accent background
        self.ingest_btn.setIcon(lucide_icon("download-cloud", "#111010", 15))
        self.ingest_btn.setIconSize(QSize(15, 15))
        self.ingest_btn.setMinimumHeight(40)
        self.ingest_btn.clicked.connect(self._start_ingest)
        lay.addWidget(self.ingest_btn)

        self.result_bar = QLabel("")
        self.result_bar.setObjectName("log_console")
        self.result_bar.setWordWrap(True)
        self.result_bar.hide()
        lay.addWidget(self.result_bar)
        parent_lay.addWidget(card)

    def _build_jobs_toolbar(self, parent_lay: QVBoxLayout):
        tb = QHBoxLayout()
        tb.setSpacing(4)

        from ..theme import get_accent, notifier
        self.refresh_btn = QPushButton("  Refresh Jobs")
        self.refresh_btn.setObjectName("tb_btn")
        self.refresh_btn.setIcon(lucide_icon("refresh-cw", get_accent(), 13))
        self.refresh_btn.setIconSize(QSize(13, 13))
        self.refresh_btn.clicked.connect(self.refresh_jobs)
        tb.addWidget(self.refresh_btn)

        self.cancel_btn = QPushButton("  Cancel Job")
        self.cancel_btn.setObjectName("tb_btn_danger")
        self.cancel_btn.setIcon(lucide_icon("x", "#f87171", 13))
        self.cancel_btn.setIconSize(QSize(13, 13))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_selected)
        tb.addWidget(self.cancel_btn)

        tb.addStretch()

        from ..theme import accent_qcolor, get_font
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {accent_qcolor().name()}; font-size:{int(get_font()[1])}px; background:transparent;")
        tb.addWidget(self.status_lbl)
        # parent_lay may be an outer layout or the inner scroll area layout;
        # accept either by adding the QHBoxLayout to the provided layout.
        parent_lay.addLayout(tb)
        try:
            notifier().accent_changed.connect(lambda _old, _new: self._on_accent_changed(_old, _new))
        except Exception:
            pass

    def _on_accent_changed(self, old, new):
        try:
            from ..theme import accent_qcolor
            # Keep ingest icon dark to match the button text color
            self.ingest_btn.setIcon(lucide_icon("download-cloud", "#111010", 15))
            self.refresh_btn.setIcon(lucide_icon("refresh-cw", get_accent(), 13))
            from ..theme import get_font
            self.status_lbl.setStyleSheet(f"color: {accent_qcolor().name()}; font-size:{int(get_font()[1])}px; background:transparent;")
        except Exception:
            pass

    def _build_jobs_tree(self, parent_lay: QVBoxLayout):
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["File", "Status", "Progress", "Job ID"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        hdr.resizeSection(0, 280)   # File
        hdr.resizeSection(1, 100)   # Status
        hdr.resizeSection(2, 90)    # Progress
        hdr.resizeSection(3, 160)   # Job ID
        # When the jobs tree is inside the scroll area, disable its own
        # vertical scrollbar and let the outer QScrollArea provide scrolling
        # for the whole page. Also adjust size to contents so the tree grows
        # naturally and the QScrollArea handles overflow.
        try:
            # Let the outer scroll area handle vertical scrolling; make the
            # tree expand to take available space so it isn't rendered tiny.
            self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            # Provide a reasonable minimum so the jobs area is usable even
            # when there are few or no items yet.
            self.tree.setMinimumHeight(200)
        except Exception:
            pass
        parent_lay.addWidget(self.tree, 1)

    def _make_card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        return frame

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse_dest(self):
        api_key = self.get_api_key()
        if not api_key:
            self._status("⚠ Enter your API key in Settings first.")
            return
        dlg = FolderBrowserDialog(
            api_key, self.base_url,
            self.path_edit.text().strip() or "/",
            parent=self,
        )
        dlg.setWindowTitle("Choose remote ingest destination")
        if dlg.exec():
            self.path_edit.setText(dlg.selected)

    def _start_ingest(self):
        api_key    = self.get_api_key()
        source_url = self.url_edit.text().strip()
        if not api_key:
            self._status("⚠ Enter your API key in Settings first.")
            return
        if not source_url:
            self._status("⚠ Paste a source URL first.")
            return
        file_name = self.file_name_edit.text().strip() or self._filename_from_url(source_url)
        if not file_name:
            self._status("⚠ Enter a filename for this URL.")
            return

        self.result_bar.hide()
        self.ingest_btn.setEnabled(False)
        self._status("Starting remote ingest…")
        self._run_worker(
            "ingest",
            source_url=source_url,
            file_name=file_name,
            path=self._normalized_path(),
        )

    def refresh_jobs(self):
        # datanodes.to has no job-list endpoint — the only way to check a remote
        # ingest is /api/upload/url?key=...&file_code=... which requires a file_code
        # returned from a prior ingest (datanodes does not return one on queue).
        # Polling is therefore not supported; the Files tab will reflect new files
        # once the ingest completes on the server side.
        self._status("No job list available — check Files tab for completed ingests.")

    def _cancel_selected(self):
        # datanodes.to has no documented endpoint to cancel a queued remote upload.
        # RemoteWorker raises NotImplementedError for "cancel". This button is kept
        # in the UI but will always show this notice.
        QMessageBox.information(
            self, "Not Supported",
            "datanodes.to does not provide an API endpoint to cancel remote ingests.",
        )

    # ── Worker dispatch ───────────────────────────────────────────────────────

    def _run_worker(self, op: str, **kwargs):
        w = RemoteWorker(op, self.get_api_key(), **kwargs)
        w.done.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_done(self, result: dict):
        op = result.get("op")
        if op == "ingest":
            self.ingest_btn.setEnabled(True)
            data = result.get("data") or {}
            # datanodes /api/upload/url returns {"status": 200, "msg": "WORKING"} --
            # no job ID or file name in the response. Track by the name we sent.
            original_name = self.file_name_edit.text().strip() or self._filename_from_url(
                self.url_edit.text().strip()
            )
            msg = data.get("msg", "")
            if str(data.get("status", "")).startswith("2") or msg.upper() in ("OK", "WORKING"):
                self.result_bar.setText(f"Queued: {original_name}")
                self.result_bar.show()
                self._status("✓ Remote ingest queued")
            else:
                self.result_bar.setText(f"Unexpected response: {data}")
                self.result_bar.show()
                self._status("⚠ Check response above")
            # Notify app that the destination folder cache should be invalidated
            if self._on_ingest_done_cb is not None:
                dest_folder = self._normalized_path()
                self._on_ingest_done_cb(dest_folder)
        elif op == "status":
            # datanodes status check returns {"status": 200, "file_code": "..."}
            self._populate_jobs(result.get("data"))
        elif op == "cancel":
            # datanodes has no cancel endpoint -- RemoteWorker raises NotImplementedError
            self._status("✓ Job cancelled")
            self.refresh_jobs()

    def _on_error(self, msg: str):
        self.ingest_btn.setEnabled(True)
        self._status(f"✗ {msg}")
        QMessageBox.warning(self, "Remote Ingest Error", msg)

    # ── Jobs table ────────────────────────────────────────────────────────────

    def _populate_jobs(self, data):
        # datanodes status check (/api/upload/url?file_code=...) returns
        # {"status": 200, "file_code": "abc123"} for a completed ingest,
        # or an error status if not found. There is no job list endpoint.
        self.tree.setSortingEnabled(False)
        self.tree.clear()

        if isinstance(data, dict):
            file_code = data.get("file_code") or data.get("filecode") or ""
            status_code = data.get("status", "")
            if file_code:
                status_text = "Complete" if str(status_code) == "200" else str(status_code)
                url = f"https://datanodes.to/{file_code}"
                item = QTreeWidgetItem([url, status_text, "—", file_code])
                item.setData(0, Qt.ItemDataRole.UserRole, {"file_code": file_code, "job_id": file_code})
                color = "#4ade80" if status_text == "Complete" else "#f87171"
                item.setForeground(1, QColor(color))
                self.tree.addTopLevelItem(item)

        self.tree.setSortingEnabled(True)
        count = self.tree.topLevelItemCount()
        self._status(f"{count} result{'s' if count != 1 else ''}")
        self.refresh_timer.stop()
        self._on_selection_changed()

    # _update_watched_jobs and _notify_ingest_finished removed:
    # datanodes has no job-list endpoint and does not return a job ID on ingest,
    # so there is nothing to poll or match against.

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_selection_changed(self):
        meta = self._selected_meta()
        self.cancel_btn.setEnabled(bool(meta and meta.get("job_id")))

    def _selected_meta(self) -> dict | None:
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_active(self, active: bool):
        self._is_active = active
        if active:
            self.refresh_jobs()
        else:
            self.refresh_timer.stop()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _normalized_path(self) -> str:
        path = self.path_edit.text().strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        return path.rstrip("/") + "/"

    @staticmethod
    def _filename_from_url(source_url: str) -> str:
        parsed = urlparse(source_url)
        return unquote(os.path.basename(parsed.path.rstrip("/")))

    def _status(self, msg: str):
        self.status_lbl.setText(msg)