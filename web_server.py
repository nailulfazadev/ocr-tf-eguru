import os
import json
import asyncio
import threading
from typing import Optional, Dict, Any, List
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import Config, Store, APP_DIR, parse_spreadsheet_id, parse_gid
from app.sheets import SheetsClient, DriveClient, get_credentials, find_service_account
from app.pipeline import Engine, load_review_queue, apply_manual
from app.gemini import GeminiReader

def _make_sheets(spreadsheet_id: str, sheet_title: str = "", sheet_id: int = 0) -> SheetsClient:
    """Buat SheetsClient dengan credentials otomatis."""
    creds = get_credentials(allow_browser=False)
    client = SheetsClient(creds, spreadsheet_id, sheet_title, sheet_id)
    if not sheet_title and spreadsheet_id:
        try:
            tabs = client.list_tabs()
            if tabs:
                client.title = tabs[0][0]
                client.sheet_id = tabs[0][1]
        except Exception:
            pass
    return client

def _make_drive() -> DriveClient:
    """Buat DriveClient dengan credentials otomatis."""
    creds = get_credentials(allow_browser=False)
    return DriveClient(creds)

def test_gemini_key(key: str, model: str = "gemini-3.5-flash-lite") -> tuple[bool, str, list[str]]:
    try:
        r = GeminiReader(key, model)
        msg = r.ping()
        models = r.available_models()
        return True, f"Kunci Gemini aktif ({msg})", models
    except Exception as e:
        return False, f"Tes gagal: {e}", []

