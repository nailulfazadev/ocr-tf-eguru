"""Akses Google Sheets & Drive memakai login Google tiap anggota tim (OAuth)."""
from __future__ import annotations
import io
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .config import (APP_DIR, CACHE_DIR, CLIENT_SECRET_PATH, SERVICE_ACCOUNT_PATH, TOKEN_PATH, Config,
                     col_to_idx, idx_to_col)
from .models import SheetRow

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive.readonly"]


class DriveAccessError(Exception):
    pass


def retry(fn, tries: int = 5):
    delay = 1.5
    for i in range(tries):
        try:
            return fn()
        except HttpError as e:
            if getattr(e.resp, "status", 0) in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(delay); delay *= 2
                continue
            raise
        except (socket.timeout, ConnectionError, OSError):
            if i < tries - 1:
                time.sleep(delay); delay *= 2
                continue
            raise


def find_client_secret() -> Path:
    cands = [CLIENT_SECRET_PATH,
             Path(sys.argv[0]).resolve().parent / "client_secret.json",
             Path(getattr(sys, "_MEIPASS", ".")) / "client_secret.json",
             Path.cwd() / "client_secret.json"]
    for c in cands:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"File client_secret.json tidak ditemukan. Letakkan di: {CLIENT_SECRET_PATH} (lihat README langkah 1).")


def find_service_account() -> Path | None:
    for c in (SERVICE_ACCOUNT_PATH, Path(sys.argv[0]).resolve().parent / "service_account.json",
              Path(getattr(sys, "_MEIPASS", ".")) / "service_account.json", Path.cwd() / "service_account.json"):
        if c.exists():
            return c
    return None


def get_credentials(allow_browser: bool = True):
    """Prioritas: service_account.json (tanpa login). Jika tidak ada: login Google per orang (OAuth)."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    sa = find_service_account()
    if sa:
        return service_account.Credentials.from_service_account_file(str(sa), scopes=SCOPES)
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except RefreshError:
            pass
    if not allow_browser:
        raise ValueError(
            "File 'service_account.json' belum ditemukan. Di server VPS / web, Google mewajibkan file Service Account "
            "agar tidak perlu login browser. Silakan unggah service_account.json di tab 'Kunci & API'."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(find_client_secret()), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")   # membuka browser untuk login
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def install_key(path: str) -> tuple[str, str]:
    """Salin file kunci Google yang dipilih pengguna ke ~/.bukti_tf dengan nama yang benar.
    Mengembalikan (jenis, info): ("service_account", email) atau ("oauth", "")."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError("File itu bukan JSON yang valid.") from e
    if not isinstance(data, dict):
        raise ValueError("Ini bukan file kunci Google.")
    if data.get("type") == "service_account" and data.get("private_key"):
        dest, kind, info = SERVICE_ACCOUNT_PATH, "service_account", str(data.get("client_email", ""))
    elif "installed" in data or "web" in data:
        dest, kind, info = CLIENT_SECRET_PATH, "oauth", ""
    else:
        raise ValueError("Ini bukan file kunci Google (service account atau OAuth client).")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return kind, info


def key_status() -> str:
    sa = find_service_account()
    if sa:
        try:
            return f"\u2705 Service account: {json.loads(sa.read_text(encoding='utf-8')).get('client_email', '?')}"
        except Exception:
            return "\u26A0 service_account.json ada tetapi tidak terbaca"
    try:
        find_client_secret()
        return "\u2705 client_secret.json (login Google per orang)"
    except FileNotFoundError:
        return "\u26A0 Belum ada kunci Google. Klik 'Pilih file kunci...'"


def extract_file_ids(text: str) -> list[str]:
    ids = re.findall(r"[?&]id=([A-Za-z0-9_-]{20,})", text) + re.findall(r"/d/([A-Za-z0-9_-]{20,})", text)
    return list(dict.fromkeys(ids))


