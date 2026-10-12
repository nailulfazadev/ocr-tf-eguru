import io, sys, pathlib, copy
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

from app.config import Config
from app.models import SheetRow, Extracted, Amount, VALID, INVALID, MANUAL
from app import pipeline
from app.pipeline import Engine, is_eligible, load_manual_queue, apply_manual
from app.gemini import GeminiQuotaError

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def receipt(lines, dark=False):
    f = ImageFont.truetype(FONT, 34)
    im = Image.new("RGB", (900, 70 * len(lines) + 80), (20, 20, 20) if dark else "white")
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        d.text((40, 40 + 70 * i), ln, font=f, fill="white" if dark else "black")
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

LINES = ["Transaksi Berhasil", "15 Sep 2026, 21:30:12 WIB", "No. Ref 123456789012",
         "Bank Tujuan BRI", "Nama Tujuan GURU ERA DIGITAL", "No. Tujuan 127001000238564",
         "Nominal Rp20.000", "Biaya Admin Rp0"]

class FakeSheets:
    def __init__(self, rows): self.rows = {r.row: r for r in rows}; self.writes = []; self.highlighted = set()
    def highlight_cells(self, cells): self.highlighted |= set(cells)
    def list_tabs(self): return [("T", 0, 100)]
    def ensure_checkbox(self, cfg, n): pass
    def read_rows(self, cfg): return [copy.copy(r) for r in self.rows.values()]
    def read_cells(self, a1s):
        return [self.rows[int(a1[1:])].note if a1[0] == "K" else "" for a1 in a1s]
    def batch_write(self, pairs):
        for a1, v in pairs:
            r = self.rows[int(a1[1:])]; col = a1[0]
            if col == "K": r.note = v
            elif col == "I": r.check = bool(v)
            elif col == "L": r.ref = v
        self.writes.append(pairs)

class FakeDrive:
    def __init__(self, imgs): self.imgs = imgs
    def fetch(self, fid): return self.imgs[fid], "image/png"

def link(fid): return f"https://drive.google.com/open?id={fid}"
REK = "BRI 127001000238564 an GURU ERA DIGITAL"
def mk(n, fid, nominal="20.000", **kw):
    return SheetRow(row=n, nama=f"N{n}", nominal_raw=nominal, rekening_raw=REK,
                    link_raw=link(fid), timestamp_raw="15/09/2026 21:32:26", **kw)

def cfg(use_gemini=False):
    c = Config(); c.officer = "Budi"; c.use_gemini = use_gemini; c.workers = 2; c.first_row = 2
    c.amount_tolerance = 0      # tes lama mengharapkan nominal persis; toleransi diuji terpisah
    return c

def run(rows, imgs, c, gemini=None):
    s = FakeSheets(rows); logs = []
    e = Engine(c, s, FakeDrive(imgs), on_log=logs.append)
    if gemini: e.gemini = gemini
    e.run(); return s, e, logs

def test_end_to_end_skip_rules_and_statuses():
    good = receipt(LINES)
    other_ref = receipt([l.replace("123456789012", "999888777666") for l in LINES], dark=True)  # mode gelap
    stale = f"[diproses|Ani|{(datetime.now() - timedelta(hours=2)):%Y-%m-%dT%H:%M:%S}]"
    fresh = f"[diproses|Ani|{datetime.now():%Y-%m-%dT%H:%M:%S}]"
    rows = [
        mk(2, "A" * 25),                       # valid
        mk(3, "B" * 25, nominal="25.000"),     # nominal salah -> TIDAK VALID (Gemini dimatikan pengguna)
        mk(4, "A" * 25, check=True),           # sudah dicentang -> skip
        mk(5, "A" * 25, note="VALID | sudah"), # sudah ada catatan -> skip
        mk(6, "A" * 25),                       # ref sama dengan baris 2 -> duplikat
        mk(7, "C" * 25, note=stale),           # klaim kedaluwarsa -> diproses
        mk(8, "C" * 25, note=fresh),           # klaim masih segar -> skip
    ]
    imgs = {"A" * 25: good, "B" * 25: good, "C" * 25: other_ref}
    s, e, logs = run(rows, imgs, cfg())
    R = s.rows
    assert R[2].check and R[2].note.startswith(VALID), R[2].note
    assert not R[3].check and R[3].note.startswith(INVALID) and "25.000" in R[3].note, R[3].note
    assert R[4].note == "" and R[4].check            # tidak disentuh
    assert R[5].note == "VALID | sudah"
    assert R[6].note.startswith(INVALID) and "baris 2" in R[6].note, R[6].note
    assert R[7].check and R[7].note.startswith(VALID), R[7].note   # gambar mode gelap terbaca
    assert R[8].note == fresh
    assert R[2].ref == "123456789012|2026-09-15T21:30:12"    # referensi + tanggal + jam

