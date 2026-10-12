import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.parser import extract, parse_amount, extract_datetime
from app.validator import validate, Context, parse_rekening, match_account
from app.models import SheetRow, VALID, INVALID, MANUAL

BRIMO = """Transaksi Berhasil
15 Sep 2026, 21:30:12 WIB
No. Ref 123456789012
Sumber Dana BUDI SANTOSO 1234 5678 9012 345
Jenis Transaksi Transfer BI Fast
Bank Tujuan BRI
Nama Tujuan GURU ERA DIGITAL
No. Tujuan 127001000238564
Nominal Rp20.000
Biaya Admin Rp2.500
Total Transaksi Rp22.500
"""

DANA_MASKED = """Transfer Berhasil
15/09/2026 22:04
ID Transaksi 20260915ABC99887766
Ke BRI ****4538 Graynada Rastafathya
Jumlah
Rp 10.000
"""

def row(nominal="20.000", rek="BRI 127001000238564 an GURU ERA DIGITAL", ts="15/09/2026 21:32:26", n=2):
    return SheetRow(row=n, nominal_raw=nominal, rekening_raw=rek, timestamp_raw=ts)

CTX = Context(other_accounts=[parse_rekening("BRI 591601037044538 an Graynada Rastafathya")])

def test_parse_amount_formats():
    assert parse_amount("20.000") == 20000
    assert parse_amount("20000") == 20000
    assert parse_amount("10.001") == 10001
    assert parse_amount("Rp 1.500.000,00") == 1500000
    assert parse_amount("20,000.00") == 20000
    assert parse_amount("abc") is None

def test_parse_rekening():
    a = parse_rekening("BRI 127001000238564 an GURU ERA DIGITAL")
    assert (a.bank, a.number, a.name) == ("BRI", "127001000238564", "GURU ERA DIGITAL")

def test_brimo_valid_ignores_fee():
    ext = extract(BRIMO, 90)
    assert ext.primary.value == 20000
    assert validate(row(), ext, CTX).status == VALID

def test_total_with_fee_not_rejected():
    ext = extract(BRIMO.replace("Nominal Rp20.000\n", ""), 90)   # hanya "Total" yang terbaca
    v = validate(row(), ext, CTX)
    assert v.status == MANUAL and "NOMINAL_AMBIGUOUS" in v.codes

def test_masked_account_and_next_line_amount():
    ext = extract(DANA_MASKED, 90)
    assert ext.primary.value == 10000
    r = row("10000", "BRI 591601037044538 an Graynada Rastafathya")
    assert validate(r, ext, CTX).status == VALID

def test_mismatch_from_ocr_is_not_final():
    ext = extract(BRIMO, 95)
    v = validate(row("25.000"), ext, CTX)          # OCR saja -> belum boleh menolak
    assert v.status == MANUAL and "NOMINAL_MISMATCH_OCR" in v.codes

def test_mismatch_confirmed_is_invalid():
    ext = extract(BRIMO, 95); ext.source = "gemini"
    assert validate(row("25.000"), ext, CTX).status == INVALID

def test_mismatch_invalid_when_gemini_disabled():
    ctx = Context(allow_ocr_invalid=True, other_accounts=CTX.other_accounts)
    assert validate(row("25.000"), extract(BRIMO, 95), ctx).status == INVALID

def test_ocr_misread_digits_goes_manual():
    ext = extract(BRIMO.replace("Rp20.000", "RpZ0.000").replace("Rp22.500", "Rp22.500"), 90)
    txt = BRIMO.replace("Nominal Rp20.000", "Nominal Rp2O.OOO")
    ext = extract(txt, 90)
    assert ext.primary.repaired and ext.primary.value == 20000
    v = validate(row(), ext, CTX)
    assert v.status == MANUAL and "NOMINAL_REPAIRED" in v.codes

def test_other_account():
    v = validate(row(), extract(DANA_MASKED, 90), CTX)
    assert "DEST_OTHER_ACCOUNT" in v.codes and v.status == MANUAL

def test_sheet_typo_10_vs_10000():
    ext = extract(DANA_MASKED, 90)
    v = validate(row("10", "BRI 591601037044538 an Graynada Rastafathya"), ext, CTX)
    assert "NOMINAL_SHEET_TYPO" in v.codes and v.status == MANUAL

