"""Cold start in a fresh process: what costs time before / outside the per-game search?"""
import os, sys, time
T0 = time.perf_counter()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
which = sys.argv[1]  # "plutonium" or "native"

t = time.perf_counter()
import fbench  # imports player_api + the engine
t_import = time.perf_counter() - t
P = fbench.P
deals = fbench.bench_set(n_versions=10, seeds=(1, 2, 3))

t = time.perf_counter()
if which == "native":
    import native
    native.lib()  # load (or build, if missing) the compiled library
    solve = native.solve_native
else:
    solve = P.solve_plutonium
t_setup = time.perf_counter() - t

times = []
for v, s in deals:
    t = time.perf_counter()
    g, _ = P.compile_sgdl(v["sgdl"], s)
    solve(g, 2000, 600)
    times.append(time.perf_counter() - t)
print(f"{which:9s}: interpreter start -> imports done {T0 and (time.perf_counter() - T0) and 0:.0f}"
      .replace(" 0", ""), end="")
print(f" | engine import {t_import*1000:6.0f} ms | solver setup {t_setup*1000:6.0f} ms | "
      f"1st game {times[0]*1000:7.1f} ms | median of next {len(times)-1} games {sorted(times[1:])[len(times)//2]*1000:7.1f} ms")
