import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from datetime import datetime, timedelta, date
import pytest

from app.models import SheetRow, VALID, INVALID, MANUAL
from app.parser import extract
from app.validator import Context, RefRegistry, validate, parse_rekening, same_moment
from app.pipeline import is_eligible, encode_ref, decode_ref, LIVE_MARKERS, Engine
from test_logic import BRIMO, row, CTX
from test_pipeline import (FakeSheets, FakeDrive, mk, receipt, LINES, cfg, run, link, REK)


def ctx(**kw):
    return Context(other_accounts=CTX.other_accounts, **kw)

def text_with_nominal(nom, label="Nominal"):
    return BRIMO.replace("Nominal Rp20.000\n", f"{label} {nom}\n").replace("Total Transaksi Rp22.500", "Total Transaksi Rp0")

# ---------------- 1) toleransi nominal ----------------
@pytest.mark.parametrize("sheet,proof,ok", [("50.000", "52.500", True), ("50.000", "56.500", True),     # tepat di batas 6.500
                                            ("50.000", "56.501", False), ("50.000", "47.500", True),      # kurang juga boleh
                                            ("50.000", "43.500", True), ("50.000", "43.499", False)])
def test_tolerance_both_directions_inclusive(sheet, proof, ok):
    ext = extract(text_with_nominal("Rp" + proof), 90)
    v = validate(row(sheet), ext, ctx(tolerance=6500))
    if ok:
        assert v.status == VALID, v.reasons
    else:
        assert "NOMINAL_MISMATCH_OCR" in v.codes or "NOMINAL_AMBIGUOUS" in v.codes

def test_tolerance_is_noted_for_audit_but_exact_match_has_no_note():
    v = validate(row("50.000"), extract(text_with_nominal("Rp52.500"), 90), ctx(tolerance=6500))
    assert v.status == VALID and v.infos == ["selisih +Rp2.500 dalam toleransi Rp6.500"]
    assert validate(row("20.000"), extract(BRIMO, 90), ctx(tolerance=6500)).infos == []

def test_tolerance_zero_means_exact():
    assert "NOMINAL_MISMATCH_OCR" in validate(row("50.000"), extract(text_with_nominal("Rp52.500"), 90), ctx(tolerance=0)).codes

def test_tolerance_does_not_hide_sheet_typo_or_repaired_digits():
    ext = extract(text_with_nominal("Rp3.000"), 90)
    assert "NOMINAL_SHEET_TYPO" in validate(row("3"), ext, ctx(tolerance=6500)).codes   # sheet "3" padahal 3.000
    ext = extract(text_with_nominal("Rp52.5O0"), 90)                                      # OCR ragu pada angka
    assert "NOMINAL_REPAIRED" in validate(row("50.000"), ext, ctx(tolerance=6500)).codes

def test_min_amount_uses_the_smaller_of_sheet_and_proof():
    c = ctx(tolerance=6500, min_amount=10000)
    assert validate(row("10.000"), extract(text_with_nominal("Rp12.500"), 90), c).status == VALID      # +biaya admin
    v = validate(row("10.000"), extract(text_with_nominal("Rp8.500"), 90), c)                          # yang masuk kurang
    assert v.status == INVALID and "di bawah minimal" in v.reasons[0]

def test_total_with_fee_over_tolerance_still_manual():
    ext = extract(BRIMO.replace("Nominal Rp20.000\n", ""), 90).__class__  # placeholder agar impor terpakai
    txt = "Transaksi Berhasil\n15 Sep 2026 21:30:12\nNo. Ref 123456789012\nNama Tujuan GURU ERA DIGITAL\nTotal Transaksi Rp32.500\n"
    v = validate(row("20.000"), extract(txt, 90), ctx(tolerance=6500))
    assert v.status == MANUAL and "NOMINAL_AMBIGUOUS" in v.codes

# ---------------- 2) duplikat = referensi + tanggal + jam (sampai detik) ----------------
D = datetime(2026, 9, 15, 21, 30, 12)

def test_same_moment_rules():
    assert same_moment(D, True, D, True)
    assert not same_moment(D, True, D.replace(second=13), True)          # beda 1 detik -> bukan kembar
    assert not same_moment(D, True, D + timedelta(days=1), True)         # beda tanggal
    assert same_moment(D, False, D.replace(hour=1), True)                # salah satu tanpa jam: tanggal sama cukup
    assert same_moment(None, False, D, True)                             # tanggal tak diketahui -> tak bisa dibedakan

def test_registry_same_ref_but_different_time_is_not_duplicate():
    r = RefRegistry()
    assert r.claim("REF123", 2, D, True) is None
    assert r.claim("REF123", 3, D.replace(second=59), True) is None      # referensi sama, detik beda -> sah
    assert r.claim("REF123", 4, D, True) == 2                            # semuanya sama -> kembar dari baris 2
    assert r.find("REF123", 9, D, True) == 2 and r.find("REF123", 2, D, True) is None

def test_ref_encoding_roundtrip_and_backward_compat():
    assert decode_ref(encode_ref("ABC1234", D, True)) == ("ABC1234", D, True)
    assert decode_ref(encode_ref("ABC1234", datetime(2026, 9, 15), False)) == ("ABC1234", datetime(2026, 9, 15), False)
    assert decode_ref("OLD1234567") == ("OLD1234567", None, False)       # nilai lama (tanpa tanggal) tetap terbaca

