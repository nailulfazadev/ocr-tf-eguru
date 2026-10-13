"""Alur kerja: klaim blok baris -> unduh -> OCR -> (Gemini bila ragu) -> validasi -> tulis ke sheet."""
from __future__ import annotations
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable

import pytesseract

from . import ocr, parser
from .config import Config, gemini_params
from .gemini import GeminiModelError, GeminiQuotaError, GeminiReader
from .models import HARD_CODES, INVALID, MANUAL, VALID, Extracted, RowResult, SheetRow, Verdict
from .sheets import DriveAccessError, DriveClient, SheetsClient, extract_file_ids
from .validator import Context, RefRegistry, parse_rekening, validate

CLAIM_RE = re.compile(r"^\[diproses\|(.*?)\|(.+?)\]$")
# catatan galat teknis dari versi lama: boleh diproses ulang
RETRY_NOTE_RE = re.compile(r"^CEK MANUAL \| (galat:|tidak punya akses)")

# masalah "salah baca" -> layak dicoba ulang dengan Gemini
RETRYABLE = {"NOMINAL_MISSING", "NOMINAL_REPAIRED", "NOMINAL_AMBIGUOUS", "NOMINAL_MISMATCH_OCR",
             "DATE_MISSING", "DATE_OUT_OF_RANGE", "DATE_BEFORE_MIN_OCR", "DATE_FUTURE", "DEST_UNREADABLE", "STATUS_MISSING", "STATUS_FAILED"}


TESSERACT_HELP = ("Tesseract belum terpasang. Mac: 'brew install tesseract tesseract-lang'. Windows: pasang Tesseract "
                  "(UB Mannheim) dengan bahasa Indonesian. ATAU isi API key Gemini dan centang 'Pakai Gemini' agar "
                  "aplikasi memakai mode Gemini saja (tanpa Tesseract). Lalu jalankan ulang.")


class TransientError(Exception):
    """Galat sementara: baris tidak ditulis ke sheet dan akan dicoba lagi."""


LIVE_MARKERS: set[str] = set()                  # penanda klaim yang sedang dikerjakan di aplikasi ini (semua event)
_LIVE_LOCK = threading.Lock()
HUMAN_RE = re.compile(r"^(VALID|TIDAK VALID) \(manual\)")


def is_eligible(row: SheetRow, ttl_min: int, now: datetime | None = None, reprocess: str = "none",
                own_id: str = "", live: set[str] | frozenset[str] = frozenset()) -> bool:
    """Baris mana yang perlu diproses.
    Default (reprocess="none"): lewati yang sudah dicentang atau sudah ada catatan -> proses berhenti & lanjut tanpa mengulang.
      manual: ulangi juga baris CEK MANUAL otomatis.   auto: ulangi SEMUA hasil otomatis (VALID / TIDAK VALID / CEK MANUAL).
    Keputusan manusia ("VALID (manual)" / "TIDAK VALID (manual)") dan ceklis tanpa catatan tidak pernah diulang.
    Klaim "[diproses|...]" milik komputer ini yang tidak sedang dikerjakan (sisa proses yang macet) langsung boleh diambil lagi."""
    now = now or datetime.now()
    if not (row.link_raw or row.nama):
        return False
    note = row.note.strip()
    if row.check:
        return reprocess == "auto" and note.startswith(VALID) and not HUMAN_RE.match(note)
    if not note:
        return True
    if RETRY_NOTE_RE.match(note):
        return True
    m = CLAIM_RE.match(note)
    if m:
        if own_id and f"#{own_id}" in m.group(1) and note not in live:
            return True
        try:
            return now - datetime.fromisoformat(m.group(2)) > timedelta(minutes=ttl_min)
        except ValueError:
            return True
    if HUMAN_RE.match(note):
        return False
    if reprocess == "auto" and note.startswith((VALID, INVALID, MANUAL)):
        return True
    if reprocess == "manual" and note.startswith(MANUAL):
        return True
    return False


def encode_ref(ref: str, dt: datetime | None, has_time: bool) -> str:
    if dt is None:
        return ref
    return f"{ref}|{dt:%Y-%m-%dT%H:%M:%S}" if has_time else f"{ref}|{dt:%Y-%m-%d}"


