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


# ---------------------------------------------------------------- "find a win on speed"
# Same search as solve() - same move order, same repeated-state pruning, same results - but it
#  1. stops walking down a pile for stack moves at the first face-down card (the engine means to,
#     but its check looks at the wrong variable, so it builds and rejects every deeper candidate), and
#     at the first card where a required run rule (SRCSTACK Suit/Rank under ANDs) already fails;
#  2. checks moves lazily, one at a time as the search reaches them, instead of listing every legal
#     move of every position up front, and validates each move once instead of twice;
#  3. checks each candidate cheaply: skips empty / face-down sources first, compares piles by
#     identity instead of by string, and evaluates the rule's condition tree directly instead of
#     building the engine's explanation objects (MoveArgs).
def _required_run_leaves(condition):
    """Stack-run leaves that must hold for the rule to pass (reachable from the root through ANDs only).
    They compare neighbouring cards, so if cards[i:] fails one, every longer run cards[j:] (j < i) fails too."""
    import condition as C
    out, todo = [], [condition]
    while todo:
        c = todo.pop()
        if isinstance(c, C.AndSubTree):
            todo.extend(c.subtrees)
        elif isinstance(c, (C.StackSuitCondition, C.StackRankCondition)):
            out.append(c)
    return out


class _Rules:
    """Per-game precomputation shared by every position of one search."""
    def __init__(self, game):
        import condition as C
        self.C = C
        self.moves = list(game.move_conditions.items())
        self.stacks = [(k, cond, _required_run_leaves(cond)) for k, cond in game.move_stack_conditions.items()]
        self.has_draw = game.draw_pile is not None


def _legal_moves_lazy(g, R):
    """Yield legal moves of position g one at a time, in exactly the engine's order (see Game.get_possible_actions)."""
    C = R.C
    if R.has_draw and g.draw(perform=False):
        yield ("draw",)
    piles = g.name_to_piles
    for (src_name, dst_name), cond in R.moves:
        srcs = [(g.draw_pile, "DRAW")] if src_name == "DRAW" else [(p, f"{src_name}[{p.ind}]") for p in piles.get(src_name, ())]
        dsts = piles.get(dst_name, ())
        for src, stag in srcs:
            cards = src.cards
            if not cards or cards[-1].face_down:
                continue  # the engine would build every (src, dest) candidate and reject each one
            top = cards[-1]
            for dst in dsts:
                if dst is src:
                    continue
                if cond.evaluate(C.MoveCardComponents(top, dst)):
                    yield ("move", src_name, src, stag, dst_name, dst)
    for (src_name, dst_name), cond, required in R.stacks:
        dsts = piles.get(dst_name, ())
        for src in piles.get(src_name, ()):
            cards = src.cards
            starts = []  # valid start indices, deepest-last, same order the engine tries them (len-2 down to 0)
            for i in range(len(cards) - 2, -1, -1):
                if cards[i].face_down:
                    break
                if required:
                    comp = C.MoveStackComponents(cards[i:], None)
                    if not all(leaf.evaluate(comp) for leaf in required):
                        break
                starts.append(i)
            if not starts:
                continue
            stag = f"{src_name}[{src.ind}]"
            for dst in dsts:
                if dst is src:
                    continue
                for i in starts:
                    if cond.evaluate(C.MoveStackComponents(cards[i:], dst)):
                        yield ("stack", src_name, src, stag, dst_name, dst, i)


def _apply_lazy(parent, mv):
    """Play move mv (found on `parent`) on a copy of parent; return (child, action string)."""
    child = parent.copy()
    child.logger.active = False
    if mv[0] == "draw":
        child.draw(perform=True)
        return child, "draw"
    _, src_name, src, stag, dst_name, dst = mv[:6]
    csrc = child.draw_pile if src_name == "DRAW" else child.name_to_piles[src_name][src.ind]
    cdst = child.name_to_piles[dst_name][dst.ind]
    dtag = f"{dst_name}[{dst.ind}]"
    if mv[0] == "move":
        cdst.add([csrc.get()])
        action = f"move {stag} {dtag}"
    else:
        i = mv[6]
        cdst.add(csrc.get_many(i))
        action = f"move_stack {stag}:{i} {dtag}"
    if child.auto_move_conditions or child.auto_move_stack_conditions:
        child.check_auto_moves()
    return child, action


