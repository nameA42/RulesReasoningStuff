"""Every startup / fixed cost around a Fawon eval, measured separately.
usage: py -3.13 startup_costs.py process   (run in a fresh process: import + library load costs)
       py -3.13 startup_costs.py pergame   (fixed costs per game vs the search itself)
       py -3.13 startup_costs.py lab       (the lab pipeline's own per-process and per-game overheads)"""
import os, sys, time
T_START = time.perf_counter()
HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.dirname(HERE)
LAB = os.path.join(VIEWER, "bench", ".labengine")
os.environ.setdefault("SGDL_ENGINE", LAB)
mode = sys.argv[1]
ms = lambda s: f"{s*1000:8.1f} ms"

if mode == "process":
    t = time.perf_counter(); sys.path.insert(0, VIEWER); import player_api as P; a = time.perf_counter() - t
    t = time.perf_counter(); P.engine(); b = time.perf_counter() - t
    t = time.perf_counter(); sys.path.insert(0, HERE); import native; c = time.perf_counter() - t
    t = time.perf_counter(); native.lib(); d = time.perf_counter() - t
    print(f"import player_api (viewer glue)  {ms(a)}\nimport SolitaireGDL engine        {ms(b)}\n"
          f"import native.py (Fawon glue)     {ms(c)}\nload compiled C++ library         {ms(d)}")

elif mode == "pergame":
    import ctypes, json, statistics as st
    sys.path.insert(0, HERE)
    import fbench, native
    P = fbench.P
    deals = fbench.bench_set()
    rows = []
    for v, s in deals:
        t0 = time.perf_counter(); g, _ = P.compile_sgdl(v["sgdl"], s)
        t1 = time.perf_counter(); i32, i64, tags = native.export(g)
        t2 = time.perf_counter(); a, b = native._arrays(i32, i64); out = (ctypes.c_int32 * (4 + 4 * 2002))()
        t3 = time.perf_counter(); native.lib().fawon_solve_fast(a, len(i32), b, out, len(out), 1, 600.0)   # cap 1 = decode + setup only
        t4 = time.perf_counter(); native.lib().fawon_solve_fast(a, len(i32), b, out, len(out), 2000, 600.0)
        t5 = time.perf_counter()
        rows.append((t1 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4))
    names = ["parse SGDL + shuffle + deal (Python engine)", "export rules + position (Python)",
             "convert to C arrays (ctypes)", "C++ decode + set up search (cap 1 call)", "C++ full search (cap 2000)"]
    tot = sum(sum(r) - r[3] for r in rows)  # the cap-1 call is a measurement, not part of a real eval
    print(f"{len(rows)} games; per game: median / mean / share of a real eval's time")
    for k, name in enumerate(names):
        col = [r[k] for r in rows]
        share = "(measurement only)" if k == 3 else f"{100 * sum(col) / tot:5.1f}%"
        print(f"  {name:46s} {ms(st.median(col))} {ms(st.mean(col))}   {share}")

elif mode == "lab":
    t = time.perf_counter(); import pandas  # noqa: F401
    a = time.perf_counter() - t
    sys.path.insert(0, LAB); sys.modules.pop("parser", None)
    t = time.perf_counter(); import evaluate_gdl  # noqa: F401  (pulls in simulate_many, simulate, player, engine, tqdm)
    b = time.perf_counter() - t
    print(f"lab eval job, per process: import pandas {ms(a)} | import evaluate_gdl + engine + bots {ms(b)}")
    from parser import Parser
    import simulate_many
    from simulate import players
    sys.path.insert(0, HERE)
    import json, statistics as st
    deals = [(v, s) for v, s in __import__("fbench").bench_set(n_versions=20, seeds=(1,))]
    parse, copyt, usage, frame = [], [], [], []
    for v, s in deals:
        t0 = time.perf_counter(); g = Parser.parse(v["sgdl"], s, False, True); t1 = time.perf_counter()
        start = g.copy(); t2 = time.perf_counter()
        try:
            evaluate_gdl.get_card_usage(start, g, evaluate_gdl.Logger(False))
            evaluate_gdl.get_pile_usage(start, [], evaluate_gdl.Logger(False))
        except Exception:
            pass
        t3 = time.perf_counter()
        pandas.DataFrame({"Win": [False] * 10, "Move Count": [0] * 10}); t4 = time.perf_counter()
        parse.append(t1 - t0); copyt.append(t2 - t1); usage.append(t3 - t2); frame.append(t4 - t3)
    print(f"lab, per deal outside the search: parse {ms(st.median(parse))} | starting copy {ms(st.median(copyt))} | "
          f"card+pile usage {ms(st.median(usage))} | per file: results DataFrame {ms(st.median(frame))}")
