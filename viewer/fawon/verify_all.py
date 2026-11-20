"""Exhaustive check: every compiling logged SGDL version x 2 seeds, plutonium vs the C++ core, identical results.
Runs on a process pool. Set SGDL_ENGINE to test against a different engine checkout."""
import json, os, sys, time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _init():
    global P, native, fbench
    import native as _n, fbench as _f
    native, fbench, P = _n, _f, _n.P


def _task(arg):
    sgdl, seed, cap = arg
    g, err = P.compile_sgdl(sgdl, seed)
    if err:
        return "skip", None, None
    a = fbench.key(P.solve_plutonium(g, cap, 600))
    g, _ = P.compile_sgdl(sgdl, seed)
    b = fbench.key(native.solve_native(g, cap, 600))
    return ("same" if a == b else "DIFF"), a[:4], b[:4]


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    d = json.load(open(os.path.join(os.path.dirname(HERE), "data.json"), encoding="utf-8"))
    sgdls = {e["in"] for r in d["runs"] for gs in r["games"].values() for e in gs.values() if not e["err"]}
    sgdls |= {e["out"] for r in d["runs"] for gs in r["games"].values() for e in gs.values() if e["out"]}  # incl. never-evaluated outputs
    tasks = [(s, seed, cap) for s in sorted(sgdls) for seed in (17, 29)]
    t0 = time.time()
    with mp.get_context("spawn").Pool(min(16, os.process_cpu_count()), initializer=_init) as pool:
        res = pool.map(_task, tasks, chunksize=4)
    from collections import Counter
    c = Counter(r[0] for r in res)
    print(f"engine={os.environ.get('SGDL_ENGINE', 'local')}  {len(sgdls)} distinct SGDL texts x 2 seeds, cap {cap}: {dict(c)}  ({time.time()-t0:.0f}s)")
    for (s, seed, _), r in zip(tasks, res):
        if r[0] == "DIFF":
            print("  DIFF", s.splitlines()[0], seed, "plutonium", r[1], "native", r[2])
            break
