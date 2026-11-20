"""Race the lab's evaluation bot against the viewer's Find a win, Find a win on speed, on plutonium and on neutronium (Fawon) on identical deals.

    python bench.py                 # start, or resume where a previous run stopped
    python bench.py --fresh         # discard previous results and start over
    python bench.py --time 60 --versions 12 --deals 2

Both bots run on the lab's engine: SolitaireGDL `origin/main`, exported (read-only, via
`git archive`) into bench/.labengine the first time. The lab bot is the real code path the
evolution experiments used: simulate_many.simulate_one + players["dfs-heuristic"].

Progress is written to bench/status.json (heartbeat every 2 s) and results to
bench/results.jsonl, which the viewer's "Bots" tab reads live.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import tarfile
import io
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.dirname(HERE)
LAB = os.path.join(HERE, ".labengine")
STATUS = os.path.join(HERE, "status.json")
RESULTS = os.path.join(HERE, "results.jsonl")
REPO = os.environ.get("SGDL_REPO") or os.path.join(os.path.expanduser("~"), "Documents", "GitHub", "SolitaireGDL")
CAP = 2000

ap = argparse.ArgumentParser()
ap.add_argument("--time", type=float, default=60.0, help="seconds per deal per bot (the lab used 60)")
ap.add_argument("--versions", type=int, default=12, help="how many logged SGDL versions to test")
ap.add_argument("--deals", type=int, default=2, help="deals (seeds) per version")
ap.add_argument("--fresh", action="store_true", help="ignore previous results")
args = ap.parse_args()

status = {"state": "starting", "pid": os.getpid(), "started": time.time(), "updated": time.time(),
          "settings": {"time": args.time, "versions": args.versions, "deals": args.deals, "cap": CAP},
          "done": 0, "total": 0, "current": None, "error": None}
_lock = threading.Lock()


def atomic_write(path, text):
    """Write a file via rename. On Windows the rename fails while a reader (the viewer polling, a watcher)
    has the file open, so retry briefly. Returns False instead of raising if it never succeeds."""
    tmp = path + ".tmp"
    for attempt in range(40):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1) if attempt < 10 else 0.5)
    return False


def write_status(**kw):
    """Rewrite status.json; a failed write never kills the benchmark."""
    with _lock:
        status.update(kw)
        status["updated"] = time.time()
        if not atomic_write(STATUS, json.dumps(status)):
            print("warning: could not update status.json (file kept locked); continuing", file=sys.stderr, flush=True)


def heartbeat():
    while status["state"] in ("starting", "running"):
        write_status()
        time.sleep(2)


def export_lab_engine():
    if os.path.isfile(os.path.join(LAB, "simulate_many.py")):
        return
    write_status(state="starting", current={"note": "exporting lab engine from origin/main"})
    blob = subprocess.run(["git", "-C", REPO, "archive", "origin/main"], capture_output=True, check=True).stdout
    os.makedirs(LAB, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        t.extractall(LAB, filter="data") if sys.version_info >= (3, 12) else t.extractall(LAB)


try:
    os.makedirs(HERE, exist_ok=True)
    threading.Thread(target=heartbeat, daemon=True).start()
    export_lab_engine()
    sys.path.insert(0, LAB)
    sys.modules.pop("parser", None)
    try:
        import tqdm  # noqa: F401  (simulate_many imports it at module level)
    except ImportError:
        stub = types.ModuleType("tqdm"); stub.tqdm = lambda x, *a, **k: x; sys.modules["tqdm"] = stub
    from parser import Parser  # noqa: E402
    import simulate_many  # noqa: E402
    from simulate import players  # noqa: E402
    # "Find a win on speed" lives in the viewer's player_api; point it at the same lab engine
    os.environ["SGDL_ENGINE"] = LAB
    sys.path.insert(1, VIEWER)
    import player_api  # noqa: E402
    player_api.engine()
except Exception as e:  # noqa: BLE001
    write_status(state="failed", error=f"setup: {e}")
    raise
simulate_many.max_time_per_sim = args.time


def my_solve(game, max_moves=CAP, time_limit=args.time):
    """Same algorithm as viewer/player_api.solve: plain DFS, engine move order, repeated-state pruning."""
    valid = lambda g: [str(a).strip() for a in g.get_possible_actions(True)]
    t0 = time.time()
    root = game.copy()
    seen = {root.get_efficient_hash()}
    if root.is_win():
        return dict(win=True, path=0, states=0, exhausted=False, t=0.0, stop=None)
    frames, path, n = [[root, valid(root), 0]], [], 0
    while frames:
        if time.time() - t0 > time_limit:
            return dict(win=False, path=0, states=n, exhausted=False, t=time.time() - t0, stop="time")
        st, acts, i = frames[-1]
        if i >= len(acts):
            frames.pop()
            if path:
                path.pop()
            continue
        frames[-1][2] += 1
        ns = st.copy(); ns.logger.active = False
        Parser.perform_action_in_game(acts[i], ns)
        h = ns.get_efficient_hash()
        if h in seen:
            continue
        seen.add(h); n += 1; path.append(acts[i])
        if ns.is_win():
            return dict(win=True, path=len(path), states=n, exhausted=False, t=time.time() - t0, stop=None)
        if n >= max_moves:
            return dict(win=False, path=0, states=n, exhausted=False, t=time.time() - t0, stop="cap")
        frames.append([ns, valid(ns), 0])
    return dict(win=False, path=0, states=n, exhausted=True, t=time.time() - t0, stop=None)


VIEWER_BOTS = {"speed": "solve_speed", "plutonium": "solve_plutonium", "neutronium": "solve_neutronium"}  # result key -> player_api function
BOT_LABELS = {"lab": "lab bot", "mine": "Find a win", "speed": "Find a win on speed", "plutonium": "Find a win on plutonium",
              "neutronium": "Find a win on neutronium (Fawon)"}


def viewer_solve(key, game):
    """A player_api solver ('on speed', 'on plutonium'), reported in the same shape as my_solve."""
    t0 = time.time()
    r = getattr(player_api, VIEWER_BOTS[key])(game, CAP, args.time)
    t = time.time() - t0  # unrounded (the solvers round to 0.01 s)
    stop = {"move cap": "cap", "time limit": "time"}.get(r.get("reason"))
    return dict(win=r["win"], path=r["moves"] if r["win"] else 0, states=r.get("searched", r["moves"]),
                exhausted=r["exhausted"], t=t, stop=stop)


def fresh_game(sgdl, seed):
    g = Parser.parse(sgdl, seed, False, True)
    g.logger.active = False
    return g


def lab_run(sgdl, seed):
    t0 = time.time()
    end, moves, _, trace, _, stopped = simulate_many.simulate_one(
        0, players["dfs-heuristic"](None), sgdl, seed, CAP, True, 0, 0, 0, 0, True)
    t = time.time() - t0
    win = end.is_win()
    stop = None if win or stopped else ("cap" if moves >= CAP else "time")
    return dict(win=win, moves=moves, path=len(trace) if win else 0, exhausted=bool(stopped), t=t, stop=stop)


def pick_versions(n):
    d = json.load(open(os.path.join(VIEWER, "data.json"), encoding="utf-8"))
    pool = {}
    for r in d["runs"][:2]:
        for name, gens in r["games"].items():
            for gen, e in gens.items():
                if e["outcome"] in ("cap", "mixedloss", "somewin", "allwin"):
                    pool.setdefault(e["outcome"], []).append((r["key"], name, gen, e))
    rnd = random.Random(7)
    want = {"cap": n // 2, "mixedloss": max(1, n // 6), "somewin": max(1, n // 4), "allwin": 1}
    return [x for o, k in want.items() for x in rnd.sample(pool[o], k)]


picks = pick_versions(args.versions)
seeds = [101 * (k + 1) for k in range(args.deals)]
jobs = [(p, s) for p in picks for s in seeds]
done_keys = set()
if args.fresh and os.path.exists(RESULTS):
    os.remove(RESULTS)
rows = []
if os.path.exists(RESULTS):
    for line in open(RESULTS, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
for r in rows:
    if r.get("settings", {}).get("time") == args.time:
        done_keys.add((r["run"], r["game"], r["gen"], r["seed"]))
already = len(done_keys & {(p[0], p[1], p[2], s) for p, s in jobs})
write_status(state="running", total=len(jobs), done=already, resumedFrom=already, started=time.time())

# Races finished before a viewer bot existed: add its result without re-running the slow lab bot.
by_key = {(p[0], p[1], p[2]): p[3] for p in picks}
todo = [(r, b) for r in rows if r.get("lab") and r.get("mine") and (r["run"], r["game"], r["gen"]) in by_key
        for b in VIEWER_BOTS if b not in r]
for k, (r, b) in enumerate(todo, 1):
    write_status(current=dict(run=r["run"], game=r["game"], gen=r["gen"], seed=r["seed"], bot=b, since=time.time(),
                              note=f"adding {BOT_LABELS[b]} results to finished races: {k}/{len(todo)}"))
    r[b] = viewer_solve(b, fresh_game(by_key[(r["run"], r["game"], r["gen"])]["in"], r["seed"]))
    print(f"backfill {r['game']} g{r['gen']} seed {r['seed']}: {b} {r[b]['win']} {r[b]['states']} "
          f"{r[b]['stop']} {r[b]['t']:.2f}s", flush=True)
if todo and not atomic_write(RESULTS, "".join(json.dumps(r) + "\n" for r in rows)):
    write_status(state="failed", error="could not rewrite results.jsonl with the backfilled results (file locked)")
    sys.exit(1)

with open(RESULTS, "a", encoding="utf-8") as f:
    for (run, name, gen, e), seed in jobs:
        if (run, name, gen, seed) in done_keys:
            continue
        sgdl = e["in"]
        base = dict(run=run, game=name, gen=gen, seed=seed, logged=e["outcome"],
                    loggedWins=sum(1 for x in e["rows"] if x[1]), settings={"time": args.time, "cap": CAP})
        try:
            Parser.parse(sgdl, seed, False, True)
        except Exception as ex:  # noqa: BLE001
            f.write(json.dumps(dict(base, error=str(ex))) + "\n"); f.flush()
            write_status(done=status["done"] + 1)
            continue
        write_status(current=dict(run=run, game=name, gen=gen, seed=seed, bot="lab", since=time.time()))
        lab = lab_run(sgdl, seed)
        write_status(current=dict(run=run, game=name, gen=gen, seed=seed, bot="mine", since=time.time()))
        mine = my_solve(fresh_game(sgdl, seed))
        extra = {}
        for b in VIEWER_BOTS:
            write_status(current=dict(run=run, game=name, gen=gen, seed=seed, bot=b, since=time.time()))
            extra[b] = viewer_solve(b, fresh_game(sgdl, seed))
        f.write(json.dumps(dict(base, lab=lab, mine=mine, **extra)) + "\n"); f.flush()
        write_status(done=status["done"] + 1, current=None)
        print(f"{name} g{gen} seed {seed}: lab {lab['win']} {lab['moves']} {lab['stop']} {lab['t']:.1f}s | "
              f"mine {mine['win']} {mine['states']} {mine['stop']} {mine['t']:.1f}s | " +
              " | ".join(f"{b} {x['win']} {x['states']} {x['stop']} {x['t']:.2f}s" for b, x in extra.items()), flush=True)
write_status(state="done", current=None)
print("done")