def solve_speed(game, max_moves=2000, time_limit=10.0):
    """'Find a win on speed': same results as solve(), fewer and cheaper legality checks."""
    engine()
    t0 = time.time()
    root = game.copy()
    root.logger.active = False
    R = _Rules(root)
    seen = {root.get_efficient_hash()}
    if root.is_win():
        return {"win": True, "moves": 0, "exhausted": False, "path": [], "seconds": 0.0, "algo": "speed"}
    frames = [(root, _legal_moves_lazy(root, R))]
    path: list[str] = []
    moves = 0
    while frames:
        if time.time() - t0 > time_limit:
            return {"win": False, "moves": moves, "exhausted": False, "path": [], "reason": "time limit", "seconds": round(time.time() - t0, 2), "algo": "speed"}
        st, gen = frames[-1]
        mv = next(gen, None)
        if mv is None:
            frames.pop()
            if path:
                path.pop()
            continue
        ns, action = _apply_lazy(st, mv)
        h = ns.get_efficient_hash()
        if h in seen:
            continue
        seen.add(h)
        moves += 1
        path.append(action)
        if ns.is_win():
            return {"win": True, "moves": len(path), "searched": moves, "exhausted": False, "path": list(path), "seconds": round(time.time() - t0, 2), "algo": "speed"}
        if moves >= max_moves:
            return {"win": False, "moves": moves, "exhausted": False, "path": [], "reason": "move cap", "seconds": round(time.time() - t0, 2), "algo": "speed"}
        frames.append((ns, _legal_moves_lazy(ns, R)))
    return {"win": False, "moves": moves, "exhausted": True, "path": [], "seconds": round(time.time() - t0, 2), "algo": "speed"}


# ---------------------------------------------------------------- "find a win on plutonium" (v2 of on speed)
# Still the same search with the same move order and the same answers as solve()/solve_speed(). On top of
# "on speed" it can switch on (see PLUTONIUM_DEFAULT for what is kept, and why, after measuring each):
#   order   - each rule's AND/OR conditions are re-sorted once per search so cheap checks run first
#   runwalk - the stack-run walk checks one new pair of cards per depth, and run rules it has already
#             confirmed are dropped from the per-destination check
#   inchash - a move changes two piles, so the new position's hash is computed from those two piles alone;
#             a position seen before is skipped without ever being built
#   light   - a new position shares every pile the move didn't touch instead of copying the whole game
# Draws, moves out of the draw pile, and games with $auto rules fall back to a full copy + engine hash.
PLUTONIUM_DEFAULT = {"order": True, "runwalk": True, "inchash": True, "light": True}

_LEAF_COST = {"DestEmptyCondition": 1, "DestSizeCondition": 1, "SrcSuitCondition": 1, "SrcRankCondition": 1,
              "DestSuitCondition": 2, "DestRankCondition": 2, "DestSrcSuitCondition": 3, "DestSrcRankCondition": 3,
              "StackSizeCondition": 2, "StackSuitCondition": 20, "StackRankCondition": 20}


def _cond_cost(c, C):
    if isinstance(c, C.ConditionTree):
        return sum(_cond_cost(k, C) for k in c.subtrees)
    return _LEAF_COST.get(type(c).__name__, 10)


def _compile_cond(c, C, order, drop=()):
    """A copy of condition tree c (the engine's own tree is left untouched): leaves in `drop` - run rules
    already verified by the walk - are removed from the AND chain, and with `order` each AND/OR's children
    are sorted cheapest first. AND/OR are pure and short-circuit, so this never changes the result.
    Returns None for 'always true'."""
    if any(c is d for d in drop):
        return None
    if isinstance(c, (C.AndSubTree, C.OrSubTree)):
        is_and = isinstance(c, C.AndSubTree)
        kids = [_compile_cond(k, C, order, drop if is_and else ()) for k in c.subtrees]
        if is_and:
            kids = [k for k in kids if k is not None]
            if not kids:
                return None
        elif any(k is None for k in kids):
            return None
        if order:
            kids.sort(key=lambda k: _cond_cost(k, C))  # stable: equal-cost checks keep file order
        node = C.AndSubTree() if is_and else C.OrSubTree()
        node.subtrees = kids
        return node
    return c