def test_duplicate_flow_marks_both_cells_yellow_and_only_when_all_three_match():
    good = receipt(LINES)
    other_time = receipt([l.replace("21:30:12", "21:30:59") for l in LINES])          # ref sama, detik beda
    c = cfg(); c.workers = 1
    rows = [mk(2, "A" * 25), mk(3, "B" * 25), mk(4, "A" * 25)]
    s, e, logs = run(rows, {"A" * 25: good, "B" * 25: other_time}, c)
    assert s.rows[2].check and s.rows[3].check                                       # detik beda -> bukan kembar
    assert s.rows[4].note.startswith(INVALID) and "baris 2" in s.rows[4].note
    assert s.highlighted == {(2, c.col_link), (4, c.col_link)}                       # sel bukti kedua baris -> kuning
    assert s.rows[3].ref.endswith("2026-09-15T21:30:59")

# ---------------- 1b) toleransi end-to-end ----------------
def test_tolerance_end_to_end_and_note():
    c = cfg(); c.workers = 1; c.amount_tolerance = 6500
    lines = [l.replace("Rp20.000", "Rp22.500") for l in LINES]
    s, e, _ = run([mk(2, "A" * 25, nominal="20.000")], {"A" * 25: receipt(lines)}, c)
    assert s.rows[2].check and "selisih +Rp2.500 dalam toleransi Rp6.500" in s.rows[2].note, s.rows[2].note

# ---------------- 3) lanjut / ulangi ----------------
AUTO_VALID = "VALID | Rp20.000 | ref X | ocr/Budi 10/10 14:02"
AUTO_INVALID = "TIDAK VALID | nominal bukti Rp52.500 \u2260 sheet Rp50.000"
HUMAN = "VALID (manual) | oleh Budi 10/10 21:00 | sebelumnya: CEK MANUAL"

def el(note="", check=False, **kw):
    return is_eligible(mk(2, "A" * 25, note=note, check=check), 30, **kw)

def test_default_resume_skips_everything_that_has_a_result():
    for n, ck in ((AUTO_VALID, True), (AUTO_INVALID, False), ("CEK MANUAL | x", False), (HUMAN, True)):
        assert not el(n, ck)
    assert el("")                                                                      # belum diproses -> lanjut di sini

def test_reprocess_modes():
    assert el("CEK MANUAL | x", reprocess="manual") and not el(AUTO_INVALID, reprocess="manual")
    for n, ck in ((AUTO_VALID, True), (AUTO_INVALID, False), ("CEK MANUAL | x", False)):
        assert el(n, ck, reprocess="auto")
    assert not el(HUMAN, True, reprocess="auto")                                       # keputusan manual tak pernah diulang
    assert not el("TIDAK VALID (manual) | bukti diedit | oleh Budi", False, reprocess="auto")
    assert not el("", True, reprocess="auto")                                          # dicentang manual tanpa catatan: aman

def test_own_stale_claim_recoverable_immediately_but_not_when_live_or_foreign():
    fresh = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
    own = f"[diproses|Budi#abc123.9f2e|{fresh}]"
    assert not el(own)                                                                  # tanpa own_id: masih dianggap klaim aktif
    assert el(own, own_id="abc123")                                                     # sisa proses komputer ini yang macet -> ambil lagi
    assert not el(own, own_id="abc123", live=frozenset({own}))                          # sedang dikerjakan engine lain di aplikasi ini
    assert not el(f"[diproses|Ani#zzz999.1111|{fresh}]", own_id="abc123")               # klaim komputer lain tetap dihormati

def test_stop_then_resume_continues_without_redoing_finished_rows():
    good = receipt(LINES)
    c = cfg(); c.workers = 1
    rows = [mk(n, "A" * 25) for n in range(2, 6)]
    s = FakeSheets(rows); imgs = {"A" * 25: good}
    class StopAfterTwo(FakeDrive):
        n = 0
        def fetch(self, fid):
            StopAfterTwo.n += 1
            if StopAfterTwo.n == 3: e1.stop()
            return super().fetch(fid)
    e1 = Engine(c, s, StopAfterTwo(imgs), on_log=lambda m: None); e1.run()
    done = [r for r in s.rows.values() if r.note]
    assert 0 < len(done) < 4
    left = [r.row for r in s.rows.values() if not r.note]
    assert all("[diproses" not in r.note for r in s.rows.values())                      # klaim sisa dilepas saat berhenti
    calls = []
    class Spy(FakeDrive):
        def fetch(self, fid): calls.append(1); return super().fetch(fid)
    logs = []
    Engine(c, s, Spy(imgs), on_log=logs.append).run()
    assert len(calls) == len(left)                                                      # hanya sisanya yang diproses
    assert any("sudah punya hasil dan dilewati" in l for l in logs)

def test_reprocess_auto_rechecks_old_wrong_rejection_with_new_tolerance():
    old = "TIDAK VALID | Rp22.500 | 15/09/2026 21:30 | ref 123456789012 | nominal bukti Rp22.500 \u2260 sheet Rp20.000"
    lines = [l.replace("Rp20.000", "Rp22.500") for l in LINES]
    c = cfg(); c.workers = 1; c.amount_tolerance = 6500
    s, e, _ = run([mk(2, "A" * 25, note=old)], {"A" * 25: receipt(lines)}, c)
    assert s.rows[2].note == old                                                         # mode lanjutkan: tidak disentuh
    c.reprocess = "auto"
    s, e, _ = run([mk(2, "A" * 25, note=old)], {"A" * 25: receipt(lines)}, c)
    assert s.rows[2].check and s.rows[2].note.startswith(VALID), s.rows[2].note