def decode_ref(s: str) -> tuple[str, datetime | None, bool]:
    ref, _, d = s.strip().partition("|")
    try:
        if len(d) == 10:
            return ref, datetime.strptime(d, "%Y-%m-%d"), False
        if d:
            return ref, datetime.strptime(d, "%Y-%m-%dT%H:%M:%S"), True
    except ValueError:
        pass
    return ref, None, False


def _rp(n: int) -> str:
    return "Rp" + f"{n:,}".replace(",", ".")


def build_note(status: str, verdict: Verdict, ext: Extracted | None, officer: str) -> str:
    parts = [status]
    if ext:
        if ext.primary:
            parts.append(_rp(ext.primary.value))
        if ext.dt:
            parts.append(ext.dt.strftime("%d/%m/%Y %H:%M" if ext.has_time else "%d/%m/%Y"))
        if ext.ref:
            parts.append("ref " + ext.ref)
    if verdict.reasons:
        parts.append("; ".join(verdict.reasons))
    if verdict.infos:
        parts.append("; ".join(verdict.infos))
    src = ext.source if ext else "-"
    parts.append(f"{src}/{officer} {datetime.now():%d/%m %H:%M}")
    return " | ".join(parts)[:900]


class Engine:
    def __init__(self, cfg: Config, sheets: SheetsClient, drive: DriveClient,
                 on_result: Callable[[RowResult], None] = lambda r: None,
                 on_log: Callable[[str], None] = print,
                 on_progress: Callable[[int, int], None] = lambda d, t: None,
                 gemini: GeminiReader | None = None):
        self.cfg, self.sheets, self.drive = cfg, sheets, drive
        self.on_result, self.on_log, self.on_progress = on_result, on_log, on_progress
        self.officer = cfg.officer.strip() or "petugas"
        self.refs = RefRegistry()
        self.ctx = Context(conf_ok=cfg.ocr_conf_ok, allow_ocr_invalid=not cfg.use_gemini,
                           date_before=cfg.date_before_days, date_after=cfg.date_after_days)
        self.ctx.min_amount = max(0, int(cfg.min_amount or 0))
        self.ctx.tolerance = max(0, int(cfg.amount_tolerance or 0))
        if cfg.min_date:
            try:
                self.ctx.min_date = datetime.strptime(cfg.min_date, "%Y-%m-%d").date()
            except ValueError:
                self.on_log(f"Tanggal minimal '{cfg.min_date}' tidak valid, diabaikan.")
        self.gemini = gemini                      # pembaca bersama (satu untuk semua event) bila diberikan
        self._gemini_off = False
        key = cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        if self.gemini is None and cfg.use_gemini and key:
            rpm, conc = gemini_params(cfg)
            try:
                self.gemini = GeminiReader(key, cfg.gemini_model, rpm, conc)
            except Exception as e:
                self.on_log(f"Gemini tidak bisa dipakai: {e}")
        elif self.gemini is None and cfg.use_gemini:
            self.on_log("Gemini aktif tapi API key kosong -> bukti yang meragukan masuk CEK MANUAL.")
        self._workers = max(1, cfg.workers)
        self._run = threading.Event(); self._run.set()
        self._stop = False
        self._fatal = False
        self.ocr_ok = True
        self._token = secrets.token_hex(2)
        self._reprocess_rows: set[int] = set()   # baris yang akan diproses ulang: refnya tidak dihitung sebagai "sudah dipakai"
        self._handled: set[int] = set()   # baris yang sudah diproses pada proses ini (agar tidak berulang)
        self._skip: set[int] = set()   # baris yang gagal secara teknis pada proses ini
        self.seen_links: dict[str, int] = {}  # file_id -> row number (untuk deteksi bukti kembar)
        self._marker = ""

    @property
    def gemini_off(self) -> bool:
        return self._gemini_off or bool(getattr(self.gemini, "off", False))

    @gemini_off.setter
    def gemini_off(self, v: bool) -> None:
        self._gemini_off = v
        if v and self.gemini is not None:          # beri tahu semua event yang memakai pembaca yang sama
            try:
                self.gemini.off = True
            except AttributeError:
                pass

    # ---- kontrol dari UI ----
    def pause(self): self._run.clear()
    def resume(self): self._run.set()
    def stop(self):
        self._stop = True
        self._run.set()

    # ---- util ----
    def _in_range(self, n: int) -> bool:
        c = self.cfg
        return (not c.row_from or n >= c.row_from) and (not c.row_to or n <= c.row_to)

    def _refresh_context(self, snap: list[SheetRow]) -> None:
        accs, seen = [], set()
        for r in snap:
            a = parse_rekening(r.rekening_raw)
            if a and a.number and a.number not in seen:
                seen.add(a.number); accs.append(a)
        self.ctx.other_accounts = accs
        for r in snap:                           # ref yang sudah tercatat (oleh siapa pun di tim)
            if r.ref and r.row not in self._reprocess_rows:
                ref, dt, ht = decode_ref(r.ref)
                if ref:
                    self.refs.claim(ref, r.row, dt, ht)
            if (r.check or (r.note.strip() and not r.note.strip().startswith("[diproses"))) and r.row not in self._reprocess_rows:
                fids = extract_file_ids(r.link_raw)
                if fids and fids[0] not in self.seen_links:
                    self.seen_links[fids[0]] = r.row

    # ---- klaim blok ----
    def _claim(self, snap: list[SheetRow]) -> tuple[bool, list[SheetRow]]:
        cfg = self.cfg
        with _LIVE_LOCK:
            live = frozenset(LIVE_MARKERS)
        elig = [r for r in snap if self._in_range(r.row) and r.row not in self._handled
                and is_eligible(r, cfg.claim_ttl_min, reprocess=cfg.reprocess, own_id=cfg.install_id, live=live)]
        chosen = elig[:cfg.block_size]
        if not chosen:
            return False, []
        who = f"{self.officer}#{self.cfg.install_id}.{self._token}" if self.cfg.install_id else self.officer
        self._marker = f"[diproses|{who}|{datetime.now():%Y-%m-%dT%H:%M:%S}]"
        with _LIVE_LOCK:
            LIVE_MARKERS.add(self._marker)
        self.sheets.batch_write([(f"{cfg.col_note}{r.row}", self._marker) for r in chosen])
        time.sleep(1.5)                          # beri waktu bila ada anggota lain menulis bersamaan
        cur = self.sheets.read_cells([f"{cfg.col_note}{r.row}" for r in chosen])
        mine = [r for r, v in zip(chosen, cur) if v.strip() == self._marker]
        if len(mine) < len(chosen):
            self.on_log(f"{len(chosen) - len(mine)} baris diambil anggota lain, dilewati.")
        return True, mine

    def _release(self, rows: list[SheetRow]) -> None:
        cfg = self.cfg
        cur = self.sheets.read_cells([f"{cfg.col_note}{r.row}" for r in rows])
        self.sheets.batch_write([(f"{cfg.col_note}{r.row}", "") for r, v in zip(rows, cur)
                                 if v.strip() == self._marker])

    # ---- proses satu baris ----
    def _simple(self, row: SheetRow, code: str, msg: str, transient: bool = False) -> RowResult:
        v = Verdict(); v.add(code, msg)
        note = "" if transient else build_note(v.status, v, None, self.officer)
        return RowResult(row, v, None, note, transient=transient)

    def _analyse(self, row: SheetRow) -> RowResult:
        ids = extract_file_ids(row.link_raw)
        if not ids:
            return self._simple(row, "NO_LINK", "tidak ada link bukti")
        data, mime = self.drive.fetch(ids[0])
        if not mime.startswith("image/"):
            return self._simple(row, "NOT_IMAGE", f"file bukan gambar ({mime}), cek manual")

        if self.ocr_ok:
            text, conf = ocr.run_ocr(data)
            ext = parser.extract(text, conf)
            verdict = validate(row, ext, self.ctx)

            if verdict.codes & RETRYABLE and not verdict.codes & HARD_CODES and self.gemini and not self.gemini_off:
                ocr_ref = ext.ref
                try:
                    ext_g = self.gemini.extract(data, mime)
                    ext_g.ref = ext_g.ref or ocr_ref
                    ext, verdict = ext_g, validate(row, ext_g, self.ctx)
                except GeminiQuotaError:
                    self.gemini_off = True
                    self.on_log("Kuota Gemini habis. Sisa bukti yang meragukan masuk CEK MANUAL (lanjutkan besok).")
                    verdict.add("GEMINI_QUOTA", "kuota Gemini habis")
                except GeminiModelError as e:
                    self.gemini_off = True
                    self.on_log(f"Model Gemini '{self.cfg.gemini_model}' tidak tersedia ({str(e)[:90]}). "
                                "Buka Pengaturan, klik 'Tes Gemini', dan pilih model yang tersedia.")
                    verdict.add("GEMINI_ERROR", "model Gemini tidak tersedia")
                except Exception as e:
                    verdict.add("GEMINI_ERROR", f"Gemini gagal: {type(e).__name__}: {str(e)[:80]}")
            elif verdict.codes & RETRYABLE and self.gemini_off:
                verdict.add("GEMINI_QUOTA", "kuota Gemini habis")

        else:                                    # mode Gemini saja (Tesseract tidak tersedia)
            ext = self._gemini_only(data, mime)
            verdict = validate(row, ext, self.ctx)
        if len(ids) > 1:
            verdict.add("MULTI", f"ada {len(ids)} file bukti, hanya yang pertama diperiksa")
        ref, dup_of = "", None
        # Cek duplikat file ID Google Drive (misal bukti sama diunggah ulang)
        dup_link = self.seen_links.get(ids[0]) if ids else None
        if dup_link is not None and dup_link != row.row:
            dup_of = dup_link
            verdict.add("DUP_REF", f"file bukti sama persis dengan baris {dup_of}")
        elif ext.ref:
            # baris yang sudah ditolak tidak boleh "memegang" bukti (agar tidak meracuni baris lain)
            if verdict.status == INVALID:
                dup_of = self.refs.find(ext.ref, row.row, ext.dt, ext.has_time)
            else:
                dup_of = self.refs.claim(ext.ref, row.row, ext.dt, ext.has_time)
            if dup_of is not None:
                verdict.add("DUP_REF", f"bukti sama dengan baris {dup_of} (referensi, tanggal, dan jam sama)")
            elif verdict.status != INVALID:
                ref = encode_ref(ext.ref, ext.dt, ext.has_time)
        if ids and verdict.status != INVALID and dup_of is None:
            self.seen_links[ids[0]] = row.row
        return RowResult(row, verdict, ext, build_note(verdict.status, verdict, ext, self.officer), ref, dup_of=dup_of)

    def _gemini_only(self, data: bytes, mime: str) -> Extracted:
        if self.gemini is None or self.gemini_off:
            raise TransientError("kuota Gemini habis")
        try:
            return self.gemini.extract(data, mime)
        except GeminiQuotaError:
            self.gemini_off = True
            self.on_log("Kuota Gemini habis. Proses dihentikan; jalankan lagi nanti (baris yang selesai tidak diulang).")
            self.stop()
            raise TransientError("kuota Gemini habis")
        except GeminiModelError as e:
            self.gemini_off = True
            self.on_log(f"Model Gemini '{self.cfg.gemini_model}' tidak tersedia ({str(e)[:90]}). "
                        "Buka Pengaturan, klik 'Tes Gemini', dan pilih model yang tersedia.")
            self.stop()
            raise TransientError("model Gemini tidak tersedia")

    def _process(self, row: SheetRow) -> RowResult | None:
        self._run.wait()
        if self._stop:
            return None
        try:
            return self._analyse(row)
        except pytesseract.TesseractNotFoundError:
            self._fatal = True
            self.on_log(TESSERACT_HELP)
            self.stop()
            return None
        except TransientError as e:
            return self._simple(row, "TRANSIENT", f"{e} (tidak ditulis, akan dicoba lagi)", transient=True)
        except DriveAccessError as e:       # galat teknis: jangan "mengunci" baris dengan catatan
            return self._simple(row, "NO_ACCESS", f"{e} (tidak ditulis, akan dicoba lagi)", transient=True)
        except Exception as e:
            return self._simple(row, "ERROR", f"galat: {type(e).__name__}: {str(e)[:120]} (tidak ditulis, akan dicoba lagi)",
                                transient=True)

    # ---- tulis hasil ----
    def _flush(self, pending: list[RowResult]) -> None:
        if not pending:
            return
        cfg = self.cfg
        cur = self.sheets.read_cells([f"{cfg.col_note}{r.row.row}" for r in pending])
        pairs, written = [], []
        for r, now_note in zip(pending, cur):
            if now_note.strip() != self._marker:      # baris sudah disentuh orang lain -> jangan timpa
                self.on_log(f"Baris {r.row.row}: konflik, hasil tidak ditulis.")
                continue
            written.append(r)
            pairs.append((f"{cfg.col_note}{r.row.row}", r.note))
            pairs.append((f"{cfg.col_check}{r.row.row}", r.status == VALID))
            if r.ref:
                pairs.append((f"{cfg.col_ref}{r.row.row}", r.ref))
        self.sheets.batch_write(pairs)
        self.on_log(f"{len(written)} baris ditulis ke spreadsheet.")
        # bukti kembar (referensi + tanggal + jam sama): beri warna KUNING pada sel bukti kedua baris
        cells = sorted({(row, cfg.col_link) for r in written if r.dup_of is not None
                        for row in (r.row.row, r.dup_of)})
        if cells:
            try:
                self.sheets.highlight_cells(cells)
                self.on_log("Bukti kembar ditandai kuning di baris: " + ", ".join(str(c[0]) for c in cells))
            except Exception as e:
                self.on_log(f"Tidak bisa mewarnai sel bukti kembar: {type(e).__name__}: {str(e)[:80]}")

    def _run_block(self, block: list[SheetRow]) -> int:
        try:
            return self._run_block_inner(block)
        finally:
            with _LIVE_LOCK:
                LIVE_MARKERS.discard(self._marker)

    def _run_block_inner(self, block: list[SheetRow]) -> int:
        pending: list[RowResult] = []
        processed: set[int] = set()     # baris yang hasilnya final (ditulis ke sheet)
        n = 0
        last_flush = time.monotonic()
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futs = [ex.submit(self._process, r) for r in block]
            for f in as_completed(futs):
                res = f.result()
                if res is None:
                    continue
                n += 1
                self._handled.add(res.row.row)
                self.on_result(res)
                if res.transient:                     # tidak ditulis; klaim dilepas di bawah
                    self._skip.add(res.row.row)
                    continue
                processed.add(res.row.row)
                pending.append(res)
                if len(pending) >= 20 or time.monotonic() - last_flush >= 8:   # simpan berkala, hemat kuota Sheets API
                    self._flush(pending); pending = []; last_flush = time.monotonic()
        self._flush(pending)
        left = [r for r in block if r.row not in processed]
        if left:
            self._release(left)
        return n

    def run(self) -> None:
        cfg = self.cfg
        try:
            if getattr(cfg, "gemini_only", False) and self.gemini:
                raise pytesseract.TesseractNotFoundError()   # dipilih pengguna: lewati Tesseract
            lang = ocr.check_ready()
        except pytesseract.TesseractNotFoundError:
            if not self.gemini:
                self.on_log(TESSERACT_HELP)
                return
            self.ocr_ok = False
            why = "dipilih di Pengaturan" if getattr(cfg, "gemini_only", False) else "Tesseract tidak ditemukan"
            self.on_log(f"MODE GEMINI SAJA ({why}): semua bukti dibaca Gemini '{cfg.gemini_model}' "
                        f"({getattr(self.gemini, 'rpm', '?')} req/mnt, {getattr(self.gemini, 'concurrency', 1)} bersamaan).")
            lang = "ind"
            self._workers = max(self._workers, getattr(self.gemini, "concurrency", 1))   # Gemini saja: ikuti kecepatannya
        if "ind" not in lang:
            self.on_log("Peringatan: data bahasa Indonesia untuk Tesseract belum ada (memakai bahasa Inggris saja). "
                        "Mac: 'brew install tesseract-lang'.")
        row_count = next((c for t, i, c in self.sheets.list_tabs() if i == cfg.sheet_id), 2000)
        self.sheets.ensure_checkbox(cfg, row_count)
        snap = self.sheets.read_rows(cfg)
        def eligible_now(r):
            return (self._in_range(r.row)
                    and is_eligible(r, cfg.claim_ttl_min, reprocess=cfg.reprocess, own_id=cfg.install_id))
        todo = [r for r in snap if eligible_now(r)]
        total = len(todo)
        self._reprocess_rows = {r.row for r in todo if r.ref}      # ref lama baris yang diulang tidak boleh memblokir dirinya
        done_before = sum(1 for r in snap if self._in_range(r.row) and not eligible_now(r)
                          and (r.check or r.note.strip()))
        mode = {"none": "", "manual": " (mode: ulangi CEK MANUAL)", "auto": " (mode: ulangi SEMUA hasil otomatis)"}[cfg.reprocess]
        self.on_log(f"{total} baris akan diproses; {done_before} baris sudah punya hasil dan dilewati{mode}.")
        self.on_progress(0, total)
        done, idle = 0, 0
        while not self._stop:
            snap = self.sheets.read_rows(cfg)
            self._refresh_context(snap)
            had, block = self._claim(snap)
            if not had:
                break
            if not block:                              # semua diambil anggota lain
                idle += 1
                if idle >= 5:
                    break
                time.sleep(2)
                continue
            idle = 0
            done += self._run_block(block)
            self.on_progress(min(done, total), total)
        if self._skip and not self._fatal:
            self.on_log(f"{len(self._skip)} baris gagal secara teknis dan TIDAK ditulis ke sheet "
                        f"(akan dicoba lagi pada proses berikutnya): " + ", ".join(str(n) for n in sorted(self._skip)[:30]))
        self.on_log("Dihentikan." if self._stop else "Selesai.")


