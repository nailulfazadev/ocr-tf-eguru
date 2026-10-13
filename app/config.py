from __future__ import annotations
import json
import os
import re
import secrets
from dataclasses import dataclass, fields
from pathlib import Path

APP_NAME = "Cek Bukti TF e-guru.id"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
LEGACY_MODEL_PREFIXES = ("gemini-1.", "gemini-2.0", "gemini-2.5")
APP_DIR = Path.home() / ".bukti_tf"          # nama folder dipertahankan agar pengaturan lama tidak hilang
CACHE_DIR = APP_DIR / "cache"
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_PATH = APP_DIR / "token.json"
CLIENT_SECRET_PATH = APP_DIR / "client_secret.json"
SERVICE_ACCOUNT_PATH = APP_DIR / "service_account.json"


def col_to_idx(col: str) -> int:
    n = 0
    for ch in col.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def idx_to_col(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_spreadsheet_id(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else url.strip()


def parse_gid(url: str) -> int | None:
    m = re.search(r"gid=(\d+)", url)
    return int(m.group(1)) if m else None


def gemini_params(cfg: "Config") -> tuple[int, int]:
    """(request per menit, jumlah panggilan bersamaan). 0 = otomatis menurut mode gratis/berbayar."""
    rpm = cfg.gemini_rpm or (120 if cfg.gemini_paid else 13)
    conc = cfg.gemini_concurrency or (6 if cfg.gemini_paid else 1)
    return max(1, rpm), max(1, conc)


@dataclass
class Config:
    """Konfigurasi efektif satu event = pengaturan event + pengaturan umum yang digabung."""
    name: str = "Event 1"
    # ---- per event ----
    spreadsheet_url: str = ""
    spreadsheet_id: str = ""
    sheet_title: str = ""
    sheet_id: int = 0
    col_link: str = "H"
    col_nominal: str = "G"
    col_rekening: str = "F"
    col_nama: str = "C"
    col_timestamp: str = "A"
    col_check: str = "I"
    col_note: str = "K"
    col_ref: str = "L"
    first_row: int = 2
    row_from: int = 0              # 0 = tanpa batas
    row_to: int = 0
    reprocess: str = "none"        # none = lanjutkan saja | manual = ulang yang CEK MANUAL | auto = ulang SEMUA hasil otomatis
    amount_tolerance: int = 6500   # selisih nominal (+/-) yang masih valid, mis. biaya admin bank terbesar
    min_amount: int = 0            # donasi minimal (Rp); 0 = tanpa minimal
    min_date: str = ""             # "YYYY-MM-DD"; kosong = tidak dipakai
    date_before_days: int = 7
    date_after_days: int = 1
    # ---- umum (dipakai semua event) ----
    officer: str = ""
    use_gemini: bool = True
    gemini_only: bool = False      # True = lewati Tesseract, semua bukti langsung dibaca Gemini
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_paid: bool = False      # akun berbayar -> lebih cepat
    gemini_rpm: int = 0            # 0 = otomatis (gratis 13, berbayar 120); lihat batas Anda di AI Studio
    gemini_concurrency: int = 0    # 0 = otomatis (gratis 1, berbayar 6)
    install_id: str = ""           # penanda komputer ini (untuk mengambil kembali klaim sendiri setelah macet)
    workers: int = 3
    block_size: int = 50
    claim_ttl_min: int = 30
    ocr_conf_ok: float = 75.0

    # kompatibilitas: baca/tulis event pertama
    @classmethod
    def load(cls) -> "Config":
        s = Store.load()
        return s.config_for(s.last_event)

    def save(self) -> None:
        s = Store.load()
        s.update_global(self)
        s.update_event(self)
        s.save()


GLOBAL_KEYS = {"officer", "use_gemini", "gemini_only", "gemini_api_key", "gemini_model", "gemini_paid", "gemini_rpm",
               "gemini_concurrency", "install_id", "workers", "block_size", "claim_ttl_min", "ocr_conf_ok"}
_ALL_KEYS = {f.name for f in fields(Config)}


class Store:
    """config.json: {"global": {...}, "events": [{"name": ..., ...}], "last_event": ...}"""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.globals: dict = {}
        self.events: list[dict] = []
        self.last_event = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Store":
        s = cls(path)
        p = path or CONFIG_PATH
        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if isinstance(data.get("events"), list):
            s.globals = {k: v for k, v in (data.get("global") or {}).items() if k in GLOBAL_KEYS}
            s.events = [{k: v for k, v in e.items() if k in (_ALL_KEYS - GLOBAL_KEYS) | {"retry_manual"}} for e in data["events"]
                        if isinstance(e, dict)]
            s.last_event = str(data.get("last_event", ""))
        elif data:                                     # format lama: satu konfigurasi datar -> "Event 1"
            flat = {k: v for k, v in data.items() if k in _ALL_KEYS | {"retry_manual"}}
            s.globals = {k: v for k, v in flat.items() if k in GLOBAL_KEYS}
            s.events = [{k: v for k, v in flat.items() if k not in GLOBAL_KEYS} | {"name": "Event 1"}]
        if not s.events:
            s.events = [{"name": "Event 1"}]
        for e in s.events:                             # migrasi: opsi lama "proses ulang CEK MANUAL"
            if e.pop("retry_manual", False) and "reprocess" not in e:
                e["reprocess"] = "manual"
        if not s.globals.get("install_id"):
            s.globals["install_id"] = secrets.token_hex(3)
        seen: set[str] = set()
        for i, e in enumerate(s.events):               # nama harus ada dan unik
            n = str(e.get("name") or f"Event {i + 1}")
            while n in seen:
                n += " (2)"
            e["name"] = n; seen.add(n)
        if s.globals.get("gemini_model", "").startswith(LEGACY_MODEL_PREFIXES):
            s.globals["gemini_model"] = DEFAULT_GEMINI_MODEL     # model lama tidak tersedia untuk proyek baru
        if s.last_event not in seen:
            s.last_event = s.events[0]["name"]
        return s

    def save(self) -> None:
        p = self.path or CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 2, "global": self.globals, "events": self.events,
                                   "last_event": self.last_event}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # ---- event ----
    def names(self) -> list[str]:
        return [e["name"] for e in self.events]

    def _event(self, name: str) -> dict:
        return next(e for e in self.events if e["name"] == name)

    def config_for(self, name: str) -> Config:
        cfg = Config()
        for k, v in self.globals.items():
            setattr(cfg, k, v)
        if name in self.names():
            for k, v in self._event(name).items():
                setattr(cfg, k, v)
        cfg.name = name
        return cfg

    def unique_name(self, base: str) -> str:
        base = base.strip() or "Event"
        n, i = base, 2
        while n in self.names():
            n, i = f"{base} {i}", i + 1
        return n

    def add(self, name: str | None = None, copy_from: str | None = None) -> str:
        name = self.unique_name(name or f"Event {len(self.events) + 1}")
        ev = dict(self._event(copy_from)) if copy_from and copy_from in self.names() else {}
        ev["name"] = name
        self.events.append(ev)
        return name

    def rename(self, old: str, new: str) -> str:
        new = new.strip()
        if not new or new == old:
            return old
        if new in self.names():
            raise ValueError("Nama event sudah dipakai.")
        self._event(old)["name"] = new
        if self.last_event == old:
            self.last_event = new
        return new

    def delete(self, name: str) -> None:
        if len(self.events) <= 1:
            raise ValueError("Minimal harus ada satu event.")
        self.events = [e for e in self.events if e["name"] != name]
        if self.last_event == name:
            self.last_event = self.events[0]["name"]

    def update_event(self, cfg: Config) -> None:
        """Simpan hanya bagian event dari cfg (tidak menyentuh pengaturan umum)."""
        ev = self._event(cfg.name)
        for k in _ALL_KEYS - GLOBAL_KEYS - {"name"}:
            ev[k] = getattr(cfg, k)

    def update_global(self, cfg: Config) -> None:
        for k in GLOBAL_KEYS:
            self.globals[k] = getattr(cfg, k)