def test_date_out_of_range():
    ext = extract(BRIMO.replace("15 Sep 2026", "01 Jan 2026"), 90)
    assert "DATE_OUT_OF_RANGE" in validate(row(), ext, CTX).codes

def test_no_success_text():
    ext = extract(BRIMO.replace("Transaksi Berhasil", "Detail Transaksi"), 90)
    assert "STATUS_MISSING" in validate(row(), ext, CTX).codes

def test_date_time_not_confused_with_amount():
    dt, has_time = extract_datetime("Nominal 20.000 pada 15-09-2026 21.30.12")
    assert dt.day == 15 and has_time and dt.hour == 21

def test_match_account_masked_rejects_wrong_tail():
    assert match_account("1270****8564", "127001000238564") == "masked"
    assert match_account("1270****9999", "127001000238564") is None


from datetime import date

def test_min_date_ocr_is_soft_then_gemini_hard():
    ctx = Context(min_date=date(2026, 9, 16), other_accounts=CTX.other_accounts)
    ext = extract(BRIMO, 90)                           # bukti bertanggal 15 Sep 2026
    v = validate(row(), ext, ctx)
    assert v.status == MANUAL and "DATE_BEFORE_MIN_OCR" in v.codes
    ext.source = "gemini"
    v = validate(row(), ext, ctx)
    assert v.status == INVALID and "sebelum batas minimal 16/09/2026" in v.reasons[0]

def test_min_date_boundary_is_inclusive():
    ctx = Context(min_date=date(2026, 9, 15), other_accounts=CTX.other_accounts)
    assert validate(row(), extract(BRIMO, 90), ctx).status == VALID    # tepat di batas = sah


def test_min_amount_rules():
    ext = extract(BRIMO, 90)                                   # bukti Rp20.000, sheet Rp20.000
    assert validate(row(), ext, Context(min_amount=0, other_accounts=CTX.other_accounts)).status == VALID
    assert validate(row(), ext, Context(min_amount=20000, other_accounts=CTX.other_accounts)).status == VALID   # batas inklusif
    v = validate(row(), ext, Context(min_amount=25000, other_accounts=CTX.other_accounts))
    assert v.status == INVALID and "di bawah minimal Rp25.000" in v.reasons[0]


def test_min_date_replaces_7_day_lower_bound():
    r = row(ts="30/09/2026 10:00")                      # form diisi 15 hari setelah tanggal bukti (15 Sep)
    ext = extract(BRIMO, 90)
    assert "DATE_OUT_OF_RANGE" in validate(r, ext, CTX).codes                       # tanpa tanggal minimal: aturan 7 hari
    ctx = Context(min_date=date(2026, 9, 1), other_accounts=CTX.other_accounts)
    assert validate(r, ext, ctx).status == VALID                                    # ada tanggal minimal: itu batas bawahnya
    r2 = row(ts="10/09/2026 10:00")                      # bukti bertanggal SETELAH form diisi -> tetap janggal
    assert "DATE_OUT_OF_RANGE" in validate(r2, ext, ctx).codes

def test_future_date_detected_by_rules():
    ctx = Context(today=date(2026, 9, 10), other_accounts=CTX.other_accounts)
    assert "DATE_FUTURE" in validate(row(), extract(BRIMO, 90), ctx).codes
    ctx = Context(today=date(2026, 10, 10), other_accounts=CTX.other_accounts)
    assert validate(row(), extract(BRIMO, 90), ctx).status == VALID

def test_gemini_prompt_has_today_and_false_future_anomaly_dropped():
    from app.gemini import build_prompt, GeminiReader
    assert "2026-10-10" in build_prompt(date(2026, 10, 10))
    d = {"apakah_bukti_transfer": True, "status_transaksi": "berhasil", "nominal": 20000, "tanggal": "2026-09-15",
         "kejanggalan": "Tanggal transaksi menunjukkan tahun 2026 (masa depan)."}
    assert GeminiReader._to_extracted(d).anomaly is None
    d["kejanggalan"] = "Font pada angka nominal berbeda dari teks lain."
    assert GeminiReader._to_extracted(d).anomaly.startswith("Font")
