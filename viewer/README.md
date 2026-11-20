# Rule reasoning viewer

Browse the SGDL rule-revision logs in `../`, see where compile errors come from, and take notes.

```
python serve.py              # opens http://localhost:8765
python serve.py --rebuild    # re-extract data.json after adding/changing log folders
```

- `build_data.py` - turns the `g<gen>_<idx>_<Game>.log` files into `data.json` (add new run folders to `RUNS` at the top).
- `notes.json` - your notes and tags, written by the server as you type (previous version kept as `notes.json.bak`).
- `player_api.py` - bridge to the SolitaireGDL engine for the **Play** view (press **▶ Play** on either version in Browse).
  The engine is imported from `~/Documents/GitHub/SolitaireGDL`; set `SGDL_ENGINE` to use another checkout.
  It may differ from the engine version the lab used (e.g. no "Pilename DRAW" check), so local results can differ from the logs.
- Play has three solvers: **Find a win** (`player_api.solve`), **Find a win on speed** (`solve_speed`) and
  **Find a win on plutonium** (`solve_plutonium`). Same depth-first search, same move order, same answers.
  - on speed: skips stack moves through face-down cards or broken runs, checks moves lazily, evaluates rule
    conditions directly (about 5x faster than Find a win on the lab engine).
  - on plutonium: on speed plus cheapest-first condition order, an incremental run walk, position hashes computed
    from the two piles a move changes (seen positions are never built), and new positions that share untouched
    piles. About 3x faster than on speed on the lab engine, 7x on the older local engine. Each method can be
    switched off via `opts` for testing; `verify=True` checks every incremental hash against the engine's.
- **Find a win on neutronium (Fawon)** (`solve_neutronium`): the same search in a portable C++17 core,
  `fawon/core/fawon_core.cpp`, loaded with ctypes. Same move order, hash and answers as plutonium (verified on every
  logged SGDL x 2 seeds with both engines, and on Linux/gcc); about 30x faster than plutonium on one core.
  Build it once per machine: `python fawon/native.py build` (finds MSVC, g++ or clang++). It is never built
  implicitly; without it Fawon falls back to plutonium (identical results) and says so.
  `fawon/` also holds the experiments: `fbench.py` (300-deal test bench), `verify_all.py`, `candidates.py`.
- `bench/bench.py` - races the lab's evaluation bot (`dfs-heuristic`, from SolitaireGDL `origin/main`) against the
  viewer's Find-a-win search on the same deals. Run `python bench/bench.py` (resumes automatically; `--fresh` to restart)
  or use the **Bots** tab, which shows live progress from `bench/status.json` and `bench/results.jsonl`.
- Press `?` in the viewer for keyboard shortcuts.

Static checks shown as flags: `tabs`, `deck short`, `deck over`, `dup rule`, `DEAL draw as source`, `DRAW undefined`, `DRAW misuse`, `unchanged`.
