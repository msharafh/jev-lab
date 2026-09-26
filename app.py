"""Jev Lab — local web app for the capture-triage experiment.

Runs on your Mac only (http://127.0.0.1:8765). The API key stays in this process;
the browser never sees it. Standard library only, plus typesafe-sdk.

Start:  double-click start.command   — or —   python3 app.py
Key:    TYPESAFE_API_KEY from your shell, or a line TYPESAFE_API_KEY=... in jev-lab/.env
"""
import asyncio
import json
import os
import random
import re
import threading
import time
import webbrowser
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import triage as T
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeError

HERE = Path(__file__).parent
STATIC = HERE / "static"
RUNS_DIR = HERE / "runs"
PORT = int(os.environ.get("JEV_LAB_PORT", "8765"))
MODEL = os.environ.get("JEV_LAB_MODEL", "jev-latest")
APP_VERSION = 3  # bump when the page needs server features; the page warns on mismatch


def load_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TYPESAFE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


API_KEY = load_key()
CATALOG = json.loads(T.CATALOG_FILE.read_text(encoding="utf-8"))
BY_NAME = {p["name"]: p for p in CATALOG}
CAPS = T.load_captures(T.CAPTURES_DIR)
CAP_BY_ID = {c["id"]: c for c in CAPS}
URL_FM = re.compile(r'^url:\s*"?([^"\n]+)"?', re.M)


def capture_url(cid: str) -> str:
    try:
        head = (T.CAPTURES_DIR / cid).read_text(encoding="utf-8", errors="ignore")[:2000]
        m = URL_FM.search(head)
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


# --------------------------------------------------------------------------- runs

