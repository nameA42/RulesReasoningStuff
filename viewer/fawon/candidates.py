"""Fawon candidate solvers. Every one must return exactly what player_api.solve_plutonium returns."""
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.dirname(HERE)
sys.path.insert(0, VIEWER)
import player_api as P  # noqa: E402

P.engine()
import base as B  # noqa: E402  (engine modules, importable once player_api has set the path)
import condition as C  # noqa: E402

plutonium = P.solve_plutonium

# ---------------------------------------------------------------- rule compiler (conditions -> Python source)
COL = {B.Suit.Spades: 0, B.Suit.Clubs: 0, B.Suit.Hearts: 1, B.Suit.Diamonds: 1}
SUIT_PAIR = {"alternate_color": "COL[{a}] != COL[{b}]", "match_color": "COL[{a}] == COL[{b}]", "match": "{a} == {b}"}
RANK_PAIR = {"ascending": "{a} + 1 == {b}", "descending": "{a} == {b} + 1", "equal": "{a} == {b}",
             "add_13": "{a} + {b} == 13", "add_14": "{a} + {b} == 14"}


class Unsupported(Exception):
    pass


class Codegen:
    def __init__(self):
        self.ns = {"COL": COL, "len": len, "all": all, "any": any}
        self.n = 0

    def const(self, v):
        name = f"K{self.n}"
        self.n += 1
        self.ns[name] = v
        return name

    def pair(self, leaf, a, b):
        """Engine's _pair_comp(x, y) for a Stack/DestSrc leaf, as source; a/b are card expressions."""
        if isinstance(leaf, C.MultiSuitCondition):
            return "(" + SUIT_PAIR[str(leaf.mode.value)].format(a=f"{a}.suit", b=f"{b}.suit") + ")"
        return "(" + RANK_PAIR[str(leaf.mode.value)].format(a=f"{a}.rank", b=f"{b}.rank") + ")"

    def run_fn(self, leaf):
        name = f"RUN{self.n}"
        self.n += 1
        src = (f"def {name}(st):\n    for i in range(1, len(st)):\n        a = st[i - 1]; b = st[i]\n"
               f"        if not {self.pair(leaf, 'a', 'b')}:\n            return False\n    return True\n")
        exec(src, self.ns)
        return name

    def move_expr(self, c):
        """Expression over s (moving / leading card), dc (destination cards), st (moving stack)."""
        if c is None:
            return "True"
        if isinstance(c, C.AndSubTree):
            return "(" + " and ".join(self.move_expr(k) for k in c.subtrees) + ")"
        if isinstance(c, C.OrSubTree):
            return "(" + " or ".join(self.move_expr(k) for k in c.subtrees) + ")"
        t = type(c).__name__
        if t == "DestEmptyCondition":
            return "(not dc)"
        if t == "DestSizeCondition":
            return f"(len(dc) {c.math_op.value} {int(c.threshold)})"
        if t == "SrcSuitCondition":
            return f"(s.suit in {self.const(frozenset(c.suits))})"
        if t == "SrcRankCondition":
            return f"(s.rank in {self.const(frozenset(c.ranks))})"
        if t == "DestSuitCondition":
            return f"(bool(dc) and dc[-1].suit in {self.const(frozenset(c.suits))})"
        if t == "DestRankCondition":
            return f"(bool(dc) and dc[-1].rank in {self.const(frozenset(c.ranks))})"
        if t in ("DestSrcSuitCondition", "DestSrcRankCondition"):
            return f"(bool(dc) and {self.pair(c, 'dc[-1]', 's')})"
        if t == "StackSizeCondition":
            return f"(len(st) {c.math_op.value} {int(c.threshold)})"
        if t in ("StackSuitCondition", "StackRankCondition"):
            return f"{self.run_fn(c)}(st)"
        raise Unsupported(t)

    def pile_expr(self, c):
        """Expression over ntp (name_to_piles) and dp (draw pile) for PILE conditions."""
        if c is None:
            return "True"
        if isinstance(c, C.AndSubTree):
            return "(" + " and ".join(self.pile_expr(k) for k in c.subtrees) + ")"
        if isinstance(c, C.OrSubTree):
            return "(" + " or ".join(self.pile_expr(k) for k in c.subtrees) + ")"
        t = type(c).__name__
        if t == "PileEmptyCondition":
            test = "== 0"
        elif t == "PileSizeCondition":
            test = f"{c.math_op.value} {int(c.threshold)}"
        else:
            raise Unsupported(t)
        names = [n for n in c.pilenames if n != "DRAW"]
        parts = []
        if names:
            parts.append(f"{'all' if str(c.mode.value) == 'ALL' else 'any'}(len(p.cards) {test} for n in {self.const(tuple(names))} for p in ntp[n])")
        if "DRAW" in c.pilenames:
            parts.append(f"(dp is not None and len(dp.get_all_cards()) {test})")
        if not parts:
            return "True" if str(c.mode.value) == "ALL" else "False"
        return "(" + (" and " if str(c.mode.value) == "ALL" else " or ").join(parts) + ")"

    def function(self, args, expr):
        name = f"F{self.n}"
        self.n += 1
        exec(f"def {name}({args}):\n    return bool({expr})\n", self.ns)
        return self.ns[name]


