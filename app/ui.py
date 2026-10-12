"""Antarmuka desktop. Banyak event dalam satu aplikasi; tiap event punya proses sendiri dan bisa berjalan bersamaan."""
from __future__ import annotations
import os
import threading
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from .config import APP_NAME, Config, Store, gemini_params, parse_gid, parse_spreadsheet_id
from .gemini import GeminiReader
from .models import INVALID, MANUAL, VALID, RowResult, SheetRow
from .pipeline import Engine, apply_manual, load_review_queue
from .sheets import (DriveClient, SheetsClient, extract_file_ids, get_credentials, install_key, key_status)

COLORS = {VALID: "#d9f2d9", INVALID: "#f8d7da", MANUAL: "#fff3cd"}
TEXT_DARK = QColor("#111111")

# Baris berwarna terang -> teks HITAM (terbaca di mode gelap maupun terang); saat disorot -> biru dengan teks PUTIH.
STYLE = """
QTableWidget, QListWidget { selection-background-color: #2563eb; selection-color: #ffffff; }
"""


def paint(item, status: str) -> None:
    item.setBackground(QColor(COLORS[status]))
    item.setForeground(TEXT_DARK)


class Task(QThread):
    """Jalankan fungsi di thread lain agar jendela tidak membeku."""
    ok = Signal(object)
    fail = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.ok.emit(self.fn())
        except Exception as e:
            self.fail.emit(f"{type(e).__name__}: {e}")


def run_task(owner: QWidget, fn, ok, fail=None) -> None:
    t = Task(fn, owner)
    t.ok.connect(ok)
    t.fail.connect(fail or (lambda m: QMessageBox.warning(owner, "Gagal", m)))
    owner._tasks.append(t)
    t.finished.connect(lambda: owner._tasks.remove(t) if t in owner._tasks else None)
    t.start()


