"""Login sederhana untuk versi web (cookie bertanda tangan HMAC).

Aktif hanya bila password diatur:
  - variabel lingkungan APP_PASSWORD, atau
  - file ~/.bukti_tf/web_password (isi satu baris = password)
Bila tidak diatur, semua halaman terbuka seperti sebelumnya.
Mengganti password otomatis mengeluarkan semua sesi lama.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
from http.cookies import SimpleCookie

from starlette.responses import JSONResponse, RedirectResponse

from .config import APP_DIR

COOKIE = "tf_session"
MAX_AGE = 30 * 24 * 3600                       # 30 hari
PASSWORD_FILE = APP_DIR / "web_password"
SECRET_FILE = APP_DIR / "web_secret"
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico"}

_cache: dict = {"mtime": None, "pw": ""}


def get_password() -> str:
    env = os.environ.get("APP_PASSWORD", "").strip()
    if env:
        return env
    try:
        m = PASSWORD_FILE.stat().st_mtime
    except OSError:
        return ""
    if _cache["mtime"] != m:                    # baca ulang hanya bila file berubah
        _cache["mtime"] = m
        _cache["pw"] = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    return _cache["pw"]


def enabled() -> bool:
    return bool(get_password())


_secret_cache: list[bytes] = []


def _secret() -> bytes:
    if _secret_cache:
        return _secret_cache[0]
    try:
        s = SECRET_FILE.read_bytes()
    except OSError:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        s = secrets.token_bytes(32)
        SECRET_FILE.write_bytes(s)
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
    _secret_cache.append(s)
    return s


def make_token() -> str:
    return hmac.new(_secret(), get_password().encode(), hashlib.sha256).hexdigest()


def check_password(pw: str) -> bool:
    real = get_password()
    return bool(real) and hmac.compare_digest(pw.encode(), real.encode())


def _token_from_scope(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            c = SimpleCookie()
            try:
                c.load(v.decode("latin-1"))
            except Exception:
                return ""
            if COOKIE in c:
                return c[COOKIE].value
    return ""


def is_authed(scope) -> bool:
    tok = _token_from_scope(scope)
    return bool(tok) and hmac.compare_digest(tok, make_token())


class AuthMiddleware:
    """Middleware ASGI murni: melindungi HTTP dan WebSocket sekaligus."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not enabled():
            return await self.app(scope, receive, send)
        if scope.get("path") in PUBLIC_PATHS or is_authed(scope):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()                         # websocket.connect
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope["path"].startswith("/api/"):
            resp = JSONResponse({"detail": "Silakan login dulu."}, status_code=401)
        else:
            resp = RedirectResponse("/login", status_code=303)
        await resp(scope, receive, send)


def mask_key(key: str) -> str:
    if not key:
        return ""
    return (key[:4] + "•" * 8 + key[-4:]) if len(key) > 10 else "•" * 8


def is_masked(key: str) -> bool:
    return "•" in (key or "")


LOGIN_HTML = """<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Masuk · Cek Bukti TF e-guru.id</title>
<meta name="robots" content="noindex">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%2322d3ee'/%3E%3Cstop offset='1' stop-color='%236366f1'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url(%23g)'/%3E%3Ctext x='32' y='42' font-family='Arial' font-weight='700' font-size='26' fill='white' text-anchor='middle'%3ETF%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif;
 background:radial-gradient(1200px 600px at 10% -10%,rgba(34,211,238,.18),transparent 60%),
 radial-gradient(900px 500px at 110% 110%,rgba(99,102,241,.22),transparent 60%),#0b1020;color:#e5e7eb;padding:20px}
.card{width:100%;max-width:380px;background:rgba(17,24,39,.72);border:1px solid rgba(255,255,255,.08);border-radius:18px;
 padding:32px 28px;backdrop-filter:blur(16px);box-shadow:0 20px 60px rgba(0,0,0,.45);animation:up .5s ease}
@keyframes up{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.logo{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#22d3ee,#6366f1);display:flex;
 align-items:center;justify-content:center;font-weight:700;font-size:20px;color:#fff;margin:0 auto 16px;box-shadow:0 8px 24px rgba(99,102,241,.4)}
h1{font-size:19px;text-align:center;font-weight:600}
p.sub{text-align:center;color:#94a3b8;font-size:13px;margin:6px 0 24px}
label{font-size:12px;color:#94a3b8;font-weight:500;display:block;margin-bottom:6px}
.wrap{position:relative}
input{width:100%;padding:12px 42px 12px 14px;border-radius:10px;border:1px solid rgba(255,255,255,.1);background:rgba(2,6,23,.6);
 color:#e5e7eb;font-size:14px;outline:none;transition:border-color .2s,box-shadow .2s}
input:focus{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.25)}
.eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:0;color:#94a3b8;cursor:pointer;padding:6px}
button.go{width:100%;margin-top:18px;padding:12px;border:0;border-radius:10px;font-weight:600;font-size:14px;color:#fff;cursor:pointer;
 background:linear-gradient(135deg,#06b6d4,#6366f1);transition:transform .15s,box-shadow .2s;box-shadow:0 8px 24px rgba(99,102,241,.35)}
button.go:hover{transform:translateY(-1px);box-shadow:0 12px 28px rgba(99,102,241,.45)}
button.go:disabled{opacity:.6;cursor:wait;transform:none}
.err{color:#f87171;font-size:13px;text-align:center;margin-top:14px;min-height:18px}
</style></head><body>
<main class="card">
  <div class="logo">TF</div>
  <h1>Cek Bukti TF e-guru.id</h1>
  <p class="sub">Masukkan password untuk melanjutkan</p>
  <form id="f">
    <label for="pw">Password</label>
    <div class="wrap">
      <input id="pw" type="password" autocomplete="current-password" autofocus required>
      <button type="button" class="eye" id="eye" title="Tampilkan / Sembunyikan" aria-label="Tampilkan password">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
    </div>
    <button class="go" id="btnLogin" type="submit">Masuk</button>
    <div class="err" id="err"></div>
  </form>
</main>
<script>
const pw=document.getElementById('pw'),err=document.getElementById('err'),btn=document.getElementById('btnLogin');
document.getElementById('eye').onclick=()=>{pw.type=pw.type==='password'?'text':'password';pw.focus();};
document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();btn.disabled=true;err.textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw.value})});
    if(r.ok){location.href='/';return;}
    const d=await r.json().catch(()=>({}));err.textContent=d.detail||'Password salah';
  }catch(_){err.textContent='Tidak bisa terhubung ke server';}
  btn.disabled=false;pw.select();
};
</script></body></html>"""