# ---- review manual ----
def _status_of(note: str) -> str | None:
    n = note.strip()
    if not n or RETRY_NOTE_RE.match(n) or CLAIM_RE.match(n):
        return None
    for st in (INVALID, VALID, MANUAL):
        if n.startswith(st):
            return st
    return None


def load_review_queue(sheets: SheetsClient, cfg: Config, mode: str = "manual") -> list[SheetRow]:
    """mode: manual (perlu diputuskan) | invalid | valid | all (periksa ulang hasil otomatis)."""
    out = []
    for r in sheets.read_rows(cfg):
        st = _status_of(r.note)
        if st is None:
            continue
        if mode == "manual" and (st != MANUAL or r.check):
            continue
        if mode == "invalid" and st != INVALID:
            continue
        if mode == "valid" and st != VALID:
            continue
        out.append(r)
    return out


def load_manual_queue(sheets: SheetsClient, cfg: Config) -> list[SheetRow]:
    return load_review_queue(sheets, cfg, "manual")


def apply_manual(sheets: SheetsClient, cfg: Config, row: SheetRow, ok: bool, officer: str, reason: str = "") -> str:
    cur = sheets.read_cells([f"{cfg.col_note}{row.row}"])[0]
    if cur.strip() != row.note.strip():
        raise RuntimeError("Baris ini sudah diubah orang lain. Muat ulang antrean.")
    stamp = f"{datetime.now():%d/%m %H:%M}"
    prev = cur[:200]
    if ok:
        note = f"{VALID} (manual) | oleh {officer} {stamp} | sebelumnya: {prev}"
    else:
        note = f"{INVALID} (manual) | {reason or 'ditolak'} | oleh {officer} {stamp} | sebelumnya: {prev}"
    pairs = [(f"{cfg.col_note}{row.row}", note), (f"{cfg.col_check}{row.row}", ok)]
    if not ok:                                   # bukti yang ditolak tidak boleh memblokir referensinya
        pairs.append((f"{cfg.col_ref}{row.row}", ""))
    sheets.batch_write(pairs)
    return note