class Run:
    def __init__(self, params: dict, picks: list):
        self.id = time.strftime("%Y-%m-%d_%H%M%S") + ("-mock" if params["mock"] else "")
        self.dir = RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.params, self.picks = params, picks
        self.events, self.lock = [], threading.Lock()
        self.status, self.stop, self.errors = "running", False, 0
        (self.dir / "meta.json").write_text(json.dumps(
            {"id": self.id, "params": params, "total": len(picks), "model": MODEL,
             "created": time.strftime("%Y-%m-%d %H:%M")}, indent=2))

    def push(self, ev: dict):
        with self.lock:
            self.events.append(ev)

    def next_seq(self) -> int:
        with self.lock:
            self._seq = getattr(self, "_seq", 0) + 1
            return self._seq

    def log_call(self, rec: dict):
        with self.lock, (self.dir / "calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


RUNS: dict[str, Run] = {}
CURRENT: list = [None]


def pick_captures(params: dict) -> list:
    rnd = random.Random(params.get("seed", 7))
    labeled = [c for c in CAPS if c["gold"]]
    unlabeled = [c for c in CAPS if not c["gold"]]
    s = params["set"]
    if s == "labeled":
        pick = list(labeled)
    elif s == "unlabeled":
        pick = list(unlabeled)
    elif s == "all":
        pick = list(CAPS)
    else:
        pick = labeled + rnd.sample(unlabeled, min(100, len(unlabeled)))
    rnd.shuffle(pick)
    lim = int(params.get("limit") or 0)
    return pick[:lim] if lim > 0 else pick


def enrich(row: dict) -> dict:
    c = CAP_BY_ID.get(row["id"], {})
    return dict(row, author=c.get("author", ""), published=c.get("published", ""))


class Tracked:
    """Wraps the SDK client so every HTTP call to /v1/systemone is visible in the UI:
    a `fire` event when the request leaves, `recv` / `fail` when it comes back."""

    def __init__(self, client, run: "Run", cid: str):
        self.client, self.run, self.cid = client, run, cid

    async def system_one(self, state, questions, **kw):
        run = self.run
        stage = 1 if "page" in questions else 2
        seq = run.next_seq()
        run.push({"type": "fire", "seq": seq, "id": self.cid, "stage": stage})
        t0 = time.perf_counter()
        try:
            if run.params.get("mock"):  # simulate network time so mock runs look like live ones
                await asyncio.sleep(random.uniform(0.25, 1.6))
            r = await self.client.system_one(state, questions, **kw)
        except Exception as e:
            rec = {"type": "fail", "seq": seq, "id": self.cid, "stage": stage,
                   "ms": round((time.perf_counter() - t0) * 1000), "err": type(e).__name__}
            run.push(rec); run.log_call(rec)
            raise
        rec = {"type": "recv", "seq": seq, "id": self.cid, "stage": stage,
               "ms": round((time.perf_counter() - t0) * 1000), "tokens": r.usage.input_tokens}
        run.push(rec); run.log_call(rec)
        return r


async def execute(run: Run):
    ns = Namespace(fit=run.params["fit"], none_skip=0.8, shortlist=3)
    kw = dict(model=MODEL, retry=RetryPolicy(max_retries=4, backoff_max=10.0, timeout=60.0))
    if run.params["mock"]:
        kw.update(api_key="mock", transport=T.mock_transport())
    else:
        kw.update(api_key=API_KEY)
    sem = asyncio.Semaphore(int(run.params.get("concurrency", 6)))
    out = (run.dir / "results.jsonl").open("a", encoding="utf-8")
    fatal = []

    async def worker(c):
        async with sem:
            if run.stop or fatal:
                return
            try:
                r = await T.triage_one(Tracked(client, run, c["id"]), c, CATALOG, BY_NAME, ns)
            except TypeSafeError as e:
                run.errors += 1
                name = type(e).__name__
                run.push({"type": "error", "id": c["id"], "message": f"{name}: {e}"[:400]})
                if "Authentication" in name or "PermissionDenied" in name:
                    fatal.append(name)
                return
            out.write(json.dumps(r, ensure_ascii=False) + "\n"); out.flush()
            run.push({"type": "row", "row": enrich(r)})

    try:
        async with AsyncTypeSafeClient(**kw) as client:
            await asyncio.gather(*(worker(c) for c in run.picks))
        run.status = "stopped" if run.stop else ("failed" if fatal else "done")
    except Exception as e:  # network, config
        run.status = "failed"
        run.push({"type": "error", "id": "", "message": f"{type(e).__name__}: {e}"[:400]})
    finally:
        out.close()
        run.push({"type": "end", "status": run.status, "errors": run.errors})


def start_run(params: dict) -> Run:
    run = Run(params, pick_captures(params))
    RUNS[run.id] = run
    CURRENT[0] = run
    threading.Thread(target=lambda: asyncio.run(execute(run)), daemon=True).start()
    return run


def list_runs() -> list:
    out = []
    if RUNS_DIR.exists():
        for d in sorted(RUNS_DIR.iterdir(), reverse=True):
            m = d / "meta.json"
            res = d / "results.jsonl"
            if not d.is_dir() or not res.exists():
                continue
            meta = json.loads(m.read_text()) if m.exists() else {"id": d.name, "params": {}, "total": None}
            meta["done"] = sum(1 for _ in res.open(encoding="utf-8"))
            meta["id"] = d.name
            out.append(meta)
    return out[:40]


def load_run(rid: str) -> dict:
    d = RUNS_DIR / rid
    rows = [enrich(json.loads(l)) for l in (d / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {"id": rid}
    calls_f = d / "calls.jsonl"
    calls = [json.loads(l) for l in calls_f.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if calls_f.exists() else []
    return {"meta": meta, "rows": rows, "calls": calls}


# --------------------------------------------------------------------------- http

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
        ".ttf": "font/ttf", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p in ("/", "/index.html"):
            return self.static("index.html")
        if p.startswith("/static/"):
            return self.static(unquote(p[len("/static/"):]))
        if p == "/api/meta":
            cur = CURRENT[0]
            return self.send_json({
                "version": APP_VERSION,
                "key_present": bool(API_KEY), "model": MODEL,
                "captures": len(CAPS), "labeled": sum(bool(c["gold"]) for c in CAPS),
                "catalog": len(CATALOG), "catalog_names": sorted(BY_NAME),
                "running": cur.id if cur and cur.status == "running" else None,
                "price_per_m": T.PRICE_PER_M_INPUT,
            })
        if p == "/api/runs":
            return self.send_json(list_runs())
        if p.startswith("/api/runs/"):
            rid = unquote(p[len("/api/runs/"):])
            if not (RUNS_DIR / rid / "results.jsonl").exists() or "/" in rid:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json(load_run(rid))
        if p == "/api/capture":
            c = CAP_BY_ID.get(q.get("id", ""))
            if not c:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json(dict(c, url=capture_url(c["id"])))
        if p == "/api/stream":
            return self.stream(q.get("run", ""), int(q.get("from", "0")))
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/run":
            cur = CURRENT[0]
            if cur and cur.status == "running":
                return self.send_json({"error": "A run is already in progress"}, 409)
            mock = bool(body.get("mock"))
            if not mock and not API_KEY:
                return self.send_json({"error": "No TYPESAFE_API_KEY found (shell env or jev-lab/.env)"}, 400)
            params = {"set": body.get("set", "default"), "limit": int(body.get("limit") or 0),
                      "fit": float(body.get("fit", 0.5)), "mock": mock,
                      "concurrency": max(1, min(12, int(body.get("concurrency") or 6))),
                      "seed": int(body.get("seed") or 7)}
            run = start_run(params)
            return self.send_json({"id": run.id, "total": len(run.picks)})
        if u.path == "/api/stop":
            cur = CURRENT[0]
            if cur and cur.status == "running":
                cur.stop = True
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def static(self, rel: str):
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.send_json({"error": "not found"}, 404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def stream(self, rid: str, start: int):
        run = RUNS.get(rid)
        if not run:
            return self.send_json({"error": "run not live"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        i, last = start, time.time()
        try:
            while True:
                with run.lock:
                    batch = run.events[i:]
                for ev in batch:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                i += len(batch)
                if batch:
                    self.wfile.flush(); last = time.time()
                    if batch[-1]["type"] == "end":
                        return
                elif time.time() - last > 15:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush(); last = time.time()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is busy: an older Jev Lab is probably still running.\n"
              f"Close its Terminal window (or run: kill $(lsof -ti tcp:{PORT})) and start again.")
        return
    url = f"http://127.0.0.1:{PORT}"
    demo = "demo" in T.CAPTURES_DIR.parts
    print(f"Jev Lab running at {url}  ·  key {'found' if API_KEY else 'NOT found (mock mode only)'}  ·  "
          f"{len(CAPS)} captures, {len(CATALOG)} pages{' (demo data)' if demo else ''}  ·  Ctrl+C to stop")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
