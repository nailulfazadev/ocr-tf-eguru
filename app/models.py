from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

VALID, INVALID, MANUAL = "VALID", "TIDAK VALID", "CEK MANUAL"

# Hanya kode ini yang membuat status "TIDAK VALID". Semua masalah lain -> "CEK MANUAL".
HARD_CODES = {"NOMINAL_MISMATCH", "DUP_REF", "DATE_BEFORE_MIN", "NOMINAL_BELOW_MIN"}


@dataclass
class Amount:
    value: int
    primary: bool = False
    label: str = ""          # "primary" | "total" | ""
    repaired: bool = False   # angka dikoreksi dari salah-baca OCR (O->0, S->5, ...)


@dataclass
class Extracted:
    """Hasil baca satu bukti transfer (dari OCR atau Gemini) dalam format seragam."""
    text: str = ""
    amounts: list[Amount] = field(default_factory=list)
    dt: datetime | None = None
    has_time: bool = False
    ref: str | None = None
    success: bool = False
    failed: bool = False
    source: str = "ocr"      # "ocr" | "gemini"
    conf: float = 0.0
    not_proof: bool = False
    anomaly: str | None = None
    blurry: bool = False

    @property
    def primary(self) -> Amount | None:
        return next((a for a in self.amounts if a.primary), None)


@dataclass
class SheetRow:
    row: int
    nama: str = ""
    nominal_raw: str = ""
    rekening_raw: str = ""
    link_raw: str = ""
    timestamp_raw: str = ""
    check: bool = False
    note: str = ""
    ref: str = ""


@dataclass
class Verdict:
    codes: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)      # catatan informasi (tidak mengubah status)

    def add(self, code: str, msg: str) -> None:
        self.codes.add(code)
        self.reasons.append(msg)

    @property
    def status(self) -> str:
        if self.codes & HARD_CODES:
            return INVALID
        return MANUAL if self.codes else VALID


@dataclass
class RowResult:
    row: SheetRow
    verdict: Verdict
    ext: Extracted | None
    note: str
    ref: str = ""
    marker: str = ""
    dup_of: int | None = None  # baris pemilik bukti yang sama (untuk diberi warna kuning di sheet)
    transient: bool = False   # galat teknis: jangan tulis ke sheet, coba lagi nanti

    @property
    def status(self) -> str:
        return self.verdict.status
