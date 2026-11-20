"""Generation-sized batch: 100 SGDL versions x 10 deals = 1,000 searches (cap 2000). Wall time per setup."""
import os, sys, time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _init():
    global cand
    import candidates as cand  # noqa: F401


def _file_task(arg):
    """One work item = one SGDL file's 10 deals (how an eval would hand out work)."""
    sgdl, seeds, solver = arg
    f = getattr(cand, solver)
    out = []
    for s in seeds:
        g, err = cand.P.compile_sgdl(sgdl, s)
        r = f(g, 2000, 600)
        out.append((r["win"], r["moves"], r["exhausted"]))
    return out


if __name__ == "__main__":
    import fbench
    files = sorted({v["sgdl"] for v, _ in fbench.bench_set(n_versions=100, seeds=(1,))})
    seeds = list(range(1, 11))
    results = {}
    for solver, workers in [("solve_native", 1), ("solve_native", 4), ("solve_native", 8), ("solve_native", 16),
                            ("plutonium", 16), ("solve_compiled_gc", 16)]:
        tasks = [(s, seeds, solver) for s in files]
        t0 = time.perf_counter()
        if workers == 1:
            _init()
            res = [_file_task(t) for t in tasks]
        else:
            with mp.get_context("spawn").Pool(workers, initializer=_init) as pool:
                res = pool.map(_file_task, tasks, chunksize=1)
        t = time.perf_counter() - t0
        results.setdefault(solver, res)
        same = res == results["solve_native"]
        print(f"{solver:18s} {workers:2d} worker(s): {t:6.1f}s for {len(files)} files x 10 deals"
              f"{'' if same else '  RESULTS DIFFER'}", flush=True)
