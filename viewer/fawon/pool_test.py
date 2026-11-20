"""Scaling of a process pool running the 300 bench deals with the compiled solver. Works with spawn (Windows) and fork."""
import json, os, sys, time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _init():
    global P, cand
    import candidates as cand  # noqa: F401  (imports engine once per worker)
    P = cand.P


def _task(arg):
    i, sgdl, seed = arg
    import fbench
    g, err = P.compile_sgdl(sgdl, seed)
    return i, fbench.key(getattr(cand, os.environ.get("FAWON_SOLVER", "solve_compiled_gc"))(g, 2000, 600))


if __name__ == "__main__":
    import fbench
    deals = fbench.bench_set()
    ref = json.load(open(fbench.REF))["results"]
    tasks = [(i, v["sgdl"], s) for i, (v, s) in enumerate(deals)]
    print(f"cores reported: os.process_cpu_count() = {os.process_cpu_count()}")
    base = None
    for n in [int(x) for x in sys.argv[1:]] or [1, 2, 4, 8, 12, 16, 22]:
        t0 = time.perf_counter()
        with mp.get_context("spawn").Pool(n, initializer=_init) as pool:
            t_ready = time.perf_counter()
            out = dict(pool.imap_unordered(_task, tasks, chunksize=1))
        t = time.perf_counter() - t0
        same = sum(out[i] == ref[i] for i in range(len(ref)))
        base = base or t
        print(f"{n:2d} workers: {t:6.1f}s wall (pool start {t_ready - t0:.1f}s) -> {base / t:4.1f}x vs 1 worker; identical {same}/{len(ref)}", flush=True)