app = FastAPI(title="Cek Bukti TF e-guru.id - Web Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# State manager
class AppState:
    def __init__(self):
        self.engines: Dict[str, Any] = {}          # event_name -> Engine
        self.threads: Dict[str, threading.Thread] = {}
        self.running_events: Dict[str, bool] = {}   # event_name -> is_running
        self.paused_events: Dict[str, bool] = {}
        self.event_progress: Dict[str, dict] = {}   # event_name -> {"done": 0, "total": 0, "percent": 0}
        self.recent_logs: List[dict] = []
        self.recent_results: List[Dict[str, Any]] = []
        self.ws_clients: List[WebSocket] = []
        self.lock = threading.Lock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def add_log(self, event_name: str, msg: str):
        with self.lock:
            self.recent_logs.append({"event": event_name, "message": msg})
            if len(self.recent_logs) > 300:
                self.recent_logs.pop(0)

    def add_result(self, event_name: str, res_dict: Dict[str, Any]):
        with self.lock:
            res_dict["event_name"] = event_name
            self.recent_results.append(res_dict)
            if len(self.recent_results) > 150:
                self.recent_results.pop(0)

    async def broadcast(self, data: dict):
        with self.lock:
            clients = list(self.ws_clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        if dead:
            with self.lock:
                for d in dead:
                    if d in self.ws_clients:
                        self.ws_clients.remove(d)

state = AppState()

@app.on_event("startup")
async def startup_event():
    state.loop = asyncio.get_running_loop()

def sync_broadcast(data: dict):
    if state.loop is None:
        try:
            state.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
    if state.loop and state.loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(state.broadcast(data), state.loop)
        except Exception as e:
            print("Broadcast error:", e)

# --- API Endpoints ---

@app.get("/api/config")
def get_config(event_name: Optional[str] = None):
    try:
        s = Store.load()
        name = event_name or s.last_event or (s.names()[0] if s.names() else "Event 1")
        cfg = s.config_for(name)
        return {
            "events": s.names(),
            "current_event": name,
            "config": cfg.__dict__,
            "has_credentials": bool(find_service_account()) or (APP_DIR / "client_secret.json").exists() or (Path.cwd() / "client_secret.json").exists(),
            "is_running": bool(state.running_events.get(name)),
            "is_paused": bool(state.paused_events.get(name)),
            "progress": state.event_progress.get(name, {"done": 0, "total": 0, "percent": 0}),
            "tabs": [],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat config: {str(e)}")

class SaveConfigRequest(BaseModel):
    event_name: str
    config: Dict[str, Any]

@app.post("/api/config")
def save_config(req: SaveConfigRequest):
    s = Store.load()
    cfg = s.config_for(req.event_name)
    for k, v in req.config.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    # Auto-derive spreadsheet_id & gid dari URL jika ada
    if cfg.spreadsheet_url:
        sid = parse_spreadsheet_id(cfg.spreadsheet_url)
        if sid:
            cfg.spreadsheet_id = sid
        gid = parse_gid(cfg.spreadsheet_url)
        if gid is not None:
            cfg.sheet_id = gid
    s.update_global(cfg)
    s.update_event(cfg)
    s.save()
    return {"status": "saved", "name": req.event_name}

@app.post("/api/event/add")
def add_event(payload: Dict[str, str]):
    name = payload.get("name", "").strip()
    s = Store.load()
    new_name = s.add(name or None)
    s.last_event = new_name
    s.save()
    return {"status": "created", "name": new_name}

@app.post("/api/event/rename")
def rename_event(payload: Dict[str, str]):
    old_name = payload.get("old_name", "").strip()
    new_name = payload.get("new_name", "").strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Nama event tidak boleh kosong")
    s = Store.load()
    try:
        result = s.rename(old_name, new_name)
        s.save()
        return {"status": "renamed", "name": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/event/delete")
def delete_event(payload: Dict[str, str]):
    name = payload.get("name", "").strip()
    s = Store.load()
    try:
        s.delete(name)
        s.save()
        return {"status": "deleted", "current": s.last_event}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/sheets/connect")
def connect_sheets(payload: Dict[str, str]):
    url = payload.get("url", "")
    sheet_id = parse_spreadsheet_id(url)
    if not sheet_id:
        raise HTTPException(status_code=400, detail="URL spreadsheet tidak valid")
    try:
        client = _make_sheets(sheet_id)
        tabs = client.list_tabs()
        title = client.get_spreadsheet_title()
        return {
            "status": "ok",
            "spreadsheet_id": sheet_id,
            "title": title,
            "tabs": [{"title": t, "id": i, "rows": r} for t, i, r in tabs]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal menghubungkan: {str(e)}")

@app.post("/api/gemini/test")
def test_gemini(payload: Dict[str, str]):
    key = payload.get("key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key Gemini kosong")
    ok, msg, models = test_gemini_key(key)
    return {"ok": ok, "message": msg, "models": models}

@app.post("/api/process/start")
def start_process(payload: Dict[str, str]):
    event_name = payload.get("event_name")
    s = Store.load()
    name = event_name or s.last_event or (s.names()[0] if s.names() else "Event 1")
    cfg = s.config_for(name)

    if state.running_events.get(name):
        return {"status": "already_running", "event": name}
    
    if not cfg.spreadsheet_url and not cfg.spreadsheet_id:
        raise HTTPException(
            status_code=400,
            detail=f"Spreadsheet untuk event '{name}' belum diatur. Buka tab 'Pengaturan', isi URL Spreadsheet Google Sheets, klik 'Hubungkan', lalu 'Simpan Pengaturan Event'."
        )
    
    if not cfg.spreadsheet_id and cfg.spreadsheet_url:
        cfg.spreadsheet_id = parse_spreadsheet_id(cfg.spreadsheet_url)
    
    if not cfg.spreadsheet_id:
        raise HTTPException(
            status_code=400,
            detail="URL Spreadsheet tidak valid. Pastikan link spreadsheet Google Sheets sudah benar."
        )
    
    try:
        sheets_client = _make_sheets(cfg.spreadsheet_id, cfg.sheet_title, cfg.sheet_id)
        drive_client = _make_drive()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Koneksi Google gagal: {str(e)}")

    def on_log(msg: str):
        state.add_log(name, msg)
        sync_broadcast({"type": "log", "event_name": name, "message": msg})

    def on_progress(done: int, total: int):
        pct = int((done / total * 100)) if total > 0 else 0
        state.event_progress[name] = {"done": done, "total": total, "percent": pct}
        sync_broadcast({"type": "progress", "event_name": name, "done": done, "total": total, "percent": pct})

    def on_result(res):
        ext_dict = None
        if getattr(res, "ext", None):
            ext = res.ext
            prim_val = ext.primary.value if ext.primary else None
            dt_str = ext.dt.strftime("%d/%m/%Y %H:%M") if ext.dt else None
            ext_dict = {
                "nominal": prim_val,
                "date": dt_str,
                "ref": ext.ref,
                "source": ext.source,
                "conf": ext.conf,
            }
        item = {
            "row": res.row.row,
            "nama": res.row.nama,
            "nominal_target": res.row.nominal_raw,
            "status": res.status,
            "reasons": getattr(res.verdict, "reasons", []),
            "infos": getattr(res.verdict, "infos", []),
            "note": res.note,
            "extracted": ext_dict,
        }
        state.add_result(name, item)
        sync_broadcast({"type": "result", "event_name": name, "item": item})

    engine = Engine(cfg, sheets_client, drive_client, on_result=on_result, on_log=on_log, on_progress=on_progress)
    state.engines[name] = engine
    state.running_events[name] = True
    state.paused_events[name] = False
    state.event_progress[name] = {"done": 0, "total": 0, "percent": 0}
    sync_broadcast({"type": "status", "event_name": name, "is_running": True, "is_paused": False})

    def worker():
        try:
            on_log(f"Memulai proses event: {cfg.name}")
            engine.run()
        except Exception as e:
            on_log(f"Error eksekusi: {e}")
        finally:
            state.running_events[name] = False
            state.paused_events[name] = False
            state.engines.pop(name, None)
            sync_broadcast({"type": "status", "event_name": name, "is_running": False, "is_paused": False})

    t = threading.Thread(target=worker, daemon=True)
    state.threads[name] = t
    t.start()
    return {"status": "started", "event": name}

@app.post("/api/process/pause")
def pause_process(payload: Dict[str, str] = None):
    event_name = (payload or {}).get("event_name")
    s = Store.load()
    name = event_name or s.last_event
    engine = state.engines.get(name)
    if engine and state.running_events.get(name):
        engine.pause()
        state.paused_events[name] = True
        sync_broadcast({"type": "status", "event_name": name, "is_running": True, "is_paused": True})
        return {"status": "paused", "event": name}
    return {"status": "idle"}

@app.post("/api/process/resume")
def resume_process(payload: Dict[str, str] = None):
    event_name = (payload or {}).get("event_name")
    s = Store.load()
    name = event_name or s.last_event
    engine = state.engines.get(name)
    if engine and state.running_events.get(name):
        engine.resume()
        state.paused_events[name] = False
        sync_broadcast({"type": "status", "event_name": name, "is_running": True, "is_paused": False})
        return {"status": "resumed", "event": name}
    return {"status": "idle"}

@app.post("/api/process/stop")
def stop_process(payload: Dict[str, str] = None):
    event_name = (payload or {}).get("event_name")
    s = Store.load()
    name = event_name or s.last_event
    engine = state.engines.get(name)
    if engine:
        engine.stop()
        return {"status": "stopping", "event": name}
    return {"status": "idle"}

# --- Review Manual Queue ---

@app.get("/api/review/queue")
def get_review_queue(event_name: Optional[str] = None, mode: str = "manual"):
    s = Store.load()
    name = event_name if event_name and event_name != "undefined" else s.last_event
    cfg = s.config_for(name or (s.names()[0] if s.names() else "Event 1"))
    if not cfg.spreadsheet_id and cfg.spreadsheet_url:
        cfg.spreadsheet_id = parse_spreadsheet_id(cfg.spreadsheet_url)
    if not cfg.spreadsheet_id:
        return {"items": [], "count": 0, "message": "Spreadsheet belum diatur"}
    try:
        sheets_client = _make_sheets(cfg.spreadsheet_id, cfg.sheet_title, cfg.sheet_id)
        queue = load_review_queue(sheets_client, cfg, mode=mode)
        items = []
        from app.parser import parse_amount
        for r in queue:
            parsed_nom = parse_amount(r.nominal_raw)
            items.append({
                "row": r.row,
                "nama": r.nama,
                "nominal": parsed_nom if parsed_nom is not None else r.nominal_raw,
                "nominal_raw": r.nominal_raw,
                "rekening": r.rekening_raw,
                "link": r.link_raw,
                "timestamp": r.timestamp_raw,
                "check": r.check,
                "note": r.note,
                "ref": r.ref
            })
        return {"items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class ManualDecisionRequest(BaseModel):
    event_name: str
    row: int
    note: str
    ok: bool
    officer: str
    reason: Optional[str] = ""

@app.post("/api/review/decision")
def post_manual_decision(req: ManualDecisionRequest):
    s = Store.load()
    cfg = s.config_for(req.event_name)
    sheets_client = _make_sheets(cfg.spreadsheet_id, cfg.sheet_title, cfg.sheet_id)
    from app.models import SheetRow
    row_dummy = SheetRow(row=req.row, link_raw="", nominal_raw="", rekening_raw="", nama="", timestamp_raw="", check=False, note=req.note)
    try:
        note_result = apply_manual(sheets_client, cfg, row_dummy, req.ok, req.officer or "petugas", req.reason or "")
        return {"status": "ok", "note": note_result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Upload Credential File
@app.post("/api/credentials/upload")
async def upload_credential(file: UploadFile = File(...)):
    name = file.filename
    content = await file.read()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="File harus berupa file JSON yang valid")

    if "type" in data and data["type"] == "service_account":
        target = APP_DIR / "service_account.json"
        target.write_bytes(content)
        try:
            (Path.cwd() / "service_account.json").write_bytes(content)
        except Exception:
            pass
        email = data.get("client_email", "")
        return {
            "status": "ok",
            "type": "service_account",
            "email": email,
            "message": f"Service Account berhasil disimpan ({email}). Pastikan spreadsheet telah dibagikan ke email ini sebagai Editor!"
        }
    elif "installed" in data or "web" in data:
        target = APP_DIR / "client_secret.json"
        target.write_bytes(content)
        return {
            "status": "ok",
            "type": "client_secret",
            "message": "File adalah OAuth Client Secret. Perhatian: Untuk VPS/Web Server, disarankan service_account.json agar tidak memerlukan login browser."
        }
    else:
        raise HTTPException(status_code=400, detail="Format JSON tidak dikenali (harus service account atau OAuth client)")

# Drive Image Proxy for Web UI
@app.get("/api/drive/image")
def get_drive_image(file_id: str):
    if not file_id:
        raise HTTPException(status_code=400, detail="file_id wajib diisi")
    try:
        drive_client = _make_drive()
        data, mime = drive_client.fetch(file_id)
        from fastapi.responses import Response
        return Response(content=data, media_type=mime)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# WebSocket Endpoint for Realtime Dashboard
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if state.loop is None:
        try:
            state.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
    await ws.accept()
    with state.lock:
        state.ws_clients.append(ws)
    await ws.send_json({
        "type": "init",
        "running_events": state.running_events,
        "paused_events": state.paused_events,
        "event_progress": state.event_progress,
        "recent_logs": [l["message"] for l in state.recent_logs[-50:]],
        "recent_results": state.recent_results[-30:],
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        with state.lock:
            if ws in state.ws_clients:
                state.ws_clients.remove(ws)
    except Exception:
        with state.lock:
            if ws in state.ws_clients:
                state.ws_clients.remove(ws)

# Web UI Main HTML
@app.get("/", response_class=HTMLResponse)
def index():
    html_file = Path(__file__).parent / "web_static" / "index.html"
    content = html_file.read_text(encoding="utf-8") if html_file.exists() else "<h1>Web UI tidak ditemukan</h1>"
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=True)
