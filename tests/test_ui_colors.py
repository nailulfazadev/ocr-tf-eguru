"""Teks di baris berwarna harus HITAM (terbaca di tema gelap); saat disorot -> teks PUTIH di latar biru."""
import os, sys, pathlib, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from app.models import RowResult, SheetRow, Verdict
from app.ui import MainWindow


def lum(c):  # luminans 0..255
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


@pytest.fixture(scope="module")
def win():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    pal = QPalette()                                  # meniru mode gelap macOS: teks bawaan PUTIH
    for role, col in ((QPalette.Window, "#262626"), (QPalette.Base, "#1e1e1e"), (QPalette.Text, "#ffffff"),
                      (QPalette.WindowText, "#ffffff"), (QPalette.ButtonText, "#ffffff"), (QPalette.Button, "#3a3a3a"),
                      (QPalette.AlternateBase, "#2a2a2a")):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    w = MainWindow(); w.resize(1100, 700); w.show(); app.processEvents()
    return app, w


def cell_pixels(table, row, col):
    table.viewport().repaint()
    rect = table.visualItemRect(table.item(row, col))
    img = table.viewport().grab().toImage().copy(rect)
    return [QColor(img.pixel(x, y)) for x in range(img.width()) for y in range(img.height())]


@pytest.mark.parametrize("codes,bg", [(set(), "#d9f2d9"), ({"NOMINAL_MISMATCH"}, "#f8d7da"), ({"DATE_MISSING"}, "#fff3cd")])
def test_text_is_dark_on_light_rows_and_white_when_selected(win, codes, bg):
    app, w = win
    page = w.pages["Event 1"]
    page.table.setRowCount(0)
    v = Verdict(); [v.add(c, "alasan uji yang cukup panjang") for c in codes]
    page.on_result(RowResult(SheetRow(row=2, nama="Budi Santoso", nominal_raw="20.000"), v, None, "n"))
    app.processEvents()

    px = cell_pixels(page.table, 0, 1)                # kolom "Nama"
    assert min(lum(c) for c in px) < 70, "teks harus hitam, bukan putih"            # ada piksel teks gelap
    assert sum(1 for c in px if lum(c) > 200) > len(px) * 0.5                          # latar tetap terang

    page.table.selectRow(0); page.table.setFocus(); app.processEvents()
    px = cell_pixels(page.table, 0, 1)
    blue = sum(1 for c in px if c.blue() > c.red() + 60)
    assert blue > len(px) * 0.5, "baris terpilih harus berlatar biru"
    assert max(lum(c) for c in px) > 235, "teks baris terpilih harus putih (kontras)"
    page.table.clearSelection()