def test_ocr_mismatch_defers_to_gemini_and_quota():
    good = receipt(LINES)
    good2 = receipt([l.replace("123456789012", "555444333222") for l in LINES])
    rows = [mk(2, "A" * 25, nominal="25.000"), mk(3, "B" * 25, nominal="25.000")]

    class G:
        n = 0
        def extract(self, img, mime):
            G.n += 1
            if G.n == 2: raise GeminiQuotaError("429")
            e = Extracted(source="gemini", conf=100, success=True, dt=datetime(2026, 9, 15, 21, 30),
                          text="BRI 127001000238564 GURU ERA DIGITAL", ref="123456789012")
            e.amounts = [Amount(20000, True, "primary")]
            return e
    c = cfg(use_gemini=True); c.workers = 1
    s, e, logs = run(rows, {"A" * 25: good, "B" * 25: good2}, c, gemini=G())
    assert s.rows[2].note.startswith(INVALID) and "gemini" in s.rows[2].note, s.rows[2].note   # dikonfirmasi Gemini
    assert s.rows[3].note.startswith(MANUAL) and "kuota Gemini" in s.rows[3].note, s.rows[3].note

def test_ocr_mismatch_without_gemini_key_is_manual_not_invalid():
    good = receipt(LINES)
    c = cfg(use_gemini=True); c.workers = 1          # Gemini aktif tapi tidak ada API key
    s, e, logs = run([mk(2, "A" * 25, nominal="25.000")], {"A" * 25: good}, c)
    assert s.rows[2].note.startswith(MANUAL), s.rows[2].note

def test_manual_review_flow():
    c = cfg(); s = FakeSheets([mk(2, "A" * 25, note="CEK MANUAL | x"), mk(3, "A" * 25, note="CEK MANUAL | y"),
                               mk(4, "A" * 25, note="VALID | z")])
    q = load_manual_queue(s, c); assert [r.row for r in q] == [2, 3]
    apply_manual(s, c, q[0], True, "Budi")
    apply_manual(s, c, q[1], False, "Budi", "bukti diedit")
    assert s.rows[2].check and s.rows[2].note.startswith("VALID (manual)")
    assert not s.rows[3].check and s.rows[3].note.startswith("TIDAK VALID (manual) | bukti diedit")
    assert load_manual_queue(s, c) == []

def test_claim_conflict_not_overwritten():
    c = cfg(); s = FakeSheets([mk(2, "A" * 25)])
    e = Engine(c, s, FakeDrive({"A" * 25: receipt(LINES)}), on_log=lambda m: None)
    e._marker = "[diproses|Budi|X]"
    s.rows[2].note = "[diproses|Ani|Y]"              # orang lain menimpa klaim
    res = e._analyse(s.rows[2]); e._flush([res])
    assert s.rows[2].note == "[diproses|Ani|Y]" and not s.rows[2].check

def test_rejected_row_does_not_poison_ref():
    good = receipt(LINES)
    c = cfg(); c.workers = 1
    rows = [mk(2, "A" * 25, nominal="25.000"), mk(3, "A" * 25)]   # baris 2 ditolak lebih dulu, baris 3 sah
    s, e, logs = run(rows, {"A" * 25: good}, c)
    assert s.rows[2].note.startswith(INVALID) and s.rows[2].ref == ""
    assert s.rows[3].check and s.rows[3].note.startswith(VALID), s.rows[3].note


