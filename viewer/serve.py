"""Serve the rule-reasoning viewer locally and persist notes to notes.json.

    python serve.py            -> http://localhost:8765
    python serve.py 9000       -> custom port
    python serve.py --rebuild  -> re-extract data.json from the logs first

Stdlib only. Notes are written to notes.json next to this file (a .bak copy is kept).
"""
import http.server
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import player_api  # noqa: E402  (SGDL engine bridge; engine itself is imported lazily)

NOTES = os.path.join(HERE, "notes.json")
DATA = os.path.join(HERE, "data.json")


def _pid_running(pid) -> bool:
    """True if a process with this pid exists (never signals it: os.kill(pid, 0) terminates on Windows)."""
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True).stdout
        return f'"{int(pid)}"' in out
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):  # keep the console quiet except for notes saves
        pass

    def end_headers(self):
        # always re-check index.html / data.json so an updated viewer shows up without a hard refresh
        if not self.path.startswith("/cards/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/play/status"):
            return self._json(200, player_api.available())
        if self.path.startswith("/cards/"):
            name = os.path.basename(self.path.split("?")[0])
            if not re.fullmatch(r"[0-9A-Z]{2}\.svg", name):
                return self._json(404, {"error": "no such card"})
            fp = os.path.join(player_api.TEXTURE_DIR, name)
            if not os.path.isfile(fp):
                return self._json(404, {"error": "card art not found"})
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/notes"):
            if os.path.exists(NOTES):
                with open(NOTES, encoding="utf-8") as f:
                    return self._json(200, json.load(f))
            return self._json(200, {"version": 1, "items": {}})
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/play/"):
            return self._play(self.path.split("?")[0].rsplit("/", 1)[-1])
        if self.path.startswith("/api/bench/"):
            return self._bench(self.path.split("?")[0].rsplit("/", 1)[-1])
        if not self.path.startswith("/api/notes"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
            assert isinstance(obj, dict) and isinstance(obj.get("items"), dict)
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"bad notes payload: {e}"})
        if os.path.exists(NOTES):
            shutil.copyfile(NOTES, NOTES + ".bak")
        tmp = NOTES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, NOTES)
        print(f"saved {len(obj['items'])} notes -> notes.json")
        return self._json(200, {"ok": True, "count": len(obj["items"])})

    def _bench(self, op):
        """Start/resume or stop bench/bench.py as a detached process (it outlives this server)."""
        n = int(self.headers.get("Content-Length", 0))
        b = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        status_fp = os.path.join(HERE, "bench", "status.json")
        st = {}
        if os.path.exists(status_fp):
            with open(status_fp, encoding="utf-8") as f:
                st = json.load(f)
        alive = st.get("state") in ("starting", "running") and (
            time.time() - st.get("updated", 0) < 30 or _pid_running(st.get("pid")))
        if op == "start":
            if alive:
                return self._json(409, {"error": "benchmark is already running"})
            cmd = [sys.executable, os.path.join(HERE, "bench", "bench.py"), "--time", str(float(b.get("time", 60)))]
            if b.get("fresh"):
                cmd.append("--fresh")
            flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            log = open(os.path.join(HERE, "bench", "bench.log"), "a", encoding="utf-8")
            subprocess.Popen(cmd, cwd=os.path.join(HERE, "bench"), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != "nt")
            return self._json(200, {"ok": True})
        if op == "stop":
            if not alive:
                return self._json(200, {"ok": True, "note": "not running"})
            try:
                os.kill(int(st["pid"]), signal.SIGTERM)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": f"could not stop pid {st.get('pid')}: {e}"})
            st.update(state="stopped", updated=time.time())
            with open(status_fp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": f"unknown bench op {op}"})

    def _play(self, op):
        n = int(self.headers.get("Content-Length", 0))
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"bad request: {e}"})
        P = player_api
        seed = b.get("seed")
        seed = int(seed) if seed not in (None, "") else None
        try:
            if op == "new":
                res = P.new_game(b["sgdl"], seed)
            elif op == "act":
                res = P.act(b["id"], b["action"])
            elif op == "undo":
                res = P.undo(b["id"])
            elif op == "restart":
                res = P.restart(b["id"])
            elif op == "random":
                res = P.random_move(b["id"])
            elif op == "solve":
                algo = b.get("algo")
                if algo is not None and algo not in P.SOLVERS:
                    return self._json(400, {"error": f"unknown solver {algo}"})
                res = P.solve_session(b["id"], int(b.get("maxMoves", 2000)), float(b.get("timeLimit", 10)), bool(b.get("speed")), algo)
            elif op == "simulate":
                res = P.simulate_deal(b["sgdl"], seed or 0, int(b.get("maxMoves", 2000)), float(b.get("timeLimit", 6)))
            else:
                return self._json(404, {"error": f"unknown play op {op}"})
        except KeyError as e:
            return self._json(410, {"error": str(e).strip("'\"")})
        except RuntimeError as e:
            return self._json(503, {"error": str(e)})
        return self._json(200, res)


def main():
    args = sys.argv[1:]
    if "--rebuild" in args or not os.path.exists(DATA):
        sys.path.insert(0, HERE)
        import build_data
        build_data.main()
    port = next((int(a) for a in args if a.isdigit()), 8765)
    # On Windows SO_REUSEADDR lets a second server silently share the port with a stale one.
    socketserver.TCPServer.allow_reuse_address = os.name != "nt"
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://localhost:{port}/"
        print(f"Rule reasoning viewer at {url}  (Ctrl+C to stop)")
        if "--no-open" not in args:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
