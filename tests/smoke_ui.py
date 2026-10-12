"""Uji antarmuka tanpa layar: beberapa event berjalan BERSAMAAN, review, dan pengelolaan event."""
import os, sys, pathlib, tempfile, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication
from test_pipeline import FakeSheets, FakeDrive, mk, receipt, LINES
from app.ui import MainWindow
from app.models import VALID, INVALID, MANUAL

app = QApplication([])
from PySide6.QtWidgets import QMessageBox
def _boom(*a, **k): raise AssertionError("dialog peringatan muncul: " + " | ".join(str(x) for x in a[1:3]))
QMessageBox.warning = _boom; QMessageBox.information = _boom     # dialog modal akan memblokir uji tanpa layar
w = MainWindow()

def wait(cond, secs=25):
    t0 = time.time()
    while not cond() and time.time() - t0 < secs:
        app.processEvents(); time.sleep(0.03)
    for _ in range(10): app.processEvents()
    assert cond(), "timeout"

img = receipt(LINES)
img2 = receipt([l.replace("123456789012", "555444333222") for l in LINES])
w.ctx.drive = FakeDrive({"A" * 25: img, "B" * 25: img2})

# pengaturan umum
g = w.global_page; g.e_officer.setText("Budi"); g.ck_gem.setChecked(False); g.sp_workers.setValue(2); g.save()

# ---- dua event, masing-masing spreadsheet sendiri ----
w.new_event(); assert w.ctx.store.names() == ["Event 1", "Event 2"]
p1, p2 = w.pages["Event 1"], w.pages["Event 2"]
p1.sheets = FakeSheets([mk(2, "A" * 25), mk(3, "B" * 25, nominal="25.000"), mk(4, "A" * 25, note="CEK MANUAL | uji")])
p2.sheets = FakeSheets([mk(2, "B" * 25), mk(3, "A" * 25, nominal="20.000")])
p2.sp_minamt.setValue(25000)                       # aturan event 2 berbeda dari event 1
assert p1.sp_tol.value() == 6500 and p2.sp_tol.value() == 6500      # toleransi default 6.500 di formulir
p1.sp_tol.setValue(0)                              # event 1: nominal harus persis (supaya uji lama tetap bermakna)
assert p1.cb_reprocess.currentData() == "none"

p1.start_run(); p2.start_run()                     # BERSAMAAN
assert p1.is_running() and p2.is_running()
assert w._item("Event 1").text().startswith("\u25B6") and w._item("Event 2").text().startswith("\u25B6")
print("menunggu event berjalan..."); wait(lambda: not p1.is_running() and not p2.is_running())
print("event1", p1.counts, "event2", p2.counts)
assert p1.counts[VALID] == 1 and p1.counts[INVALID] == 1          # baris 4 sudah punya catatan -> dilewati
assert p2.counts[INVALID] == 2 and p2.counts[VALID] == 0          # minimal 25.000 hanya berlaku di event 2
assert "di bawah minimal Rp25.000" in p2.sheets.rows[2].note
assert p2.ctx.store.config_for("Event 1").amount_tolerance == 0 and p2.ctx.store.config_for("Event 2").amount_tolerance == 6500
assert not w._item("Event 1").text().startswith("\u25B6")

# ---- review di event 1 ----
p1.cb_filter.setCurrentIndex(0); p1.load_queue()
wait(lambda: p1.list.count() == 1)
wait(lambda: p1.view._pix is not None)
p1.decide(True)
wait(lambda: p1.list.count() == 0)
assert p1.sheets.rows[4].check and p1.sheets.rows[4].note.startswith("VALID (manual)")

# ---- kelola event: duplikat, ganti nama (pengaturan ikut), hapus, simpan ke disk ----
w._select("Event 2"); w.duplicate_event()
names = w.ctx.store.names(); print(names)
assert names == ["Event 1", "Event 2", "Event 2 (salinan)"]
assert w.ctx.store.config_for("Event 2 (salinan)").min_amount == 25000          # salinan membawa aturan
w.ctx.store.rename("Event 2 (salinan)", "Rapat Akbar"); w.pages["Rapat Akbar"] = w.pages.pop("Event 2 (salinan)")
w.pages["Rapat Akbar"].set_name("Rapat Akbar")
assert w.ctx.store.config_for("Rapat Akbar").officer == "Budi"                 # pengaturan umum dipakai semua event
w.ctx.store.save()
from app.config import Store
again = Store.load(); print("disk:", again.names(), again.globals["officer"])
assert again.names() == ["Event 1", "Event 2", "Rapat Akbar"] and again.config_for("Event 1").min_amount == 0
print("SMOKE OK")