def test_min_date_end_to_end():
    good = receipt(LINES)                               # bukti bertanggal 15 Sep 2026
    c = cfg(); c.workers = 1; c.min_date = "2026-09-16"  # Gemini dimatikan -> OCR boleh menolak
    s, e, logs = run([mk(2, "A" * 25)], {"A" * 25: good}, c)
    assert s.rows[2].note.startswith(INVALID) and "batas minimal 16/09/2026" in s.rows[2].note, s.rows[2].note
    c.min_date = "2026-09-15"
    s, e, logs = run([mk(2, "A" * 25)], {"A" * 25: good}, c)
    assert s.rows[2].check, s.rows[2].note


def test_technical_errors_are_not_written_and_not_looped():
    class BrokenDrive:
        def fetch(self, fid): raise ConnectionError("jaringan putus")
    s = FakeSheets([mk(2, "A" * 25), mk(3, "A" * 25)]); logs = []
    c = cfg(); c.workers = 1
    e = Engine(c, s, BrokenDrive(), on_log=logs.append); e.run()          # harus berhenti, tidak berputar
    assert s.rows[2].note == "" and s.rows[3].note == "" and not s.rows[2].check, (s.rows[2].note, logs)
    assert any("TIDAK ditulis" in l for l in logs)

def test_old_galat_notes_are_retried_and_hidden_from_review():
    old = "CEK MANUAL | galat: TesseractNotFoundError: tesseract is not installed | -/FAZA 10/10 13:37"
    assert is_eligible(mk(2, "A" * 25, note=old), 30)
    assert not is_eligible(mk(2, "A" * 25, note="CEK MANUAL | tanggal bukti kosong"), 30)
    s = FakeSheets([mk(2, "A" * 25, note=old), mk(3, "A" * 25, note="CEK MANUAL | x")])
    assert [r.row for r in load_manual_queue(s, cfg())] == [3]
    s, e, logs = run([mk(2, "A" * 25, note=old)], {"A" * 25: receipt(LINES)}, cfg())
    assert s.rows[2].check and s.rows[2].note.startswith(VALID), s.rows[2].note

def test_missing_tesseract_aborts_without_touching_rows(monkeypatch):
    import pytesseract
    def boom(img): raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pipeline.ocr, "run_ocr", boom)
    s = FakeSheets([mk(2, "A" * 25), mk(3, "A" * 25)]); logs = []
    c = cfg(); c.workers = 1
    Engine(c, s, FakeDrive({"A" * 25: receipt(LINES)}), on_log=logs.append).run()
    assert s.rows[2].note == "" and s.rows[3].note == "", (s.rows[2].note, s.rows[3].note)
    assert any("brew install tesseract" in l for l in logs)


def _no_tesseract(monkeypatch):
    import pytesseract
    def boom(): raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pipeline.ocr, "check_ready", boom)

class _G:
    def __init__(self, quota_at=None): self.n = 0; self.quota_at = quota_at
    def extract(self, img, mime):
        self.n += 1
        if self.quota_at and self.n >= self.quota_at: raise GeminiQuotaError("429")
        e = Extracted(source="gemini", conf=100, success=True, dt=datetime(2026, 9, 15, 21, 30),
                      text="BRI 127001000238564 GURU ERA DIGITAL", ref=f"REF{self.n:09d}")
        e.amounts = [Amount(20000, True, "primary")]
        return e

def test_gemini_only_mode_without_tesseract(monkeypatch):
    _no_tesseract(monkeypatch)
    c = cfg(use_gemini=True); c.workers = 1
    rows = [mk(2, "A" * 25), mk(3, "A" * 25, nominal="25.000")]
    s, e, logs = run(rows, {"A" * 25: receipt(LINES)}, c, gemini=_G())
    assert e.ocr_ok is False and any("MODE GEMINI SAJA" in l for l in logs)
    assert s.rows[2].check and s.rows[2].note.startswith(VALID) and "gemini" in s.rows[2].note, s.rows[2].note
    assert s.rows[3].note.startswith(INVALID), s.rows[3].note

