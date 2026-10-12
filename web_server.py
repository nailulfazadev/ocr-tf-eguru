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
from app.sheets import SheetsClient, DriveClient
from app.pipeline import Engine, load_review_queue, apply_manual
from app.gemini import GeminiReader, test_gemini_key

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
        self.store = Store.load()
        self.active_engine: Optional[Engine] = None
        self.engine_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.is_paused = False
        self.progress = {"done": 0, "total": 0, "percent": 0}
        self.recent_logs: List[str] = []
        self.recent_results: List[Dict[str, Any]] = []
        self.ws_clients: List[WebSocket] = []
        self.lock = threading.Lock()

    def add_log(self, msg: str):
        with self.lock:
            self.recent_logs.append(msg)
            if len(self.recent_logs) > 300:
                self.recent_logs.pop(0)

    def add_result(self, res_dict: Dict[str, Any]):
        with self.lock:
            self.recent_results.append(res_dict)
            if len(self.recent_results) > 150:
                self.recent_results.pop(0)

    async def broadcast(self, data: dict):
        dead_clients = []
        for ws in self.ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead_clients.append(ws)
        for dead in dead_clients:
            if dead in self.ws_clients:
                self.ws_clients.remove(dead)

state = AppState()
loop = asyncio.get_event_loop()

def sync_broadcast(data: dict):
    try:
        asyncio.run_coroutine_threadsafe(state.broadcast(data), loop)
    except Exception:
        pass

# --- API Endpoints ---

@app.get("/api/config")
def get_config(event_name: Optional[str] = None):
    s = Store.load()
    name = event_name or s.last_event or (s.names()[0] if s.names() else "Event 1")
    cfg = s.config_for(name)
    return {
        "events": s.names(),
        "current_event": name,
        "config": cfg.__dict__,
        "has_credentials": (APP_DIR / "service_account.json").exists() or (APP_DIR / "client_secret.json").exists(),
        "is_running": state.is_running,
        "is_paused": state.is_paused,
        "progress": state.progress
    }

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
    cfg.save()
    return {"status": "saved", "name": req.event_name}

@app.post("/api/event/add")
def add_event(payload: Dict[str, str]):
    name = payload.get("name", "").strip()
    s = Store.load()
    new_name = s.add(name or None)
    s.last_event = new_name
    s.save()
    return {"status": "created", "name": new_name}

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
        client = SheetsClient(sheet_id)
        tabs = client.list_tabs()
        return {
            "status": "ok",
            "spreadsheet_id": sheet_id,
            "title": client.spreadsheet_title(),
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
    if state.is_running:
        return {"status": "already_running"}
    
    event_name = payload.get("event_name")
    s = Store.load()
    cfg = s.config_for(event_name or s.last_event)
    
    try:
        sheets_client = SheetsClient(cfg.spreadsheet_id)
        drive_client = DriveClient()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Koneksi Google gagal: {str(e)}")

    def on_log(msg: str):
        state.add_log(msg)
        sync_broadcast({"type": "log", "message": msg})

    def on_progress(done: int, total: int):
        pct = int((done / total * 100)) if total > 0 else 0
        state.progress = {"done": done, "total": total, "percent": pct}
        sync_broadcast({"type": "progress", "done": done, "total": total, "percent": pct})

    def on_result(res):
        ext_dict = None
        if res.extracted:
            ext_dict = {
                "nominal": res.extracted.primary.value if res.extracted.primary else None,
                "date": res.extracted.dt.strftime("%d/%m/%Y %H:%M") if res.extracted.dt else None,
                "ref": res.extracted.ref,
                "dest": res.extracted.dest.raw if res.extracted.dest else None,
            }
        item = {
            "row": res.row.row,
            "nama": res.row.nama,
            "nominal_target": res.row.nominal,
            "status": res.verdict.status,
            "reasons": res.verdict.reasons,
            "infos": res.verdict.infos,
            "note": res.note,
            "extracted": ext_dict,
        }
        state.add_result(item)
        sync_broadcast({"type": "result", "item": item})

    engine = Engine(cfg, sheets_client, drive_client, on_result=on_result, on_log=on_log, on_progress=on_progress)
    state.active_engine = engine
    state.is_running = True
    state.is_paused = False
    state.progress = {"done": 0, "total": 0, "percent": 0}

    def worker():
        try:
            on_log(f"Memulai proses event: {cfg.name}")
            engine.run()
        except Exception as e:
            on_log(f"Error eksekusi: {e}")
        finally:
            state.is_running = False
            state.is_paused = False
            state.active_engine = None
            sync_broadcast({"type": "status", "is_running": False, "is_paused": False})

    t = threading.Thread(target=worker, daemon=True)
    state.engine_thread = t
    t.start()
    return {"status": "started"}

@app.post("/api/process/pause")
def pause_process():
    if state.active_engine and state.is_running:
        state.active_engine.pause()
        state.is_paused = True
        sync_broadcast({"type": "status", "is_running": True, "is_paused": True})
        return {"status": "paused"}
    return {"status": "idle"}

@app.post("/api/process/resume")
def resume_process():
    if state.active_engine and state.is_running:
        state.active_engine.resume()
        state.is_paused = False
        sync_broadcast({"type": "status", "is_running": True, "is_paused": False})
        return {"status": "resumed"}
    return {"status": "idle"}

@app.post("/api/process/stop")
def stop_process():
    if state.active_engine and state.is_running:
        state.active_engine.stop()
        state.is_running = False
        state.is_paused = False
        sync_broadcast({"type": "status", "is_running": False, "is_paused": False})
        return {"status": "stopping"}
    return {"status": "idle"}

# --- Review Manual Queue ---

@app.get("/api/review/queue")
def get_review_queue(event_name: Optional[str] = None, mode: str = "manual"):
    s = Store.load()
    cfg = s.config_for(event_name or s.last_event)
    try:
        sheets_client = SheetsClient(cfg.spreadsheet_id)
        queue = load_review_queue(sheets_client, cfg, mode=mode)
        items = []
        for r in queue:
            items.append({
                "row": r.row,
                "nama": r.nama,
                "nominal": r.nominal,
                "rekening": r.rekening_raw,
                "link": r.link_raw,
                "timestamp": r.timestamp,
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
    sheets_client = SheetsClient(cfg.spreadsheet_id)
    from app.models import SheetRow
    row_dummy = SheetRow(row=req.row, link_raw="", nominal=0, rekening_raw="", nama="", timestamp="", check=False, note=req.note)
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
        return {"status": "ok", "type": "service_account", "message": "Service Account berhasil disimpan"}
    elif "installed" in data or "web" in data:
        target = APP_DIR / "client_secret.json"
        target.write_bytes(content)
        return {"status": "ok", "type": "client_secret", "message": "OAuth Client Secret berhasil disimpan"}
    else:
        raise HTTPException(status_code=400, detail="Format JSON kredensial Google tidak dikenali")

# WebSocket Endpoint for Realtime Dashboard
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_clients.append(ws)
    await ws.send_json({
        "type": "init",
        "is_running": state.is_running,
        "is_paused": state.is_paused,
        "progress": state.progress,
        "recent_logs": state.recent_logs[-50:],
        "recent_results": state.recent_results[-30:],
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in state.ws_clients:
            state.ws_clients.remove(ws)

# Web UI Main HTML
@app.get("/", response_class=HTMLResponse)
def index():
    html_file = Path(__file__).parent / "web_static" / "index.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    return "<h1>Web UI file tidak ditemukan</h1>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=True)