class SheetsClient:
    def __init__(self, creds: Credentials, spreadsheet_id: str, sheet_title: str = "", sheet_id: int = 0):
        self.id = spreadsheet_id
        self.title = sheet_title
        self.sheet_id = sheet_id
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._lock = threading.Lock()      # objek http googleapiclient tidak thread-safe

    def _q(self, a1: str) -> str:
        return f"'{self.title.replace(chr(39), chr(39) * 2)}'!{a1}"

    def get_spreadsheet_title(self) -> str:
        try:
            with self._lock:
                r = retry(lambda: self._svc.spreadsheets().get(
                    spreadsheetId=self.id,
                    fields="properties.title").execute())
            return r.get("properties", {}).get("title", self.id)
        except Exception:
            return self.id

    def list_tabs(self) -> list[tuple[str, int, int]]:
        with self._lock:
            r = retry(lambda: self._svc.spreadsheets().get(
                spreadsheetId=self.id,
                fields="sheets(properties(sheetId,title,gridProperties(rowCount)))").execute())
        return [(s["properties"]["title"], s["properties"]["sheetId"],
                 s["properties"]["gridProperties"]["rowCount"]) for s in r["sheets"]]

    def read_rows(self, cfg: Config) -> list[SheetRow]:
        idx = {k: col_to_idx(getattr(cfg, "col_" + k)) for k in
               ("link", "nominal", "rekening", "nama", "timestamp", "check", "note", "ref")}
        rng = self._q(f"A{cfg.first_row}:{idx_to_col(max(idx.values()))}")
        with self._lock:
            r = retry(lambda: self._svc.spreadsheets().values().get(
                spreadsheetId=self.id, range=rng, valueRenderOption="FORMATTED_VALUE").execute())
        rows = []
        for i, vals in enumerate(r.get("values", [])):
            if not any(str(v).strip() for v in vals):
                continue
            g = lambda k: str(vals[idx[k]]).strip() if idx[k] < len(vals) else ""
            rows.append(SheetRow(
                row=cfg.first_row + i, nama=g("nama"), nominal_raw=g("nominal"), rekening_raw=g("rekening"),
                link_raw=g("link"), timestamp_raw=g("timestamp"), note=g("note"), ref=g("ref"),
                check=g("check").upper() in ("TRUE", "1", "YA", "\u2713")))
        return rows

    def read_cells(self, a1s: list[str]) -> list[str]:
        with self._lock:
            r = retry(lambda: self._svc.spreadsheets().values().batchGet(
                spreadsheetId=self.id, ranges=[self._q(a) for a in a1s]).execute())
        out = []
        for vr in r.get("valueRanges", []):
            v = vr.get("values")
            out.append(str(v[0][0]) if v and v[0] else "")
        return out

    def batch_write(self, pairs: list[tuple[str, object]]) -> None:
        if not pairs:
            return
        body = {"valueInputOption": "RAW",
                "data": [{"range": self._q(a), "values": [[v]]} for a, v in pairs]}
        with self._lock:
            retry(lambda: self._svc.spreadsheets().values().batchUpdate(spreadsheetId=self.id, body=body).execute())

    def highlight_cells(self, cells: list[tuple[int, str]], rgb: tuple[float, float, float] = (1.0, 0.95, 0.4)) -> None:
        """Beri warna latar (default kuning) pada sel-sel [(nomor_baris, huruf_kolom)]."""
        if not cells:
            return
        reqs = []
        for row, col in cells:
            ci = col_to_idx(col)
            reqs.append({"repeatCell": {
                "range": {"sheetId": self.sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": ci, "endColumnIndex": ci + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}}},
                "fields": "userEnteredFormat.backgroundColor"}})
        with self._lock:
            retry(lambda: self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.id, body={"requests": reqs}).execute())

    def ensure_checkbox(self, cfg: Config, row_count: int) -> None:
        ci = col_to_idx(cfg.col_check)
        req = {"setDataValidation": {
            "range": {"sheetId": self.sheet_id, "startRowIndex": cfg.first_row - 1, "endRowIndex": row_count,
                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}}
        with self._lock:
            retry(lambda: self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.id, body={"requests": [req]}).execute())


class DriveClient:
    def __init__(self, creds: Credentials):
        self.creds = creds
        self._tl = threading.local()

    def _svc(self):
        if not hasattr(self._tl, "svc"):
            self._tl.svc = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        return self._tl.svc

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        """Ambil gambar ke memori (dan cache di disk agar layar review tidak mengunduh ulang)."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data_p, mime_p = CACHE_DIR / f"{file_id}.bin", CACHE_DIR / f"{file_id}.mime"
        if data_p.exists() and mime_p.exists():
            return data_p.read_bytes(), mime_p.read_text()
        svc = self._svc()

        def _download():
            meta = svc.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id, supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk()
            return buf.getvalue(), meta.get("mimeType", "application/octet-stream")

        try:
            data, mime = retry(_download)
        except HttpError as e:
            if getattr(e.resp, "status", 0) in (403, 404):
                raise DriveAccessError("tidak punya akses ke file bukti (cek izin Drive akun Anda)") from e
            raise
        data_p.write_bytes(data)
        mime_p.write_text(mime)
        return data, mime