def test_gemini_only_quota_stops_and_leaves_rows_untouched(monkeypatch):
    _no_tesseract(monkeypatch)
    c = cfg(use_gemini=True); c.workers = 1
    rows = [mk(2, "A" * 25), mk(3, "A" * 25), mk(4, "A" * 25)]
    s, e, logs = run(rows, {"A" * 25: receipt(LINES)}, c, gemini=_G(quota_at=2))
    assert s.rows[2].check                                       # selesai sebelum kuota habis
    assert s.rows[3].note == "" and s.rows[4].note == ""         # tidak terkunci, bisa dilanjutkan besok

def test_no_tesseract_and_no_gemini_aborts_with_help(monkeypatch):
    _no_tesseract(monkeypatch)
    c = cfg(use_gemini=False)
    s, e, logs = run([mk(2, "A" * 25)], {"A" * 25: receipt(LINES)}, c)
    assert s.rows[2].note == "" and any("Tesseract belum terpasang" in l for l in logs)


def test_min_amount_end_to_end_and_no_gemini_call():
    class G:
        def extract(self, *a): raise AssertionError("Gemini tidak boleh dipanggil untuk pelanggaran minimal")
    c = cfg(use_gemini=True); c.workers = 1; c.min_amount = 25000
    s, e, logs = run([mk(2, "A" * 25)], {"A" * 25: receipt(LINES)}, c, gemini=G())
    assert s.rows[2].note.startswith(INVALID) and "di bawah minimal Rp25.000" in s.rows[2].note, s.rows[2].note
    assert not s.rows[2].check

def test_review_filters_and_recheck_of_auto_results():
    from app.pipeline import load_review_queue
    c = cfg()
    s = FakeSheets([mk(2, "A" * 25, note="VALID | x", check=True), mk(3, "A" * 25, note="TIDAK VALID | y"),
                    mk(4, "A" * 25, note="CEK MANUAL | z"), mk(5, "A" * 25, note="[diproses|Ani|2026-10-10T10:00:00]"),
                    mk(6, "A" * 25, note="CEK MANUAL | galat: x"), mk(7, "A" * 25)])
    ids = lambda m: [r.row for r in load_review_queue(s, c, m)]
    assert ids("manual") == [4] and ids("invalid") == [3] and ids("valid") == [2] and ids("all") == [2, 3, 4]
    # koreksi hasil otomatis: tolak baris yang tadinya VALID, setujui yang tadinya TIDAK VALID
    apply_manual(s, c, s.rows[2], False, "Budi", "bukti ternyata diedit")
    apply_manual(s, c, s.rows[3], True, "Budi")
    assert not s.rows[2].check and s.rows[2].note.startswith("TIDAK VALID (manual)")
    assert s.rows[3].check and s.rows[3].note.startswith("VALID (manual)")


def test_retry_manual_option_reprocesses_auto_manual_but_not_human_decisions():
    old = "CEK MANUAL | kejanggalan: Tanggal transaksi menunjukkan tahun 2026 (masa depan). | gemini/FAZA 10/10 20:46"
    human = "TIDAK VALID (manual) | bukti diedit | oleh Budi 10/10 21:00"
    good = receipt(LINES)
    c = cfg(); c.workers = 1
    s, e, logs = run([mk(2, "A" * 25, note=old)], {"A" * 25: good}, c)
    assert s.rows[2].note == old                                     # default: tidak diulang
    c.reprocess = "manual"
    s, e, logs = run([mk(2, "A" * 25, note=old), mk(3, "B" * 25, note=human)],
                     {"A" * 25: good, "B" * 25: good}, c)
    assert s.rows[2].check and s.rows[2].note.startswith(VALID), s.rows[2].note
    assert s.rows[3].note == human                                   # keputusan manusia tidak disentuh

