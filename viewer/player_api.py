"""Bridge between the viewer and the SolitaireGDL engine (parser + game logic).

The engine is imported from a local checkout of the SolitaireGDL repo instead of being copied,
so fixes to the engine show up here automatically. Location, in order of preference:
  1. the SGDL_ENGINE environment variable
  2. ~/Documents/GitHub/SolitaireGDL

Note: this local engine may be older/newer than the one the lab server used to evaluate the logs,
so compile errors and bot results can differ slightly from what the logs report.
"""
from __future__ import annotations

import os
import random
import re
import sys
import threading
import time
import uuid

ENGINE_DIR = os.environ.get("SGDL_ENGINE") or os.path.join(os.path.expanduser("~"), "Documents", "GitHub", "SolitaireGDL")
TEXTURE_DIR = os.path.join(ENGINE_DIR, "resources", "textures")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

_engine = None
_engine_error = None


def engine():
    """Import the engine lazily; returns (Parser, base module) or raises RuntimeError."""
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    if _engine_error:
        raise RuntimeError(_engine_error)
    if not os.path.isfile(os.path.join(ENGINE_DIR, "parser.py")):
        _engine_error = f"SolitaireGDL engine not found at {ENGINE_DIR} (set SGDL_ENGINE to its folder)"
        raise RuntimeError(_engine_error)
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    try:
        sys.modules.pop("parser", None)  # make sure we get the engine's parser.py
        import parser as sgdl_parser  # noqa: E402
        import base as sgdl_base  # noqa: E402
    except Exception as e:  # noqa: BLE001
        _engine_error = f"Could not import SolitaireGDL engine from {ENGINE_DIR}: {e}"
        raise RuntimeError(_engine_error)
    _engine = (sgdl_parser.Parser, sgdl_base)
    return _engine


def available():
    try:
        engine()
        return {"ok": True, "engine": ENGINE_DIR}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "engine": ENGINE_DIR}


def clean(s: str) -> str:
    return ANSI.sub("", s or "")


def compile_sgdl(text: str, seed: int):
    """Parse + start a game. Returns (game, None) or (None, error message)."""
    Parser, _ = engine()
    try:
        game = Parser.parse(text, seed, False, True)
        game.logger.active = False
        game.logger.static_activate = False
        return game, None
    except Exception as e:  # noqa: BLE001  (engine uses assert/Exception for user errors)
        msg = str(e) or e.__class__.__name__
        return None, msg


# ---------------------------------------------------------------- state serialisation
def _card(c):
    return {"r": c.rank, "s": str(c.suit), "d": bool(c.face_down)}


def state_of(game, extra=None):
    _, base = engine()
    draw = None
    dp = game.draw_pile
    if isinstance(dp, base.RotateDrawPile):
        draw = {"type": "ROTATE", "back": len(dp.backpile), "view": [_card(c) for c in dp.cards],
                "drawn": len(dp.drawn), "redeals": dp.redeals, "maxRedeals": dp.max_redeals,
                "drawCount": dp.draw_count, "viewCount": dp.view_count}
    elif isinstance(dp, base.DealPile):
        draw = {"type": "DEAL", "count": len(dp.cards), "targets": sorted(dp.target_names)}
    piles = [{"name": name, "piles": [{"ind": p.ind, "cards": [_card(c) for c in p.cards]} for p in ps]}
             for name, ps in game.name_to_piles.items()]
    try:
        actions = [str(a).strip() for a in game.get_possible_actions(True)]
    except Exception as e:  # noqa: BLE001
        actions, extra = [], dict(extra or {}, actionsError=str(e))
    win = False
    win_summary = ""
    try:
        win = bool(game.is_win())
        from condition import GeneralConditionComponents  # engine module
        comps = GeneralConditionComponents(game.name_to_piles, game.draw_pile)
        win_summary = clean(game.win_conditions.summary(True, False, comps))
    except Exception as e:  # noqa: BLE001
        win_summary = f"(could not evaluate win condition: {e})"
    out = {"name": game.name, "draw": draw, "piles": piles, "actions": actions, "win": win, "winSummary": win_summary,
           "cards": len(game.get_all_cards())}
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------- sessions
class Session:
    def __init__(self, sgdl, seed, game):
        self.sgdl, self.seed = sgdl, seed
        self.start = game.copy()
        self.game = game
        self.history = []  # (game copy, action)
        self.lock = threading.Lock()
        self.touched = time.time()

    def view(self, **extra):
        return state_of(self.game, dict(extra, moves=len(self.history), log=[a for _, a in self.history][-200:], seed=self.seed))


SESSIONS: dict[str, Session] = {}
_SLOCK = threading.Lock()


