"""Pembaca cadangan: Gemini membaca GAMBAR langsung. Hanya dipanggil untuk bukti yang meragukan."""
from __future__ import annotations
import json
import re
from datetime import date
import threading
import time
from datetime import datetime

from .models import Amount, Extracted
from .parser import parse_amount, normalize_ref

PROMPT = """Anda membantu memverifikasi bukti transfer bank/e-wallet Indonesia.
Baca gambar dan kembalikan HANYA JSON dengan kunci berikut. Jangan menebak: jika sebuah nilai
tidak terbaca jelas, isi null.
{
 "apakah_bukti_transfer": true/false,
 "status_transaksi": "berhasil" | "gagal" | "pending" | "tidak_jelas",
 "nominal": angka rupiah tanpa titik/koma (nominal transfer, BUKAN total beserta biaya admin) atau null,
 "tanggal": "YYYY-MM-DD" atau null,
 "jam": "HH:MM" atau null,
 "bank_tujuan": teks atau null,
 "rekening_tujuan": teks persis seperti tampil (boleh mengandung bintang) atau null,
 "nama_penerima": teks atau null,
 "nomor_referensi": teks atau null,
 "kualitas_gambar": "jelas" | "buram",
 "kejanggalan": kalimat singkat bila ada tanda gambar diedit/dimanipulasi (font tidak konsisten,
                tempelan, angka tidak sejajar), selain itu null
}"""


def build_prompt(today: date | None = None) -> str:
    today = today or date.today()
    return (PROMPT + f"\n\nTanggal hari ini: {today:%Y-%m-%d}. Tanggal transaksi sampai hari ini WAJAR dan bukan masa depan "
            "(jangan menilai kewajaran tanggal, nominal, atau nama). Isi \"kejanggalan\" HANYA bila ada tanda "
            "manipulasi visual pada gambar (font/ukuran tidak konsisten, tempelan, angka tidak sejajar).")


FUTURE_RE = re.compile(r"masa depan|future|belum terjadi|belum tiba", re.I)


class GeminiQuotaError(Exception):
    pass


class GeminiModelError(Exception):
    """Model tidak ada / sudah tidak tersedia untuk API key ini."""


def classify_error(e: Exception) -> Exception | None:
    s = str(e); low = s.lower()
    if "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in low:
        return GeminiQuotaError(s[:200])
    if ("404" in s or "NOT_FOUND" in s or "no longer available" in low or "new users" in low
            or "not found" in low or "is not supported" in low):
        return GeminiModelError(s[:200])
    return None


MAX_RETRY = 4
DAILY_RE = re.compile(r"per\s*day|perday|daily|\bRPD\b", re.I)
RETRY_HINT_RE = re.compile(r"retry\w*\D{0,12}(\d+(?:\.\d+)?)\s*s", re.I)


def retry_delay(e: Exception, attempt: int) -> float:
    """Berapa detik menunggu setelah 429: pakai petunjuk dari Google bila ada, jika tidak 5/10/20/40 detik."""
    m = RETRY_HINT_RE.search(str(e))
    if m:
        return min(90.0, float(m.group(1)) + 1.0)
    return min(60.0, 5.0 * (2 ** attempt))


class GeminiReader:
    """Satu pembaca yang BOLEH dipakai bersama banyak event/thread.

    - `rpm`: batas request per menit (jarak minimal antar permulaan request, dipakai bersama semua thread).
    - `concurrency`: jumlah panggilan yang boleh berjalan bersamaan (akun berbayar -> lebih cepat).
    - 429 per-menit: semua thread menunggu bersama (cooldown) lalu mencoba lagi; 429 harian: langsung berhenti.
    """

    def __init__(self, api_key: str, model: str, rpm: int = 13, concurrency: int = 1,
                 clock=time.monotonic, sleep=time.sleep):
        from google import genai
        from google.genai import types
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.rpm, self.concurrency = max(1, int(rpm)), max(1, int(concurrency))
        self.min_interval = 60.0 / self.rpm
        self._sem = threading.BoundedSemaphore(self.concurrency)
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._cooldown_until = 0.0
        self._clock, self._sleep = clock, sleep
        self.off = False                       # True bila kuota harian habis / model tidak tersedia

    def _wait_for_slot(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next_slot, self._cooldown_until)
            self._next_slot = start + self.min_interval
        if start > now:
            self._sleep(start - now)

    def _cool_down(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    def extract(self, img: bytes, mime: str) -> Extracted:
        types = self._types
        parts = [types.Part.from_bytes(data=img, mime_type=mime or "image/jpeg"), build_prompt()]
        cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)
        resp = None
        for attempt in range(MAX_RETRY + 1):
            with self._sem:
                self._wait_for_slot()
                try:
                    resp = self.client.models.generate_content(model=self.model, contents=parts, config=cfg)
                    break
                except Exception as e:
                    cls = classify_error(e)
                    if isinstance(cls, GeminiQuotaError) and not DAILY_RE.search(str(e)) and attempt < MAX_RETRY:
                        self._cool_down(retry_delay(e, attempt))      # batas per menit: tunggu lalu coba lagi
                        continue
                    if cls:
                        raise cls from e
                    raise
        raw = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
        return self._to_extracted(json.loads(raw))

    def ping(self) -> str:
        """Uji cepat key + model. Melempar GeminiQuotaError / GeminiModelError / galat lain."""
        try:
            r = self.client.models.generate_content(model=self.model, contents="Balas hanya dengan satu kata: OK")
        except Exception as e:
            cls = classify_error(e)
            if cls:
                raise cls from e
            raise
        return (r.text or "").strip()[:40]

    def available_models(self) -> list[str]:
        """Nama model Flash yang bisa dipakai API key ini (untuk membantu memilih saat model berganti)."""
        out = []
        for m in self.client.models.list():
            name = str(getattr(m, "name", "")).replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if "gemini" not in name or "flash" not in name:
                continue
            if any(x in name for x in ("image", "live", "tts", "audio", "embedding", "native", "computer")):
                continue
            if actions and "generateContent" not in actions:
                continue
            out.append(name)
        return sorted(set(out))[:15]

    @staticmethod
    def _to_extracted(d: dict) -> Extracted:
        ext = Extracted(source="gemini", conf=100.0)
        nominal = parse_amount(str(d["nominal"])) if d.get("nominal") not in (None, "") else None
        if nominal:
            ext.amounts = [Amount(nominal, True, "primary", False)]
        if d.get("tanggal"):
            try:
                ext.dt = datetime.strptime(d["tanggal"], "%Y-%m-%d")
                if d.get("jam"):
                    hh, mm = str(d["jam"]).split(":")[:2]
                    ext.dt = ext.dt.replace(hour=int(hh), minute=int(mm))
                    ext.has_time = True
            except (ValueError, TypeError):
                ext.dt = None
        if d.get("nomor_referensi"):
            ext.ref = normalize_ref(str(d["nomor_referensi"])) or None
        st = (d.get("status_transaksi") or "").lower()
        ext.success, ext.failed = st == "berhasil", st in ("gagal", "pending")
        ext.not_proof = d.get("apakah_bukti_transfer") is False
        ext.blurry = (d.get("kualitas_gambar") or "").lower() == "buram"
        anomaly = d.get("kejanggalan") or None
        ext.anomaly = None if (anomaly and FUTURE_RE.search(str(anomaly))) else anomaly   # tanggal dinilai oleh aturan, bukan AI
        # teks sintetis agar pencocok rekening/nama yang sama bisa dipakai
        ext.text = " ".join(str(d.get(k) or "") for k in ("bank_tujuan", "rekening_tujuan", "nama_penerima"))
        return ext