class _PuRules:
    def __init__(self, game, o):
        import condition as C
        self.C, self.o = C, o
        self.has_draw = game.draw_pile is not None
        self.moves = [(k, _compile_cond(cond, C, o["order"])) for k, cond in game.move_conditions.items()]
        self.stacks = []
        for k, cond in game.move_stack_conditions.items():
            req = _required_run_leaves(cond)
            pairs = []
            for leaf in req:
                if isinstance(leaf, C.StackSuitCondition):
                    pairs.append(lambda a, b, f=leaf._pair_comp: f(a.suit, b.suit))
                else:
                    pairs.append(lambda a, b, f=leaf._pair_comp: f(a.rank, b.rank))
            compiled = _compile_cond(cond, C, o["order"], req if o["runwalk"] else ())
            self.stacks.append((k, compiled, req, pairs))


def _legal_moves_pu(g, R):
    """Same moves in the same order as _legal_moves_lazy, with compiled conditions and reused components."""
    C, runwalk = R.C, R.o["runwalk"]
    if R.has_draw and g.draw(perform=False):
        yield ("draw",)
    piles = g.name_to_piles
    for (src_name, dst_name), cond in R.moves:
        srcs = [(g.draw_pile, "DRAW")] if src_name == "DRAW" else [(p, f"{src_name}[{p.ind}]") for p in piles.get(src_name, ())]
        dsts = piles.get(dst_name, ())
        for src, stag in srcs:
            cards = src.cards
            if not cards or cards[-1].face_down:
                continue
            comp = C.MoveCardComponents(cards[-1], None)
            for dst in dsts:
                if dst is src:
                    continue
                comp.destination = dst
                if cond is None or cond.evaluate(comp):
                    yield ("move", src_name, src, stag, dst_name, dst)
    for (src_name, dst_name), cond, required, pairs in R.stacks:
        dsts = piles.get(dst_name, ())
        for src in piles.get(src_name, ()):
            cards = src.cards
            comps = []
            for i in range(len(cards) - 2, -1, -1):
                ci = cards[i]
                if ci.face_down:
                    break
                if runwalk:
                    nxt = cards[i + 1]
                    if pairs and not all(p(ci, nxt) for p in pairs):
                        break
                    comps.append((i, C.MoveStackComponents(cards[i:], None)))
                else:
                    comp = C.MoveStackComponents(cards[i:], None)
                    if required and not all(leaf.evaluate(comp) for leaf in required):
                        break
                    comps.append((i, comp))
            if not comps:
                continue
            stag = f"{src_name}[{src.ind}]"
            for dst in dsts:
                if dst is src:
                    continue
                for i, comp in comps:
                    comp.destination = dst
                    if cond is None or cond.evaluate(comp):
                        yield ("stack", src_name, src, stag, dst_name, dst, i)


class _StackHasher:
    """Reproduces the engine's Stack.get_efficient_hash exactly, with a per-pile cache (piles are never
    mutated once a search has hashed them: new positions get new pile objects)."""
    def __init__(self):
        import base as B
        self.MOD = B.Hashable.MOD
        self.B1 = B.Card.get_max_efficient_hash() + 1
        self.PMAX = B.Pile.get_max_efficient_hash()  # computed once (the older engine recomputes it per call)
        self.suit = {B.Suit.Clubs: 0, B.Suit.Diamonds: 1, B.Suit.Hearts: 2, B.Suit.Spades: 3}
        self.names, self.cache = {}, {}
        self.str_hash = B.Hashable.get_str_hash

    def of_cards(self, name, cards):
        nh = self.names.get(name)
        if nh is None:
            nh = self.names[name] = self.str_hash(name)
        n, B1, MOD, su = 0, self.B1, self.MOD, self.suit
        for c in cards:
            n = (n * B1 + (su[c.suit] * 13 + c.rank - 1) * 2 + (1 if c.face_down else 0)) % MOD
        return (n * nh + self.PMAX) % MOD

    def of_pile(self, pile):
        h = self.cache.get(pile)
        if h is None:
            h = self.cache[pile] = self.of_cards(pile.name, pile.cards)
        return h


