"""Ekstraksi nominal / tanggal / referensi / status dari teks hasil OCR."""
from __future__ import annotations
import re
from datetime import datetime

from .models import Amount, Extracted

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7,
          "agu": 8, "agt": 8, "ags": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
          "nov": 11, "des": 12, "dec": 12}

# salah-baca OCR yang umum di dalam angka
FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "s": "5", "B": "8"})


def parse_amount(s: str) -> int | None:
    """'Rp 20.000' / '20000' / '20.000,00' / '20,000.00' -> 20000. Gagal -> None."""
    s = str(s).lower().replace("rp", "").replace("idr", "").replace(" ", "").strip(".,-:")
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", s):
        return int(s.split(",")[0].replace(".", ""))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", s):
        return int(s.split(".")[0].replace(",", ""))
    if re.fullmatch(r"\d+([.,]\d{1,2})?", s):
        return int(re.split(r"[.,]", s)[0])
    return None


AMOUNT_RE = re.compile(r"(?<![A-Za-z])(?:Rp|IDR)\.?\s*([0-9OoIl|SsB][0-9OoIl|SsB.,]*)(?![A-Za-z])", re.I)
NUM_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{4,9})(?:[.,]\d{2})?(?!\d)")
PRIMARY_KW = ("nominal", "jumlah", "amount", "nilai transfer", "total transfer")
TOTAL_KW = ("total",)
FEE_KW = re.compile(r"biaya|admin|fee|charge")


def _amounts_in_line(line: str, allow_bare: bool) -> list[tuple[int, bool]]:
    out: list[tuple[int, bool]] = []
    for m in AMOUNT_RE.finditer(line):
        raw = m.group(1).rstrip(".,-")
        if not re.search(r"\d", raw):
            continue
        fixed = raw.translate(FIX)
        val = parse_amount(fixed)
        if val:
            out.append((val, fixed != raw))
    if not out and allow_bare:
        for m in NUM_RE.finditer(line):
            val = parse_amount(m.group(1))
            if val:
                out.append((val, False))
    return out


def extract_amounts(text: str) -> list[Amount]:
    lines = [ln.strip() for ln in text.splitlines()]
    result: list[Amount] = []
    for i, line in enumerate(lines):
        low = line.lower()
        kind = ""
        if not FEE_KW.search(low):
            if any(k in low for k in PRIMARY_KW):
                kind = "primary"
            elif any(k in low for k in TOTAL_KW):
                kind = "total"
        found = _amounts_in_line(line, allow_bare=bool(kind))
        if kind and not found:  # nilai ada di baris berikutnya (tata letak 2 kolom)
            nxt = next((l for l in lines[i + 1:i + 3] if l), "")
            found = _amounts_in_line(nxt, allow_bare=True)
        for val, rep in found:
            result.append(Amount(val, False, kind, rep))
    chosen = (next((a for a in result if a.label == "primary"), None)
              or next((a for a in result if a.label == "total"), None)
              or (result[0] if result else None))
    if chosen:
        chosen.primary = True
    return result


DATE_PATTERNS = [
    (re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)"), "dmy"),
    (re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)"), "dMy"),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), "ymd"),
]
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?:[:.]([0-5]\d))?(?!\d)")


def extract_datetime(text: str) -> tuple[datetime | None, bool]:
    best = None
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "dmy":
                    d, mo, y = int(m[1]), int(m[2]), int(m[3])
                elif kind == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                else:
                    mo = MONTHS.get(m[2][:3].lower())
                    if not mo:
                        continue
                    d, y = int(m[1]), int(m[3])
                if not (2000 <= y <= 2100):
                    continue
                dt = datetime(y, mo, d)
            except ValueError:
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), m.end(), dt)
    if not best:
        return None, False
    start, end, dt = best
    tm = TIME_RE.search(text[end:end + 45]) or TIME_RE.search(text[max(0, start - 25):start])
    if tm:
        dt = dt.replace(hour=int(tm[1]), minute=int(tm[2]), second=int(tm[3] or 0))
        return dt, True
    return dt, False


REF_RE = re.compile(
    r"(?:no\.?\s*(?:ref(?:erensi)?|transaksi|trx|reff)|\bref(?:erence|f)?\.?\s*(?:no\.?|number|id)?"
    r"|id\s*transaksi|transaction\s*(?:id|no\.?)|kode\s*(?:transaksi|referensi)"
    r"|nomor\s*(?:referensi|transaksi)|trx\s*id)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-]{5,})", re.I)


def extract_ref(text: str) -> str | None:
    for m in REF_RE.finditer(text):
        cand = m.group(1)
        if sum(c.isdigit() for c in cand) >= 4:
            return normalize_ref(cand)
    return None


def normalize_ref(ref: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ref.upper())


SUCCESS_RE = re.compile(r"\b(berhasil|sukses|success|successful|completed|selesai)\b", re.I)
FAIL_RE = re.compile(r"\b(gagal|failed|dibatalkan|pending|menunggu)\b", re.I)


def extract(text: str, conf: float = 0.0) -> Extracted:
    dt, has_time = extract_datetime(text)
    return Extracted(
        text=text, amounts=extract_amounts(text), dt=dt, has_time=has_time,
        ref=extract_ref(text), success=bool(SUCCESS_RE.search(text)),
        failed=bool(FAIL_RE.search(text)), source="ocr", conf=conf,
    )
