import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import threading
import pytest
from google.genai import types
import app.config as C
from app.gemini import GeminiReader, GeminiModelError, GeminiQuotaError, classify_error


class FakeClock:
    def __init__(self): self.t = 0.0; self.sleeps = []
    def now(self): return self.t
    def sleep(self, s): self.sleeps.append(s); self.t += s

def reader(client, rpm=6000, concurrency=1, clock=None, sleep=None):
    kw = {}
    if clock: kw["clock"] = clock
    if sleep: kw["sleep"] = sleep
    r = GeminiReader("k", "m", rpm=rpm, concurrency=concurrency, **kw)    # tidak ada akses jaringan saat dibuat
    r.client = client
    return r

class Models:
    def __init__(self, items=None, exc=None): self.items, self.exc = items or [], exc
    def generate_content(self, **kw):
        if self.exc: raise self.exc
        return type("R", (), {"text": "OK"})()
    def list(self): return self.items

class Client:
    def __init__(self, **kw): self.models = Models(**kw)

def M(name, actions=("generateContent",)): return type("M", (), {"name": "models/" + name, "supported_actions": list(actions)})()

def test_old_model_is_migrated_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "c.json")
    (tmp_path / "c.json").write_text(json.dumps({"gemini_model": "gemini-2.5-flash", "officer": "Budi"}))
    cfg = C.Config.load()
    assert cfg.gemini_model == "gemini-3.5-flash-lite" and cfg.officer == "Budi"
    (tmp_path / "c.json").write_text(json.dumps({"gemini_model": "gemini-3.8-flash"}))
    assert C.Config.load().gemini_model == "gemini-3.8-flash"        # pilihan model baru oleh pengguna tidak diubah

@pytest.mark.parametrize("msg,cls", [
    ("404 NOT_FOUND. models/gemini-2.5-flash is no longer available to new users", GeminiModelError),
    ("429 RESOURCE_EXHAUSTED quota exceeded", GeminiQuotaError)])
def test_extract_classifies_errors(msg, cls):
    clk = FakeClock()
    r = reader(Client(exc=Exception(msg)), clock=clk.now, sleep=clk.sleep)    # jam palsu: tidak menunggu sungguhan
    with pytest.raises(cls):
        r.extract(b"x", "image/png")

def test_unrelated_error_not_misclassified():
    assert classify_error(Exception("connection reset by peer")) is None

def test_ping_and_model_list_filters_noise():
    items = [M("gemini-3.5-flash-lite"), M("gemini-3.8-flash"), M("gemini-3.1-flash-image-preview"),
             M("gemini-3.1-flash-live-preview"), M("gemini-embedding-001", ("embedContent",)), M("gemma-4")]
    r = reader(Client(items=items))
    assert r.ping() == "OK"
    assert r.available_models() == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


# ---------- pembatas laju, panggilan bersamaan, coba ulang 429 ----------
import time

OK_JSON = json.dumps({"apakah_bukti_transfer": True, "status_transaksi": "berhasil", "nominal": 20000,
                      "tanggal": "2026-09-15", "kualitas_gambar": "jelas"})

class ScriptedModels:
    def __init__(self, script, delay=0.0, log=None):
        self.script, self.calls, self.delay, self.active, self.max_active = list(script), 0, delay, 0, 0
        self.starts = []; self.lock = threading.Lock(); self.log = log
    def generate_content(self, **kw):
        with self.lock:
            self.calls += 1; self.active += 1; self.max_active = max(self.max_active, self.active)
            step = self.script.pop(0) if self.script else None
            if self.log is not None: self.starts.append(self.log())
        try:
            if self.delay: time.sleep(self.delay)
            if isinstance(step, Exception): raise step
            return type("R", (), {"text": OK_JSON})()
        finally:
            with self.lock: self.active -= 1

def scripted(script, **kw):
    c = type("C", (), {})(); c.models = ScriptedModels(script, **kw); return c

def test_requests_are_spaced_by_rpm():
    clk = FakeClock()
    cl = scripted([], log=clk.now)
    r = reader(cl, rpm=60, clock=clk.now, sleep=clk.sleep)               # 60 rpm = 1 request per detik
    for _ in range(4): r.extract(b"x", "image/png")
    gaps = [b - a for a, b in zip(cl.models.starts, cl.models.starts[1:])]
    assert all(g >= 0.999 for g in gaps), gaps

def test_concurrency_limit_is_respected_and_allows_parallel():
    def peak(conc):
        cl = scripted([], delay=0.15)
        r = reader(cl, rpm=60000, concurrency=conc)
        ts = [threading.Thread(target=r.extract, args=(b"x", "image/png")) for _ in range(8)]
        t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]
        return cl.models.max_active, time.time() - t0
    p1, d1 = peak(1); p4, d4 = peak(4)
    assert p1 == 1 and 2 <= p4 <= 4
    assert d4 < d1 * 0.6                                                  # paralel jelas lebih cepat

def test_per_minute_429_waits_and_retries_instead_of_giving_up():
    clk = FakeClock()
    err = Exception("429 RESOURCE_EXHAUSTED: Please retry in 7s. quota metric PerMinute")
    cl = scripted([err, err])
    r = reader(cl, rpm=6000, clock=clk.now, sleep=clk.sleep)
    ext = r.extract(b"x", "image/png")
    assert ext.amounts[0].value == 20000 and cl.models.calls == 3
    assert sum(clk.sleeps) >= 14                                          # menunggu petunjuk Google (8 dtk x 2)

def test_daily_429_stops_immediately():
    cl = scripted([Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")])
    r = reader(cl, clock=FakeClock().now, sleep=lambda s: None)
    with pytest.raises(GeminiQuotaError):
        r.extract(b"x", "image/png")
    assert cl.models.calls == 1

def test_persistent_per_minute_429_eventually_raises_quota():
    cl = scripted([Exception("429 RESOURCE_EXHAUSTED per minute")] * 10)
    r = reader(cl, clock=FakeClock().now, sleep=lambda s: None)
    with pytest.raises(GeminiQuotaError):
        r.extract(b"x", "image/png")
    assert cl.models.calls == 5                                           # 1 + 4 percobaan ulang