def _get(sid):
    s = SESSIONS.get(sid)
    if not s:
        raise KeyError("Game session expired - press New deal")
    s.touched = time.time()
    return s


def new_game(sgdl: str, seed: int | None):
    if seed is None:
        seed = random.randint(0, 99999)
    game, err = compile_sgdl(sgdl, seed)
    if err:
        return {"error": err, "seed": seed}
    s = Session(sgdl, seed, game)
    sid = uuid.uuid4().hex[:10]
    with _SLOCK:
        SESSIONS[sid] = s
        if len(SESSIONS) > 40:
            for k, _ in sorted(SESSIONS.items(), key=lambda kv: kv[1].touched)[:len(SESSIONS) - 40]:
                SESSIONS.pop(k, None)
    return dict(s.view(), id=sid, description=clean(_describe(game)))


def _describe(game):
    try:
        return game.get_description()
    except Exception as e:  # noqa: BLE001
        return f"(no description: {e})"


def act(sid: str, action: str):
    Parser, _ = engine()
    s = _get(sid)
    with s.lock:
        try:
            summary = clean(Parser.get_action_summary(action, s.game, True, True))
        except Exception as e:  # noqa: BLE001
            summary = f"Could not explain this action: {e}"
        before = s.game.copy()
        try:
            ok = bool(Parser.perform_action_in_game(action, s.game))
        except Exception as e:  # noqa: BLE001
            s.game = before
            return s.view(ok=False, attempted=action, summary=summary, error=str(e))
        if ok:
            s.history.append((before, action))
        return s.view(ok=ok, attempted=action, summary=summary)


def undo(sid: str):
    s = _get(sid)
    with s.lock:
        if s.history:
            s.game, _ = s.history.pop()
        return s.view()


def restart(sid: str):
    s = _get(sid)
    with s.lock:
        s.game, s.history = s.start.copy(), []
        return s.view()


# ---------------------------------------------------------------- bots
def _valid(game):
    return [str(a).strip() for a in game.get_possible_actions(True)]


def solve(game, max_moves=2000, time_limit=10.0):
    """Exhaustive DFS with repeated-state pruning, like the lab's evaluation bot.

    Returns dict(win, moves, exhausted, path). `moves` counts states expanded (the lab's 'Moves' on a loss).
    """
    Parser, _ = engine()
    t0 = time.time()
    root = game.copy()
    seen = {root.get_efficient_hash()}
    if root.is_win():
        return {"win": True, "moves": 0, "exhausted": False, "path": [], "seconds": 0.0}
    frames = [[root, _valid(root), 0]]
    path: list[str] = []
    moves = 0
    while frames:
        if time.time() - t0 > time_limit:
            return {"win": False, "moves": moves, "exhausted": False, "path": [], "reason": "time limit", "seconds": round(time.time() - t0, 2)}
        st, acts, i = frames[-1]
        if i >= len(acts):
            frames.pop()
            if path:
                path.pop()
            continue
        frames[-1][2] += 1
        ns = st.copy()
        ns.logger.active = False
        try:
            Parser.perform_action_in_game(acts[i], ns)
        except Exception:  # noqa: BLE001
            continue
        h = ns.get_efficient_hash()
        if h in seen:
            continue
        seen.add(h)
        moves += 1
        path.append(acts[i])
        if ns.is_win():
            return {"win": True, "moves": len(path), "searched": moves, "exhausted": False, "path": list(path), "seconds": round(time.time() - t0, 2)}
        if moves >= max_moves:
            return {"win": False, "moves": moves, "exhausted": False, "path": [], "reason": "move cap", "seconds": round(time.time() - t0, 2)}
        frames.append([ns, _valid(ns), 0])
    return {"win": False, "moves": moves, "exhausted": True, "path": [], "seconds": round(time.time() - t0, 2)}


def solve_session(sid: str, max_moves=2000, time_limit=10.0):
    s = _get(sid)
    with s.lock:
        g = s.game.copy()
    return solve(g, max_moves, time_limit)


def simulate_deal(sgdl: str, seed: int, max_moves=2000, time_limit=6.0):
    """Run the DFS bot on one deal of an SGDL text (the viewer calls this once per deal)."""
    game, err = compile_sgdl(sgdl, seed)
    if err:
        return {"error": err}
    r = solve(game, max_moves, time_limit)
    r.pop("path", None)
    return dict(r, seed=seed)


def random_move(sid: str):
    s = _get(sid)
    with s.lock:
        acts = _valid(s.game)
    if not acts:
        return _get(sid).view(ok=False, summary="No valid moves.")
    return act(sid, random.choice(acts))