class AppContext:
    """Dipakai bersama semua event: penyimpanan pengaturan, login Google, dan SATU pembaca Gemini (laju dibagi)."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store.load()
        self.creds = None
        self.drive: DriveClient | None = None
        self._gemini: GeminiReader | None = None
        self._sig = None
        self._lock = threading.Lock()

    def get_creds(self):
        with self._lock:
            if self.creds is None:
                self.creds = get_credentials()
                self.drive = DriveClient(self.creds)
            return self.creds

    def reset_creds(self) -> None:
        with self._lock:
            self.creds, self.drive = None, None

    def get_gemini(self, cfg: Config) -> GeminiReader | None:
        key = cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        if not cfg.use_gemini or not key:
            return None
        rpm, conc = gemini_params(cfg)
        sig = (key, cfg.gemini_model, rpm, conc)
        with self._lock:
            if self._gemini is None or self._sig != sig:
                self._gemini, self._sig = GeminiReader(key, cfg.gemini_model, rpm, conc), sig
            return self._gemini


class EngineThread(QThread):
    result = Signal(object)
    log = Signal(str)
    progress = Signal(int, int)

    def __init__(self, cfg, sheets, drive, gemini, parent=None):
        super().__init__(parent)
        self.engine = Engine(cfg, sheets, drive, on_result=self.result.emit, on_log=self.log.emit,
                             on_progress=self.progress.emit, gemini=gemini)

    def run(self):
        try:
            self.engine.run()
        except Exception as e:
            self.log.emit(f"GALAT: {type(e).__name__}: {e}")


class ImageView(QScrollArea):
    def __init__(self):
        super().__init__()
        self.label = QLabel(alignment=Qt.AlignCenter)
        self.setWidget(self.label)
        self.setWidgetResizable(False)
        self._pix: QPixmap | None = None
        self._zoom, self._fit = 1.0, True

    def set_image(self, data: bytes | None, msg: str = ""):
        self._pix = None
        if data:
            pm = QPixmap()
            if pm.loadFromData(data):
                self._pix = pm
        if self._pix is None:
            self.label.setPixmap(QPixmap())
            self.label.setText(msg or "Gambar tidak bisa ditampilkan")
            self.label.adjustSize()
            return
        self._fit = True
        self._render()

    def zoom(self, f: float):
        self._fit = False
        self._zoom = max(0.1, min(5.0, self._zoom * f))
        self._render()

    def fit(self):
        self._fit = True
        self._render()

    def _render(self):
        if not self._pix:
            return
        if self._fit:
            pm = self._pix.scaledToWidth(max(200, self.viewport().width() - 4), Qt.SmoothTransformation)
            self._zoom = pm.width() / self._pix.width()
        else:
            pm = self._pix.scaled(self._pix.size() * self._zoom, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.label.setPixmap(pm)
        self.label.adjustSize()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self._render()


# =====================================================================================
class EventPage(QWidget):
    """Satu event: Proses | Review manual | Pengaturan event. Berjalan mandiri dari event lain."""
    running_changed = Signal(str, bool)

    FILTERS = [("manual", "Cek manual (perlu diputuskan)"), ("invalid", "Tidak valid (periksa ulang)"),
               ("valid", "Valid (periksa sampel)"), ("all", "Semua yang sudah diproses")]

    def __init__(self, ctx: AppContext, name: str, parent=None):
        super().__init__(parent)
        self.ctx, self.name = ctx, name
        self.cfg = ctx.store.config_for(name)
        self.sheets: SheetsClient | None = None
        self.eng_thread: EngineThread | None = None
        self._tasks: list[Task] = []
        self.counts = {VALID: 0, INVALID: 0, MANUAL: 0}
        self.queue: list[SheetRow] = []
        self.current: SheetRow | None = None
        self._review_loaded = False

        tabs = QTabWidget()
        tabs.addTab(self._build_run_tab(), "Proses")
        tabs.addTab(self._build_review_tab(), "Review manual")
        tabs.addTab(self._build_settings_tab(), "Pengaturan event")
        self.tabs = tabs
        tabs.currentChanged.connect(self._tab_changed)
        self.lbl_title = QLabel(); self.lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; padding: 4px;")
        self.set_name(name)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.lbl_title); lay.addWidget(tabs)
        if not self.cfg.spreadsheet_id:
            tabs.setCurrentIndex(2)

    def set_name(self, name: str):
        self.name = self.cfg.name = name
        self.lbl_title.setText(f"Event: {name}")

    def is_running(self) -> bool:
        return self.eng_thread is not None and self.eng_thread.isRunning()

    def reset_connection(self):
        self.sheets = None
        self.lbl_conn.setText("Belum terhubung")

    def _ready(self) -> bool:
        if self.sheets and self.ctx.drive:
            return True
        QMessageBox.information(self, "Belum terhubung",
                                "Buka tab 'Pengaturan event', isi URL spreadsheet, lalu klik 'Hubungkan'.\n"
                                "(Pastikan kunci Google sudah dipasang di 'Pengaturan umum'.)")
        self.tabs.setCurrentIndex(2)
        return False

    # ---------------- tab Proses ----------------
    def _build_run_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.btn_start = QPushButton("\u25B6  Mulai / Lanjutkan Cek"); self.btn_start.setMinimumHeight(40)
        self.btn_pause = QPushButton("Jeda"); self.btn_stop = QPushButton("Berhenti")
        self.btn_pause.setEnabled(False); self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.stop_run)
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            row.addWidget(b)
        lay.addLayout(row)
        hint = QLabel("<b>Jeda</b> menahan proses (klik Lanjut untuk meneruskan). <b>Berhenti</b> atau tutup aplikasi kapan saja: "
                      "hasil yang selesai sudah tersimpan di sheet. Klik <b>Mulai / Lanjutkan Cek</b> lagi, dan baris yang sudah "
                      "ada hasilnya dilewati, jadi proses lanjut dari sisanya. Untuk mengulang dengan aturan baru, ubah "
                      "'Baris yang sudah ada hasilnya' di Pengaturan event.")
        hint.setWordWrap(True); hint.setTextFormat(Qt.RichText); lay.addWidget(hint)
        self.progress = QProgressBar(); lay.addWidget(self.progress)
        self.lbl_counts = QLabel(); lay.addWidget(self.lbl_counts)
        self._update_counts()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Baris", "Nama", "Nominal sheet", "Nominal bukti", "Status", "Alasan"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(130)
        sp = QSplitter(Qt.Vertical); sp.addWidget(self.table); sp.addWidget(self.log)
        sp.setStretchFactor(0, 4)
        lay.addWidget(sp, 1)
        return w

    def _update_counts(self):
        c = self.counts
        self.lbl_counts.setText(f"\u2705 Valid: {c[VALID]}    \u274C Tidak valid: {c[INVALID]}    \u26A0 Cek manual: {c[MANUAL]}")

    def _log(self, msg: str):
        self.log.appendPlainText(msg)

    def _save(self) -> Config:
        """Simpan pengaturan event dari formulir, lalu kembalikan konfigurasi efektif (event + umum terbaru)."""
        self._collect_cfg()
        self.ctx.store.update_event(self.cfg)
        self.ctx.store.save()
        self.cfg = self.ctx.store.config_for(self.name)
        return self.cfg

    def start_run(self):
        if not self._ready():
            return
        cfg = self._save()
        if cfg.reprocess == "auto" and QMessageBox.question(
                self, "Ulangi semua hasil otomatis?",
                "Semua baris dengan hasil otomatis (VALID, TIDAK VALID, CEK MANUAL) akan DIPROSES ULANG dan hasil lamanya "
                "ditimpa. Keputusan manual tidak diubah.\n\nLanjutkan?") != QMessageBox.Yes:
            return
        try:
            reader = self.ctx.get_gemini(cfg)
        except Exception as e:
            reader = None
            self._log(f"Gemini tidak bisa dipakai: {e}")
        if reader is not None:
            reader.off = False                       # pengguna sengaja menjalankan lagi -> coba kembali
        self.counts = {VALID: 0, INVALID: 0, MANUAL: 0}; self._update_counts()
        self.table.setRowCount(0); self.progress.setValue(0)
        t = EngineThread(cfg, self.sheets, self.ctx.drive, reader, self)
        t.result.connect(self.on_result); t.log.connect(self._log); t.progress.connect(self.on_progress)
        t.finished.connect(self.on_finished)
        self.eng_thread = t
        self.btn_start.setEnabled(False); self.btn_pause.setEnabled(True); self.btn_stop.setEnabled(True)
        t.start()
        self.running_changed.emit(self.name, True)

    def toggle_pause(self):
        e = self.eng_thread.engine
        if self.btn_pause.text() == "Jeda":
            e.pause(); self.btn_pause.setText("Lanjut"); self._log("Dijeda.")
        else:
            e.resume(); self.btn_pause.setText("Jeda"); self._log("Dilanjutkan.")

    def stop_run(self):
        if self.eng_thread:
            self._log("Menghentikan... (menunggu baris yang sedang diproses)")
            self.eng_thread.engine.stop()

    def on_finished(self):
        self.btn_start.setEnabled(True); self.btn_pause.setEnabled(False); self.btn_stop.setEnabled(False)
        self.btn_pause.setText("Jeda")
        self._review_loaded = False                  # hasil baru -> muat ulang saat tab Review dibuka
        self.running_changed.emit(self.name, False)

    def on_progress(self, done: int, total: int):
        self.progress.setMaximum(max(total, 1)); self.progress.setValue(done)

    def on_result(self, r: RowResult):
        self.counts[r.status] += 1; self._update_counts()
        i = self.table.rowCount(); self.table.insertRow(i)
        prim = r.ext.primary.value if r.ext and r.ext.primary else ""
        vals = [r.row.row, r.row.nama, r.row.nominal_raw, f"{prim:,}".replace(",", ".") if prim else "-",
                r.status, "; ".join(r.verdict.reasons)]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v)); paint(it, r.status)
            self.table.setItem(i, c, it)
        self.table.scrollToBottom()

    # ---------------- tab Review ----------------
    def _build_review_tab(self) -> QWidget:
        w = QWidget(); outer = QVBoxLayout(w)
        help_ = QLabel("Tab ini untuk <b>memutuskan baris yang aplikasi ragu</b> (status CEK MANUAL), atau "
                       "<b>memeriksa ulang</b> hasil otomatis lewat filter di kiri. Baris yang aplikasi yakin tidak perlu "
                       "Anda sentuh, jadi daftar CEK MANUAL biasanya pendek.")
        help_.setWordWrap(True); help_.setTextFormat(Qt.RichText)
        outer.addWidget(help_)
        lay = QHBoxLayout(); outer.addLayout(lay, 1)

        left = QVBoxLayout()
        self.cb_filter = QComboBox()
        for key, label in self.FILTERS:
            self.cb_filter.addItem(label, key)
        self.cb_filter.activated.connect(lambda _i: self.load_queue())
        self.btn_load = QPushButton("Muat ulang"); self.btn_load.clicked.connect(self.load_queue)
        self.lbl_queue = QLabel("")
        self.list = QListWidget(); self.list.currentRowChanged.connect(self.show_item)
        for wdg in (self.cb_filter, self.btn_load, self.lbl_queue):
            left.addWidget(wdg)
        left.addWidget(self.list, 1)
        lw = QWidget(); lw.setLayout(left); lw.setMaximumWidth(290)

        self.view = ImageView()
        zr = QHBoxLayout()
        for txt, fn in (("\uFF0B", lambda: self.view.zoom(1.25)), ("\uFF0D", lambda: self.view.zoom(0.8)),
                        ("Pas layar", self.view.fit)):
            b_ = QPushButton(txt); b_.clicked.connect(fn); zr.addWidget(b_)
        zr.addStretch(1)
        mid = QVBoxLayout(); mid.addLayout(zr); mid.addWidget(self.view, 1)
        mw = QWidget(); mw.setLayout(mid)

        right = QVBoxLayout()
        self.detail = QPlainTextEdit(); self.detail.setReadOnly(True)
        self.btn_ok = QPushButton("\u2705 Valid  (V)"); self.btn_no = QPushButton("\u274C Tolak  (T)")
        self.btn_skip = QPushButton("Lewati  (S)")
        for b_ in (self.btn_ok, self.btn_no):
            b_.setMinimumHeight(40)
        self.btn_ok.clicked.connect(lambda: self.decide(True))
        self.btn_no.clicked.connect(lambda: self.decide(False))
        self.btn_skip.clicked.connect(self.next_item)
        right.addWidget(self.detail, 1)
        for b_ in (self.btn_ok, self.btn_no, self.btn_skip):
            right.addWidget(b_)
        rw = QWidget(); rw.setLayout(right); rw.setMaximumWidth(330)

        lay.addWidget(lw); lay.addWidget(mw, 1); lay.addWidget(rw)
        QShortcut(QKeySequence("V"), w, activated=lambda: self.decide(True))
        QShortcut(QKeySequence("T"), w, activated=lambda: self.decide(False))
        QShortcut(QKeySequence("S"), w, activated=self.next_item)
        return w

    def _tab_changed(self, i: int):
        if i == 1 and self.sheets and self.ctx.drive and not self._review_loaded:
            self.load_queue()

    def load_queue(self):
        if not self._ready():
            return
        self._collect_cfg()
        mode = self.cb_filter.currentData()
        cfg, sheets = self.cfg, self.sheets
        self.btn_load.setEnabled(False); self.lbl_queue.setText("Memuat...")
        run_task(self, lambda: load_review_queue(sheets, cfg, mode),
                 lambda rows: self._queue_loaded(rows, mode),
                 lambda m: (self.btn_load.setEnabled(True), self.lbl_queue.setText(""),
                            QMessageBox.warning(self, "Gagal", m)))

    def _queue_loaded(self, rows, mode: str):
        self.btn_load.setEnabled(True)
        self._review_loaded = True
        self.queue = rows
        self.list.clear()
        short = {VALID: "valid", INVALID: "tidak valid", MANUAL: "manual"}
        for r in rows:
            st = VALID if r.note.startswith(VALID) else INVALID if r.note.startswith(INVALID) else MANUAL
            it = QListWidgetItem(f"Baris {r.row} \u00B7 {short[st]} \u00B7 {r.nama[:20]}")
            paint(it, st)
            self.list.addItem(it)
        self.lbl_queue.setText(f"{len(rows)} baris")
        if rows:
            self.list.setCurrentRow(0)
        elif mode == "manual":
            self.detail.setPlainText("Tidak ada baris CEK MANUAL.\n\nArtinya semua baris yang sudah diproses diputuskan "
                                     "otomatis dan aplikasi tidak ragu. Untuk memeriksa ulang hasil otomatis, pilih "
                                     "filter 'Tidak valid' atau 'Valid' di atas.")
        else:
            self.detail.setPlainText("Tidak ada baris untuk filter ini.")

    def show_item(self, i: int):
        if i < 0 or i >= len(self.queue):
            self.current = None
            return
        r = self.current = self.queue[i]
        self.detail.setPlainText(
            f"Baris: {r.row}\nNama: {r.nama}\nNominal di sheet: {r.nominal_raw}\nRekening tujuan: {r.rekening_raw}\n"
            f"Waktu isi form: {r.timestamp_raw}\n\nHasil pemeriksaan otomatis:\n{r.note}")
        self.view.set_image(None, "Memuat gambar...")
        fid = (extract_file_ids(r.link_raw) or [""])[0]
        if not fid:
            self.view.set_image(None, "Tidak ada link bukti"); return
        drive = self.ctx.drive

        def done(res, row=r):
            if self.current is row:
                data, mime = res
                self.view.set_image(data if mime.startswith("image/") else None,
                                    f"File bukan gambar ({mime}). Buka manual: {row.link_raw}")
        run_task(self, lambda: drive.fetch(fid), done, lambda m: self.view.set_image(None, m))

    def next_item(self):
        i = self.list.currentRow()
        if i + 1 < self.list.count():
            self.list.setCurrentRow(i + 1)

    def decide(self, ok: bool):
        r = self.current
        if r is None:
            return
        reason = ""
        if not ok:
            reason, accepted = QInputDialog.getText(self, "Tolak", "Alasan penolakan:", text="bukti tidak sesuai")
            if not accepted:
                return
        row_idx = self.list.currentRow()
        officer = self.cfg.officer or "petugas"
        cfg, sheets = self.cfg, self.sheets

        def done(_):
            self.queue.remove(r)
            self.list.takeItem(row_idx)
            self.current = None
            if self.list.count():
                self.list.setCurrentRow(min(row_idx, self.list.count() - 1))
            else:
                self.detail.setPlainText("Antrean selesai.")
        run_task(self, lambda: apply_manual(sheets, cfg, r, ok, officer, reason), done)

    # ---------------- tab Pengaturan event ----------------
    def _build_settings_tab(self) -> QWidget:
        c = self.cfg
        w = QWidget(); lay = QVBoxLayout(w)
        g1 = QGroupBox("Spreadsheet event ini"); f1 = QFormLayout(g1)
        self.e_url = QLineEdit(c.spreadsheet_url)
        self.btn_connect = QPushButton("Hubungkan"); self.btn_connect.clicked.connect(self.connect_google)
        self.lbl_conn = QLabel("Belum terhubung")
        self.cb_tab = QComboBox()
        if c.sheet_title:
            self.cb_tab.addItem(c.sheet_title, c.sheet_id)
        f1.addRow("URL spreadsheet", self.e_url)
        f1.addRow(self.btn_connect, self.lbl_conn); f1.addRow("Tab / sheet", self.cb_tab)
        lay.addWidget(g1)

        g2 = QGroupBox("Kolom (huruf)"); grid = QGridLayout(g2)
        self.col_edits = {}
        fields = [("link", "Link bukti"), ("nominal", "Nominal transfer"), ("rekening", "Bank/rekening tujuan"),
                  ("nama", "Nama"), ("timestamp", "Timestamp"), ("check", "Ceklis (hasil)"),
                  ("note", "Catatan (hasil)"), ("ref", "No. referensi (hasil)")]
        for i, (k, label) in enumerate(fields):
            e = QLineEdit(getattr(c, "col_" + k)); e.setMaximumWidth(50); e.setAlignment(Qt.AlignCenter)
            self.col_edits[k] = e
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2); grid.addWidget(e, i // 2, (i % 2) * 2 + 1)
        lay.addWidget(g2)

        g3 = QGroupBox("Aturan event ini"); f3 = QFormLayout(g3)
        self.sp_minamt = QSpinBox(); self.sp_minamt.setRange(0, 2_000_000_000); self.sp_minamt.setSingleStep(1000)
        self.sp_minamt.setPrefix("Rp "); self.sp_minamt.setGroupSeparatorShown(True)
        self.sp_minamt.setSpecialValueText("Tanpa minimal"); self.sp_minamt.setValue(int(c.min_amount or 0))
        self.ck_min = QCheckBox("Pakai tanggal minimal")
        self.ck_min.setToolTip("Bukti transfer bertanggal sebelum ini dianggap tidak sah (tanggal itu sendiri diterima)")
        self.dt_min = QDateEdit(calendarPopup=True); self.dt_min.setDisplayFormat("dd/MM/yyyy")
        d = QDate.fromString(c.min_date, "yyyy-MM-dd") if c.min_date else QDate.currentDate()
        self.dt_min.setDate(d if d.isValid() else QDate.currentDate())
        self.ck_min.setChecked(bool(c.min_date)); self.dt_min.setEnabled(bool(c.min_date))
        self.ck_min.toggled.connect(self.dt_min.setEnabled)
        row_min = QHBoxLayout(); row_min.addWidget(self.ck_min); row_min.addWidget(self.dt_min); row_min.addStretch(1)
        self.sp_first = QSpinBox(); self.sp_first.setRange(1, 1_000_000); self.sp_first.setValue(c.first_row)
        self.sp_from = QSpinBox(); self.sp_from.setRange(0, 1_000_000); self.sp_from.setValue(c.row_from)
        self.sp_to = QSpinBox(); self.sp_to.setRange(0, 1_000_000); self.sp_to.setValue(c.row_to)
        self.sp_tol = QSpinBox(); self.sp_tol.setRange(0, 1_000_000); self.sp_tol.setSingleStep(500)
        self.sp_tol.setPrefix("\u00B1 Rp "); self.sp_tol.setGroupSeparatorShown(True)
        self.sp_tol.setSpecialValueText("Harus persis sama"); self.sp_tol.setValue(int(c.amount_tolerance))
        self.sp_tol.setToolTip("Selisih nominal bukti dan sheet (lebih atau kurang) yang masih dianggap valid, "
                               "mis. biaya admin bank terbesar. Default 6.500.")
        self.cb_reprocess = QComboBox()
        for key, label in (("none", "Lanjutkan saja (lewati baris yang sudah ada hasilnya)"),
                           ("manual", "Ulangi juga baris CEK MANUAL"),
                           ("auto", "Ulangi SEMUA hasil otomatis (keputusan manual tidak diubah)")):
            self.cb_reprocess.addItem(label, key)
        self.cb_reprocess.setCurrentIndex(max(0, self.cb_reprocess.findData(c.reprocess)))
        f3.addRow("Toleransi selisih nominal", self.sp_tol)
        f3.addRow("Donasi minimal", self.sp_minamt)
        f3.addRow("Tanggal minimal bukti", row_min)
        f3.addRow("Baris data pertama", self.sp_first)
        f3.addRow("Batasi dari baris (0 = semua)", self.sp_from); f3.addRow("sampai baris (0 = semua)", self.sp_to)
        f3.addRow("Baris yang sudah ada hasilnya", self.cb_reprocess)
        lay.addWidget(g3)

        btn = QPushButton("Simpan pengaturan event")
        btn.clicked.connect(lambda: (self._save(), self.tabs.setCurrentIndex(0)))
        lay.addWidget(btn); lay.addStretch(1)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setWidget(w)
        return sc

    def _collect_cfg(self):
        c = self.cfg
        c.spreadsheet_url = self.e_url.text().strip()
        c.spreadsheet_id = parse_spreadsheet_id(c.spreadsheet_url) if c.spreadsheet_url else ""
        if self.cb_tab.currentIndex() >= 0:
            c.sheet_title = self.cb_tab.currentText(); c.sheet_id = int(self.cb_tab.currentData() or 0)
        for k, e in self.col_edits.items():
            setattr(c, "col_" + k, (e.text().strip().upper() or getattr(c, "col_" + k)))
        c.first_row, c.row_from, c.row_to = self.sp_first.value(), self.sp_from.value(), self.sp_to.value()
        c.reprocess = self.cb_reprocess.currentData() or "none"
        c.amount_tolerance = self.sp_tol.value()
        c.min_amount = self.sp_minamt.value()
        c.min_date = self.dt_min.date().toString("yyyy-MM-dd") if self.ck_min.isChecked() else ""
        if self.sheets:                                   # tab bisa diganti setelah terhubung
            self.sheets.title, self.sheets.sheet_id = c.sheet_title, c.sheet_id

    def connect_google(self):
        self._collect_cfg()
        if not self.cfg.spreadsheet_id:
            QMessageBox.information(self, "URL kosong", "Tempel URL spreadsheet dulu."); return
        self.btn_connect.setEnabled(False); self.lbl_conn.setText("Menghubungkan (login Google bila perlu)...")
        url, sid, ctx = self.cfg.spreadsheet_url, self.cfg.spreadsheet_id, self.ctx

        def work():
            creds = ctx.get_creds()
            sh = SheetsClient(creds, sid)
            return sh, sh.list_tabs()
        run_task(self, work, lambda r: self._connected(r, url), self._connect_failed)

    def _connect_failed(self, msg: str):
        self.btn_connect.setEnabled(True); self.lbl_conn.setText("Gagal terhubung")
        QMessageBox.warning(self, "Gagal terhubung", msg)

    def _connected(self, res, url: str):
        sh, tabs = res
        self.btn_connect.setEnabled(True)
        self.cb_tab.clear()
        gid = parse_gid(url)
        pick = 0
        for i, (title, sid, _rows) in enumerate(tabs):
            self.cb_tab.addItem(title, sid)
            if (gid is not None and sid == gid) or (gid is None and title == self.cfg.sheet_title):
                pick = i
        self.cb_tab.setCurrentIndex(pick)
        sh.title, sh.sheet_id = self.cb_tab.currentText(), int(self.cb_tab.currentData())
        self.sheets = sh
        self.lbl_conn.setText("\u2705 Terhubung")


# =====================================================================================
class GlobalPage(QWidget):
    """Pengaturan yang berlaku untuk SEMUA event: kunci Google, petugas, Gemini, kecepatan."""
    key_changed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._tasks: list[Task] = []
        c = ctx.store.config_for("")
        lay = QVBoxLayout(self)
        lay.addWidget(self._title("Pengaturan umum (berlaku untuk semua event)"))

        gk = QGroupBox("Kunci akses Google"); fk = QHBoxLayout(gk)
        self.lbl_key = QLabel(key_status()); self.lbl_key.setWordWrap(True)
        btn_key = QPushButton("Pilih file kunci (.json)..."); btn_key.clicked.connect(self.pick_key)
        fk.addWidget(self.lbl_key, 1); fk.addWidget(btn_key)
        lay.addWidget(gk)

        g1 = QGroupBox("Petugas & kecepatan baca"); f1 = QFormLayout(g1)
        self.e_officer = QLineEdit(c.officer); self.e_officer.setPlaceholderText("nama Anda (tercatat di kolom catatan)")
        self.sp_workers = QSpinBox(); self.sp_workers.setRange(1, 16); self.sp_workers.setValue(c.workers)
        self.sp_block = QSpinBox(); self.sp_block.setRange(5, 500); self.sp_block.setValue(c.block_size)
        f1.addRow("Nama petugas", self.e_officer)
        f1.addRow("Proses paralel per event", self.sp_workers); f1.addRow("Ukuran blok klaim", self.sp_block)
        lay.addWidget(g1)

        g4 = QGroupBox("Gemini (dipakai bersama semua event)"); f4 = QFormLayout(g4)
        self.ck_gem = QCheckBox("Pakai Gemini"); self.ck_gem.setChecked(c.use_gemini)
        self.e_key = QLineEdit(c.gemini_api_key); self.e_key.setEchoMode(QLineEdit.Password)
        self.e_model = QLineEdit(c.gemini_model)
        self.cb_mode = QComboBox()
        self.cb_mode.addItem("Gratis (pelan, hemat kuota)", False); self.cb_mode.addItem("Berbayar (cepat)", True)
        self.cb_mode.setCurrentIndex(1 if c.gemini_paid else 0)
        self.sp_rpm = QSpinBox(); self.sp_rpm.setRange(0, 10000); self.sp_rpm.setSpecialValueText("Otomatis")
        self.sp_rpm.setValue(c.gemini_rpm)
        self.sp_conc = QSpinBox(); self.sp_conc.setRange(0, 64); self.sp_conc.setSpecialValueText("Otomatis")
        self.sp_conc.setValue(c.gemini_concurrency)
        hint = QLabel("Mode berbayar: billing proyek Google Cloud harus aktif. Otomatis = gratis 13 request/menit, 1 panggilan; "
                      "berbayar 120 request/menit, 6 panggilan bersamaan. Isi manual sesuai batas akun Anda "
                      "(lihat di AI Studio > Rate limits). Bila terkena batas per menit, aplikasi menunggu lalu mencoba lagi.")
        hint.setWordWrap(True)
        btn_gem = QPushButton("Tes Gemini (key + model)"); btn_gem.clicked.connect(self.test_gemini)
        f4.addRow(self.ck_gem); f4.addRow("API key", self.e_key); f4.addRow("Model", self.e_model)
        f4.addRow("Mode akun", self.cb_mode); f4.addRow("Batas request/menit", self.sp_rpm)
        f4.addRow("Panggilan bersamaan", self.sp_conc); f4.addRow(hint); f4.addRow(btn_gem)
        lay.addWidget(g4)

        row = QHBoxLayout()
        btn = QPushButton("Simpan pengaturan umum"); btn.clicked.connect(self.save)
        self.lbl_saved = QLabel(""); row.addWidget(btn); row.addWidget(self.lbl_saved, 1)
        lay.addLayout(row); lay.addStretch(1)

    @staticmethod
    def _title(text: str) -> QLabel:
        l = QLabel(text); l.setStyleSheet("font-size: 16px; font-weight: bold; padding: 4px;"); return l

    def save(self):
        g = self.ctx.store.globals
        g.update(officer=self.e_officer.text().strip(), workers=self.sp_workers.value(),
                 block_size=self.sp_block.value(), use_gemini=self.ck_gem.isChecked(),
                 gemini_api_key=self.e_key.text().strip(), gemini_model=self.e_model.text().strip(),
                 gemini_paid=bool(self.cb_mode.currentData()), gemini_rpm=self.sp_rpm.value(),
                 gemini_concurrency=self.sp_conc.value())
        self.ctx.store.save()
        self.lbl_saved.setText("\u2705 Tersimpan")

    def pick_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Pilih file kunci Google", str(Path.home() / "Downloads"),
                                              "File JSON (*.json)")
        if not path:
            return
        try:
            kind, info = install_key(path)
        except ValueError as e:
            QMessageBox.warning(self, "File tidak sesuai", str(e)); return
        self.lbl_key.setText(key_status())
        self.key_changed.emit()                      # kunci berganti -> semua event perlu 'Hubungkan' ulang
        if kind == "service_account":
            QMessageBox.information(self, "Kunci terpasang",
                f"Pastikan spreadsheet DAN folder upload Google Form tiap event sudah dibagikan ke:\n\n{info}\n\n"
                "(spreadsheet: Editor, folder upload: Viewer). Lalu klik 'Hubungkan' di tiap event.")
        else:
            QMessageBox.information(self, "Kunci terpasang", "Klik 'Hubungkan' di event untuk login dengan akun Google Anda.")

    def test_gemini(self):
        key = self.e_key.text().strip() or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            QMessageBox.information(self, "API key kosong", "Isi API key Gemini dulu."); return
        model = self.e_model.text().strip()

        def work():
            r = GeminiReader(key, model)
            out = {"ok": False, "msg": "", "models": []}
            try:
                out["msg"] = r.ping(); out["ok"] = True
            except Exception as e:
                out["msg"] = f"{type(e).__name__}: {str(e)[:300]}"
            try:
                out["models"] = r.available_models()
            except Exception:
                pass
            return out

        def show(o):
            lst = "\n".join("  - " + m for m in o["models"]) or "  (daftar tidak bisa diambil)"
            if o["ok"]:
                QMessageBox.information(self, "Gemini OK", f"\u2705 Model '{model}' berfungsi.\n\n"
                                        f"Model Flash yang tersedia untuk key Anda:\n{lst}")
            else:
                QMessageBox.warning(self, "Gemini gagal", f"\u274C Model '{model}':\n{o['msg']}\n\n"
                                    f"Model Flash yang tersedia untuk key Anda (isi salah satunya di kolom Model):\n{lst}")
        run_task(self, work, show)


# =====================================================================================
class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 820)
        self.setStyleSheet(STYLE)
        self.ctx = ctx or AppContext()
        store = self.ctx.store
        self.pages: dict[str, EventPage] = {}

        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Event</b>"))
        self.event_list = QListWidget()
        left.addWidget(self.event_list, 1)
        grid = QGridLayout()
        for i, (txt, fn) in enumerate((("+ Event baru", self.new_event), ("Duplikat", self.duplicate_event),
                                       ("Ganti nama", self.rename_event), ("Hapus", self.delete_event))):
            b = QPushButton(txt); b.clicked.connect(fn); grid.addWidget(b, i // 2, i % 2)
        left.addLayout(grid)
        self.btn_global = QPushButton("\u2699  Pengaturan umum"); self.btn_global.clicked.connect(self.show_global)
        left.addWidget(self.btn_global)
        lw = QWidget(); lw.setLayout(left); lw.setMinimumWidth(210); lw.setMaximumWidth(280)

        self.stack = QStackedWidget()
        self.global_page = GlobalPage(self.ctx)
        self.global_page.key_changed.connect(self._key_changed)
        self.stack.addWidget(self.global_page)
        for n in store.names():
            self._add_page(n)

        sp = QSplitter(); sp.addWidget(lw); sp.addWidget(self.stack); sp.setStretchFactor(1, 1)
        self.setCentralWidget(sp)
        self.event_list.currentItemChanged.connect(self._on_select)
        self._select(store.last_event)
        if "\u26A0" in key_status():
            self.show_global()

    # ---- daftar event ----
    def _item(self, name: str) -> QListWidgetItem | None:
        for i in range(self.event_list.count()):
            it = self.event_list.item(i)
            if it.data(Qt.UserRole) == name:
                return it
        return None

    def _label(self, name: str) -> str:
        return ("\u25B6 " if name in self.pages and self.pages[name].is_running() else "") + name

    def _add_page(self, name: str) -> EventPage:
        page = EventPage(self.ctx, name)
        page.running_changed.connect(lambda n, _r: self._refresh_item(n))
        self.pages[name] = page
        self.stack.addWidget(page)
        it = QListWidgetItem(name); it.setData(Qt.UserRole, name)
        self.event_list.addItem(it)
        return page

    def _refresh_item(self, name: str):
        it = self._item(name)
        if it:
            it.setText(self._label(name))

    def _select(self, name: str):
        it = self._item(name)
        if it:
            self.event_list.setCurrentItem(it)

    def _current_name(self) -> str | None:
        it = self.event_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        name = cur.data(Qt.UserRole)
        self.stack.setCurrentWidget(self.pages[name])
        self.ctx.store.last_event = name

    def show_global(self):
        self.event_list.setCurrentRow(-1)
        self.stack.setCurrentWidget(self.global_page)

    def _key_changed(self):
        self.ctx.reset_creds()
        for p in self.pages.values():
            p.reset_connection()

    # ---- kelola event ----
    def new_event(self):
        name = self.ctx.store.add()
        self.ctx.store.save()
        self._add_page(name); self._select(name)

    def duplicate_event(self):
        cur = self._current_name()
        if not cur:
            return
        self.pages[cur]._save()
        name = self.ctx.store.add(f"{cur} (salinan)", copy_from=cur)
        self.ctx.store.save()
        self._add_page(name); self._select(name)

    def rename_event(self):
        cur = self._current_name()
        if not cur:
            return
        new, ok = QInputDialog.getText(self, "Ganti nama event", "Nama baru:", text=cur)
        if not ok or not new.strip():
            return
        try:
            new = self.ctx.store.rename(cur, new)
        except ValueError as e:
            QMessageBox.warning(self, "Gagal", str(e)); return
        self.ctx.store.save()
        page = self.pages.pop(cur); self.pages[new] = page; page.set_name(new)
        it = self._item(cur); it.setData(Qt.UserRole, new); it.setText(self._label(new))

    def delete_event(self):
        cur = self._current_name()
        if not cur:
            return
        if self.pages[cur].is_running():
            QMessageBox.information(self, "Sedang berjalan", "Hentikan prosesnya dulu sebelum menghapus event."); return
        if len(self.pages) <= 1:
            QMessageBox.information(self, "Tidak bisa", "Minimal harus ada satu event."); return
        if QMessageBox.question(self, "Hapus event", f"Hapus event '{cur}' dari aplikasi?\n"
                                "(Data di spreadsheet TIDAK ikut terhapus.)") != QMessageBox.Yes:
            return
        page = self.pages.pop(cur)
        self.stack.removeWidget(page); page.deleteLater()
        self.event_list.takeItem(self.event_list.row(self._item(cur)))
        self.ctx.store.delete(cur); self.ctx.store.save()
        self._select(self.ctx.store.last_event)

    def closeEvent(self, e):
        running = [p for p in self.pages.values() if p.is_running()]
        if running:
            names = ", ".join(p.name for p in running)
            if QMessageBox.question(self, "Proses masih berjalan",
                                    f"Masih berjalan: {names}.\nHentikan dan keluar? (baris yang selesai sudah tersimpan di sheet)"
                                    ) != QMessageBox.Yes:
                e.ignore(); return
            for p in running:
                p.eng_thread.engine.stop()
            for p in running:
                p.eng_thread.wait(8000)
        self.ctx.store.save()
        e.accept()


def run() -> None:
    import sys
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())