def solve_plutonium(game, max_moves=2000, time_limit=10.0, opts=None, verify=False):
    """'Find a win on plutonium': same results as solve()/solve_speed(); verify=True checks every
    incrementally computed hash against the engine's own."""
    o = dict(PLUTONIUM_DEFAULT, **(opts or {}))
    if o["light"]:
        o["inchash"] = True
    engine()
    import base as B
    StackCls, CardCls = B.Stack, B.Card
    t0 = time.time()
    root = game.copy()
    root.logger.active = False
    R = _PuRules(root, o)
    hz = _StackHasher()
    MOD = hz.MOD
    fast_ok = o["inchash"] and not (root.auto_move_conditions or root.auto_move_stack_conditions)
    H0 = root.get_efficient_hash()
    seen = {H0}
    done = lambda **kw: dict(kw, seconds=round(time.time() - t0, 2), algo="plutonium")
    if root.is_win():
        return done(win=True, moves=0, exhausted=False, path=[])
    frames = [(root, _legal_moves_pu(root, R), H0)]
    path: list[str] = []
    moves = 0
    while frames:
        if time.time() - t0 > time_limit:
            return done(win=False, moves=moves, exhausted=False, path=[], reason="time limit")
        st, gen, H = frames[-1]
        mv = next(gen, None)
        if mv is None:
            frames.pop()
            if path:
                path.pop()
            continue
        if fast_ok and mv[0] != "draw" and mv[1] != "DRAW":
            _, sname, src, stag, dname, dst = mv[:6]
            cards = src.cards
            i = len(cards) - 1 if mv[0] == "move" else mv[6]
            src_new = cards[:i]
            if src_new and src_new[-1].face_down:  # the engine turns the new top card face up
                top = src_new[-1]
                src_new[-1] = CardCls(top.suit, top.rank, False)
            dst_new = dst.cards + cards[i:]
            hs, hd = hz.of_cards(sname, src_new), hz.of_cards(dname, dst_new)
            Hc = (H - hz.of_pile(src) - hz.of_pile(dst) + hs + hd) % MOD
            if Hc in seen:
                continue  # never built
            dtag = f"{dname}[{dst.ind}]"
            action = f"move {stag} {dtag}" if mv[0] == "move" else f"move_stack {stag}:{i} {dtag}"
            if o["light"]:
                ns = object.__new__(type(st))
                ns.__dict__.update(st.__dict__)
                ntp = dict(st.name_to_piles)
                s_pile, d_pile = StackCls(src_new, sname, src.ind), StackCls(dst_new, dname, dst.ind)
                lst = ntp[sname] = list(ntp[sname])
                lst[src.ind] = s_pile
                lst = ntp[dname] if dname == sname else list(ntp[dname])
                lst[dst.ind] = d_pile
                ntp[dname] = lst
                ns.name_to_piles = ntp
                hz.cache[s_pile], hz.cache[d_pile] = hs, hd
            else:
                ns, _ = _apply_lazy(st, mv)
            if verify:
                assert Hc == ns.get_efficient_hash(), f"incremental hash mismatch after {action}"
        else:
            ns, action = _apply_lazy(st, mv)
            Hc = ns.get_efficient_hash()
            if Hc in seen:
                continue
        seen.add(Hc)
        moves += 1
        path.append(action)
        if ns.is_win():
            return done(win=True, moves=len(path), searched=moves, exhausted=False, path=list(path))
        if moves >= max_moves:
            return done(win=False, moves=moves, exhausted=False, path=[], reason="move cap")
        frames.append((ns, _legal_moves_pu(ns, R), Hc))
    return done(win=False, moves=moves, exhausted=True, path=[])


def solve_neutronium(game, max_moves=2000, time_limit=10.0):
    """'Find a win on neutronium' (Fawon): the same search in a portable C++ core (fawon/core), same results as
    solve_plutonium. Falls back to plutonium (identical results, Python speed) if the library isn't built
    (`python fawon/native.py build`) or the game uses rules the core doesn't support ($auto)."""
    fawon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fawon")
    if fawon_dir not in sys.path:
        sys.path.insert(0, fawon_dir)
    import native
    return native.solve_native(game, max_moves, time_limit)


SOLVERS = {"plain": solve, "speed": solve_speed, "plutonium": solve_plutonium, "neutronium": solve_neutronium}


def solve_session(sid: str, max_moves=2000, time_limit=10.0, speed=False, algo=None):
    s = _get(sid)
    with s.lock:
        g = s.game.copy()
    return SOLVERS[algo or ("speed" if speed else "plain")](g, max_moves, time_limit)


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