def test_retry_manual_does_not_loop_on_rows_that_stay_manual():
    c = cfg(); c.workers = 1; c.reprocess = "manual"
    bad = SheetRow(row=2, nama="x", nominal_raw="20.000", rekening_raw="", link_raw=link("A" * 25),
                   timestamp_raw="15/09/2026 21:32:26", note="CEK MANUAL | x")   # rekening sheet kosong -> tetap manual
    s, e, logs = run([bad], {"A" * 25: receipt(LINES)}, c)               # harus selesai, bukan berputar
    assert s.rows[2].note.startswith(MANUAL) and "rekening tujuan di sheet" in s.rows[2].note


def test_model_unavailable_in_gemini_only_mode_stops_cleanly(monkeypatch):
    from app.gemini import GeminiModelError
    _no_tesseract(monkeypatch)
    class G:
        def extract(self, *a): raise GeminiModelError("404 NOT_FOUND no longer available to new users")
    c = cfg(use_gemini=True); c.workers = 1
    s, e, logs = run([mk(2, "A" * 25), mk(3, "A" * 25)], {"A" * 25: receipt(LINES)}, c, gemini=G())
    assert s.rows[2].note == "" and s.rows[3].note == ""
    assert any("tidak tersedia" in l and "Tes Gemini" in l for l in logs)

def test_model_unavailable_in_fallback_mode_goes_manual_and_stops_calling():
    from app.gemini import GeminiModelError
    calls = []
    class G:
        def extract(self, *a): calls.append(1); raise GeminiModelError("404 NOT_FOUND")
    c = cfg(use_gemini=True); c.workers = 1
    rows = [mk(2, "A" * 25, nominal="25.000"), mk(3, "B" * 25, nominal="25.000")]
    img2 = receipt([l.replace("123456789012", "555444333222") for l in LINES])
    s, e, logs = run(rows, {"A" * 25: receipt(LINES), "B" * 25: img2}, c, gemini=G())
    assert s.rows[2].note.startswith(MANUAL) and "model Gemini tidak tersedia" in s.rows[2].note, s.rows[2].note
    assert len(calls) == 1                                           # tidak mencoba lagi untuk baris berikutnya


def test_shared_gemini_quota_stops_other_events_too():
    """Satu pembaca Gemini dipakai bersama: bila kuota habis di event A, event B tidak ikut membuang request."""
    calls = []
    class Shared:
        off = False
        def extract(self, *a):
            calls.append(1); raise GeminiQuotaError("429 PerDay")
    g = Shared()
    c = cfg(use_gemini=True); c.workers = 1
    img_a = receipt(LINES); img_b = receipt([l.replace("123456789012", "555444333222") for l in LINES])
    sA, eA, _ = run([mk(2, "A" * 25, nominal="25.000")], {"A" * 25: img_a}, c, gemini=g)
    assert g.off is True and len(calls) == 1
    sB, eB, logsB = run([mk(2, "B" * 25, nominal="25.000"), mk(3, "B" * 25, nominal="25.000")],
                        {"B" * 25: img_b}, c, gemini=g)
    assert len(calls) == 1                                           # event B tidak memanggil Gemini lagi
    assert sB.rows[2].note.startswith(MANUAL) and "kuota Gemini habis" in sB.rows[2].note


def test_gemini_only_mode_uses_gemini_concurrency_as_worker_count(monkeypatch):
    _no_tesseract(monkeypatch)
    class G:
        concurrency = 6; off = False
        def extract(self, *a):
            return _G().extract()
    c = cfg(use_gemini=True); c.workers = 2
    s = FakeSheets([mk(2, "A" * 25)]); e = Engine(c, s, FakeDrive({"A" * 25: receipt(LINES)}), on_log=lambda m: None, gemini=G())
    e.run()
    assert e._workers == 6                                           # mode Gemini saja: mengikuti kecepatan Gemini
