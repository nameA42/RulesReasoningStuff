"""Fawon test bench: a fixed set of deals, plutonium's reference results, and a timer + identity check.

    PYTHONHASHSEED=0 py -3.13 fbench.py reference     # (re)build reference.json with plutonium
    PYTHONHASHSEED=0 py -3.13 fbench.py check NAME    # time solver NAME from candidates.py, compare to reference

Run with PYTHONHASHSEED=0: the engine deals to several pile types in Python-set order, which otherwise
changes from process to process.
"""
import json, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.dirname(HERE)
os.environ.setdefault("SGDL_ENGINE", os.path.join(VIEWER, "bench", ".labengine"))
sys.path.insert(0, VIEWER)
sys.path.insert(0, HERE)
import player_api as P  # noqa: E402

P.engine()
CAP = 2000
REF = os.path.join(HERE, "reference.json")


def bench_set(n_versions=60, seeds=(1, 2, 3, 4, 5)):
    d = json.load(open(os.path.join(VIEWER, "data.json"), encoding="utf-8"))
    entries = [(r["key"], name, gen, e["in"]) for r in d["runs"] for name, gs in r["games"].items()
               for gen, e in gs.items() if not e["err"]]
    rnd = random.Random(11)
    rnd.shuffle(entries)
    out = []
    for rk, name, gen, sgdl in entries:
        if P.compile_sgdl(sgdl, 1)[1]:
            continue
        out.append({"id": f"{rk}/{name}/g{gen}", "sgdl": sgdl})
        if len(out) == n_versions:
            break
    return [(v, s) for v in out for s in seeds]


def key(r):
    return [r["win"], r["moves"], r["exhausted"], r.get("searched"), list(r.get("path") or [])]


def run(solver, deals, time_limit=600):
    res, t0 = [], time.perf_counter()
    for v, s in deals:
        g, err = P.compile_sgdl(v["sgdl"], s)
        res.append(key(solver(g, CAP, time_limit)))
    return res, time.perf_counter() - t0


if __name__ == "__main__":
    deals = bench_set()
    if sys.argv[1] == "reference":
        res, t = run(P.solve_plutonium, deals)
        json.dump({"deals": [[v["id"], s] for v, s in deals], "results": res, "seconds": t}, open(REF, "w"))
        wins = sum(r[0] for r in res); ex = sum(r[2] for r in res)
        print(f"reference: {len(deals)} deals, plutonium {t:.1f}s; {wins} wins, {ex} proved unwinnable, {len(deals)-wins-ex} hit the cap")
    else:
        import candidates
        ref = json.load(open(REF))
        name = sys.argv[2]
        solver = getattr(candidates, name)
        res, t = run(solver, deals)
        bad = [i for i, (a, b) in enumerate(zip(res, ref["results"])) if a != b]
        print(f"{name}:{t:.1f}s vs plutonium {ref['seconds']:.1f}s -> {ref['seconds']/t:.2f}x; "
              f"identical on {len(deals)-len(bad)}/{len(deals)} deals" + (f"; first mismatch {ref['deals'][bad[0]]}" if bad else ""))