class CompiledRules:
    """Per-game: move/stack rule functions (plutonium's cheap-first order and run-walk pruning, then compiled),
    the win test and the draw test."""
    def __init__(self, game):
        o = P.PLUTONIUM_DEFAULT
        cg = Codegen()
        self.has_draw = game.draw_pile is not None
        self.moves = [(k, cg.function("s, dc, st", cg.move_expr(P._compile_cond(cond, C, o["order"]))))
                      for k, cond in game.move_conditions.items()]
        self.stacks = []
        for k, cond in game.move_stack_conditions.items():
            req = P._required_run_leaves(cond)
            pair = None
            if req:
                pair = cg.function("a, b", " and ".join(cg.pair(leaf, "a", "b") for leaf in req))
            rest = P._compile_cond(cond, C, o["order"], req)
            self.stacks.append((k, cg.function("s, dc, st", cg.move_expr(rest)), pair))
        self.win = cg.function("ntp, dp", cg.pile_expr(P._compile_cond(game.win_conditions, C, True)))
        dc = game.draw_conditions
        self.draw_cond = cg.function("ntp, dp", cg.pile_expr(P._compile_cond(dc, C, True))) if dc is not None else None

    def can_draw(self, g):
        if self.draw_cond is not None and not self.draw_cond(g.name_to_piles, g.draw_pile):
            return False
        return g.draw_func(False)

    def is_win(self, g):
        return self.win(g.name_to_piles, g.draw_pile)


def legal_moves_compiled(g, R):
    """Same moves, same order as the engine / plutonium, using compiled rule functions."""
    if R.has_draw and R.can_draw(g):
        yield ("draw",)
    piles = g.name_to_piles
    for (src_name, dst_name), f in R.moves:
        srcs = [(g.draw_pile, "DRAW")] if src_name == "DRAW" else [(p, f"{src_name}[{p.ind}]") for p in piles.get(src_name, ())]
        dsts = piles.get(dst_name, ())
        for src, stag in srcs:
            cards = src.cards
            if not cards:
                continue
            top = cards[-1]
            if top.face_down:
                continue
            for dst in dsts:
                if dst is not src and f(top, dst.cards, None):
                    yield ("move", src_name, src, stag, dst_name, dst)
    for (src_name, dst_name), f, pair in R.stacks:
        dsts = piles.get(dst_name, ())
        for src in piles.get(src_name, ()):
            cards = src.cards
            starts = []
            for i in range(len(cards) - 2, -1, -1):
                ci = cards[i]
                if ci.face_down or (pair is not None and not pair(ci, cards[i + 1])):
                    break
                starts.append((i, cards[i:]))
            if not starts:
                continue
            stag = f"{src_name}[{src.ind}]"
            for dst in dsts:
                if dst is src:
                    continue
                dc = dst.cards
                for i, st in starts:
                    if f(st[0], dc, st):
                        yield ("stack", src_name, src, stag, dst_name, dst, i)


def solve_compiled(game, max_moves=2000, time_limit=10.0, freeze_gc=False):
    """Plutonium's search with compiled rules (moves, win, draw)."""
    if game.auto_move_conditions or game.auto_move_stack_conditions:
        return plutonium(game, max_moves, time_limit)
    try:
        R = CompiledRules(game)
    except Unsupported:
        return plutonium(game, max_moves, time_limit)
    if freeze_gc:
        gc.collect(); gc.freeze(); was = gc.isenabled(); gc.disable()
    try:
        return _search(game, R, max_moves, time_limit)
    finally:
        if freeze_gc:
            gc.unfreeze()
            if was:
                gc.enable()


def solve_compiled_gc(game, max_moves=2000, time_limit=10.0):
    return solve_compiled(game, max_moves, time_limit, freeze_gc=True)


def _search(game, R, max_moves, time_limit):
    StackCls, CardCls = B.Stack, B.Card
    t0 = time.time()
    root = game.copy()
    root.logger.active = False
    hz = P._StackHasher()
    MOD = hz.MOD
    of_cards, of_pile, cache = hz.of_cards, hz.of_pile, hz.cache
    H0 = root.get_efficient_hash()
    seen = {H0}
    done = lambda **kw: dict(kw, seconds=round(time.time() - t0, 2), algo="compiled")
    if R.is_win(root):
        return done(win=True, moves=0, exhausted=False, path=[])
    frames = [(root, legal_moves_compiled(root, R), H0)]
    path, moves = [], 0
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
        if mv[0] != "draw" and mv[1] != "DRAW":
            _, sname, src, stag, dname, dst = mv[:6]
            cards = src.cards
            i = len(cards) - 1 if mv[0] == "move" else mv[6]
            src_new = cards[:i]
            if src_new and src_new[-1].face_down:
                top = src_new[-1]
                src_new[-1] = CardCls(top.suit, top.rank, False)
            dst_new = dst.cards + cards[i:]
            hs, hd = of_cards(sname, src_new), of_cards(dname, dst_new)
            Hc = (H - of_pile(src) - of_pile(dst) + hs + hd) % MOD
            if Hc in seen:
                continue
            dtag = f"{dname}[{dst.ind}]"
            action = f"move {stag} {dtag}" if mv[0] == "move" else f"move_stack {stag}:{i} {dtag}"
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
            cache[s_pile], cache[d_pile] = hs, hd
        else:
            ns, action = P._apply_lazy(st, mv)
            Hc = ns.get_efficient_hash()
            if Hc in seen:
                continue
        seen.add(Hc)
        moves += 1
        path.append(action)
        if R.is_win(ns):
            return done(win=True, moves=len(path), searched=moves, exhausted=False, path=list(path))
        if moves >= max_moves:
            return done(win=False, moves=moves, exhausted=False, path=[], reason="move cap")
        frames.append((ns, legal_moves_compiled(ns, R), Hc))
    return done(win=False, moves=moves, exhausted=True, path=[])


# ---------------------------------------------------------------- C++ core
def solve_native(game, max_moves=2000, time_limit=10.0):
    import native
    return native.solve_native(game, max_moves, time_limit)


def solve_native_v1(game, max_moves=2000, time_limit=10.0):
    import native
    return native.solve_native(game, max_moves, time_limit, fast=False)
