"""Aturan validasi. Keputusan akhir dibuat di sini (aturan pasti), bukan oleh AI."""
from __future__ import annotations
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from .models import Extracted, SheetRow, Verdict
from .parser import parse_amount, normalize_ref


@dataclass
class Account:
    bank: str
    number: str
    name: str


def parse_rekening(raw: str) -> Account | None:
    """'BRI 127001000238564 an GURU ERA DIGITAL' -> Account."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(r"^(?P<bank>\S+)\s+(?P<num>[\d\s\-]{6,}?)\s+(?:a\.?\s?n\.?|atas nama)\s+(?P<name>.+)$", raw, re.I)
    if m:
        return Account(m["bank"].upper(), re.sub(r"\D", "", m["num"]), m["name"].strip())
    digits = re.search(r"\d[\d\s\-]{5,}\d", raw)
    name = re.search(r"(?:a\.?\s?n\.?|atas nama)\s+(.+)$", raw, re.I)
    return Account(raw.split()[0].upper(), re.sub(r"\D", "", digits.group(0)) if digits else "",
                   name.group(1).strip() if name else "")


def _compact(text: str) -> str:
    return re.sub(r"[\s\-.]", "", text)


MASK = "*xX•·●"


def match_account(text: str, number: str) -> str | None:
    """'full' bila nomor penuh ada; 'masked' bila format tersamar (1270****8564) cocok."""
    if len(number) < 6:
        return None
    c = _compact(text)
    if number in c:
        return "full"
    for m in re.finditer(r"(\d{0,6})[" + re.escape(MASK) + r"]{2,}(\d{3,6})", c):
        lead, tail = m.group(1), m.group(2)
        if number.endswith(tail) and (not lead or number.startswith(lead)):
            return "masked"
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s.upper())).strip()


def match_name(text: str, name: str) -> bool:
    n = _norm(name)
    return len(n) >= 4 and fuzz.partial_ratio(n, _norm(text)) >= 88


def parse_ts(raw: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def same_moment(a_dt, a_has_time: bool, b_dt, b_has_time: bool) -> bool:
    """Apakah dua bukti terjadi pada saat yang sama? Tanggal sama, dan jam sampai detik sama bila keduanya punya jam.
    Bila tanggal salah satunya tidak diketahui, tidak bisa dibedakan -> dianggap sama (referensi sama)."""
    if a_dt is None or b_dt is None:
        return True
    if a_dt.date() != b_dt.date():
        return False
    if a_has_time and b_has_time:
        return a_dt.time() == b_dt.time()
    return True


class RefRegistry:
    """Bukti yang sudah dipakai: referensi + tanggal + jam (dari kolom ref di sheet + proses berjalan)."""

    def __init__(self):
        self._d: dict[str, list[tuple[int, object, bool]]] = {}
        self._lock = threading.Lock()

    def _find(self, key: str, row: int, dt, has_time: bool) -> int | None:
        for r, d2, h2 in self._d.get(key, []):
            if r != row and same_moment(d2, h2, dt, has_time):
                return r
        return None

    def find(self, ref: str, row: int, dt=None, has_time: bool = False) -> int | None:
        """Baris LAIN yang memegang bukti yang sama (tanpa mendaftarkan), atau None."""
        with self._lock:
            return self._find(normalize_ref(ref), row, dt, has_time)

    def claim(self, ref: str, row: int, dt=None, has_time: bool = False) -> int | None:
        """Daftarkan bukti untuk baris ini. Mengembalikan baris pemilik pertama bila duplikat, selain itu None."""
        key = normalize_ref(ref)
        with self._lock:
            other = self._find(key, row, dt, has_time)
            if other is not None:
                return other
            entries = self._d.setdefault(key, [])
            if not any(r == row for r, _, _ in entries):
                entries.append((row, dt, has_time))
            return None


@dataclass
class Context:
    conf_ok: float = 75.0
    allow_ocr_invalid: bool = False   # True hanya jika Gemini dimatikan pengguna
    other_accounts: list[Account] = field(default_factory=list)
    date_before: int = 7
    date_after: int = 1
    min_amount: int = 0               # donasi di bawah ini tidak sah (0 = nonaktif)
    tolerance: int = 0                # selisih nominal (+/-) yang masih dianggap sama, mis. biaya admin bank
    today: date | None = None         # untuk uji; default = hari ini
    min_date: date | None = None      # bukti bertanggal sebelum ini tidak sah


def _rp(n: int) -> str:
    return "Rp" + f"{n:,}".replace(",", ".")


def _check_nominal(v: Verdict, exp: int, ext: Extracted, ctx: Context) -> None:
    prim = ext.primary
    if not ext.amounts or prim is None:
        v.add("NOMINAL_MISSING", "nominal di bukti tidak terbaca")
        return
    diff = prim.value - exp
    if exp < 1000 and prim.value == exp * 1000:            # mis. sheet "10" padahal 10.000: periksa manual
        v.add("NOMINAL_SHEET_TYPO", f"sheet {_rp(exp)}, bukti {_rp(prim.value)} (kemungkinan salah ketik di sheet)")
        return
    if abs(diff) <= ctx.tolerance:                          # sama, atau selisih dalam toleransi (biaya admin dsb.)
        if prim.repaired:
            v.add("NOMINAL_REPAIRED", f"angka nominal {_rp(prim.value)} terbaca ragu (dikoreksi dari OCR)")
            return
        if diff:
            v.infos.append(f"selisih {'+' if diff > 0 else '-'}{_rp(abs(diff))} dalam toleransi {_rp(ctx.tolerance)}")
        donated = min(exp, prim.value)                      # yang benar-benar masuk tidak lebih dari yang terkecil
        if ctx.min_amount and donated < ctx.min_amount:
            v.add("NOMINAL_BELOW_MIN", f"donasi {_rp(donated)} di bawah minimal {_rp(ctx.min_amount)}")
        return
    if prim.label == "total" and 0 < diff <= 15000:
        v.add("NOMINAL_AMBIGUOUS", f"total {_rp(prim.value)} vs sheet {_rp(exp)}: selisih mungkin biaya admin")
    elif any(a.value == exp for a in ext.amounts):
        v.add("NOMINAL_AMBIGUOUS", f"nominal utama {_rp(prim.value)}, tapi {_rp(exp)} juga muncul di bukti")
    elif ext.source == "gemini" or ctx.allow_ocr_invalid:
        v.add("NOMINAL_MISMATCH", f"nominal bukti {_rp(prim.value)} \u2260 sheet {_rp(exp)}")
    else:  # hasil OCR saja belum cukup untuk menolak -> minta verifikasi Gemini / manual
        v.add("NOMINAL_MISMATCH_OCR", f"nominal terbaca {_rp(prim.value)} \u2260 sheet {_rp(exp)} (perlu verifikasi)")


def validate(row: SheetRow, ext: Extracted, ctx: Context) -> Verdict:
    v = Verdict()
    acc = parse_rekening(row.rekening_raw)

    exp = parse_amount(row.nominal_raw)
    if exp is None:
        v.add("SHEET_NOMINAL", "nominal di sheet tidak terbaca")
    else:
        _check_nominal(v, exp, ext, ctx)

    if ext.not_proof:
        v.add("NOT_PROOF", "gambar tampaknya bukan bukti transfer")
    if ext.anomaly:
        v.add("ANOMALY", f"kejanggalan: {ext.anomaly}")
    if ext.blurry:
        v.add("LOW_QUALITY", "gambar buram/kurang jelas")

    if ext.failed and not ext.success:
        v.add("STATUS_FAILED", "bukti menunjukkan transaksi gagal/pending")
    elif not ext.success:
        v.add("STATUS_MISSING", "tidak ada tulisan transaksi berhasil")

    if acc and acc.number:
        by_number = match_account(ext.text, acc.number)
        by_name = bool(acc.name) and match_name(ext.text, acc.name)
        if not by_number and not by_name:
            other = next((o for o in ctx.other_accounts
                          if o.number != acc.number and match_account(ext.text, o.number)), None)
            if other:
                v.add("DEST_OTHER_ACCOUNT", f"ditransfer ke rekening lain ({other.bank} ...{other.number[-4:]})")
            else:
                v.add("DEST_UNREADABLE", "rekening/nama tujuan tidak terbaca di bukti")
    else:
        v.add("SHEET_DEST", "rekening tujuan di sheet tidak terbaca")

    ts = parse_ts(row.timestamp_raw)
    if ext.dt is None:
        v.add("DATE_MISSING", "tanggal di bukti tidak terbaca")
    elif ext.dt.date() > (ctx.today or date.today()) + timedelta(days=1):
        v.add("DATE_FUTURE", f"tanggal bukti {ext.dt:%d/%m/%Y} ada di masa depan (salah baca atau tidak wajar)")
    elif ctx.min_date and ext.dt.date() < ctx.min_date:
        msg = f"tanggal bukti {ext.dt:%d/%m/%Y} sebelum batas minimal {ctx.min_date:%d/%m/%Y}"
        if ext.source == "gemini" or ctx.allow_ocr_invalid:
            v.add("DATE_BEFORE_MIN", msg)
        else:  # tanggal dari OCR bisa salah baca -> verifikasi Gemini dulu
            v.add("DATE_BEFORE_MIN_OCR", msg + " (perlu verifikasi)")
    elif ts:
        delta = (ext.dt.date() - ts.date()).days
        too_late = delta > ctx.date_after                                  # bukti bertanggal setelah form diisi
        too_early = (not ctx.min_date) and delta < -ctx.date_before       # batas bawah = tanggal minimal bila diisi
        if too_late or too_early:
            v.add("DATE_OUT_OF_RANGE", f"tanggal bukti {ext.dt:%d/%m/%Y} jauh dari waktu isi form {ts:%d/%m/%Y}")
    return v
