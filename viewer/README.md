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
- Press `?` in the viewer for keyboard shortcuts.

Static checks shown as flags: `tabs`, `deck short`, `deck over`, `dup rule`, `DEAL draw as source`, `DRAW undefined`, `DRAW misuse`, `unchanged`.
