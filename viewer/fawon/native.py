"""Python side of the Fawon C++ core: export a dealt game + compiled rules, call the library, decode the result.

Portable: loads core/fawon_core.dll (Windows) or core/libfawon_core.so / .dylib (Linux / macOS) with ctypes.
`python native.py build` compiles it with whatever C++ compiler the machine has (MSVC, g++ or clang++).
Games the core doesn't support ($auto rules) fall back to the Python solver with identical results.
"""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "core")
sys.path.insert(0, os.path.dirname(HERE))
import player_api as P  # noqa: E402

P.engine()
import base as B  # noqa: E402
import condition as C  # noqa: E402

LIBNAME = {"win32": "fawon_core.dll", "darwin": "libfawon_core.dylib"}.get(sys.platform, "libfawon_core.so")
SUITNUM = {B.Suit.Clubs: 0, B.Suit.Diamonds: 1, B.Suit.Hearts: 2, B.Suit.Spades: 3}
OPS = {"==": 0, ">": 1, "<": 2, ">=": 3, "<=": 4}
SUIT_MODE = {"alternate_color": 0, "match_color": 1, "match": 2}
RANK_MODE = {"ascending": 0, "descending": 1, "equal": 2, "add_13": 3, "add_14": 4}
STATUS = {0: "exhausted", 1: "win", 2: "move cap", 3: "time limit"}


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- building the library (any OS, any compiler)
def build(verbose=True):
    import glob, shutil, subprocess  # build-only: not loaded during normal runs
    src = os.path.join(CORE, "fawon_core.cpp")
    out = os.path.join(CORE, LIBNAME)
    if sys.platform == "win32" and shutil.which("cl") is None:
        vswhere = os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe")
        if not os.path.exists(vswhere):
            raise RuntimeError("No C++ compiler found: install Visual Studio Build Tools (C++ workload)")
        vs = subprocess.run([vswhere, "-latest", "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                            capture_output=True, text=True).stdout.strip()
        vcvars = os.path.join(vs, "VC", "Auxiliary", "Build", "vcvars64.bat")
        cmd = f'call "{vcvars}" >nul && cl /nologo /O2 /LD /EHsc /std:c++17 "{src}" /Fe:"{out}" /Fo:"{CORE}\\\\"'
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=CORE)
    elif sys.platform == "win32":
        r = subprocess.run(["cl", "/nologo", "/O2", "/LD", "/EHsc", "/std:c++17", src, f"/Fe:{out}"], capture_output=True, text=True, cwd=CORE)
    else:
        cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if not cxx:
            raise RuntimeError("No C++ compiler found (set CXX or install g++/clang++)")
        r = subprocess.run([cxx, "-O2", "-shared", "-fPIC", "-std=c++17", src, "-o", out], capture_output=True, text=True)
    for junk in glob.glob(os.path.join(CORE, "*.obj")) + glob.glob(os.path.join(CORE, "*.exp")) + glob.glob(os.path.join(CORE, "*.lib")):
        os.remove(junk)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("C++ build failed:\n" + r.stdout + r.stderr)
    if verbose:
        print("built", out)
    return out


_lib = None


class LibraryMissing(RuntimeError):
    pass


def available():
    """True if the compiled library exists. Nothing is ever built implicitly: run `python native.py build`."""
    return os.path.exists(os.path.join(CORE, LIBNAME))


def lib():
    global _lib
    if _lib is None:
        path = os.path.join(CORE, LIBNAME)
        if not os.path.exists(path):
            raise LibraryMissing(f"{path} not found - build it once with: python native.py build")
        L = ctypes.CDLL(path)
        L.fawon_solve.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.POINTER(ctypes.c_int64),
                                  ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int, ctypes.c_double]
        L.fawon_solve.restype = ctypes.c_int
        L.fawon_solve_fast.argtypes = L.fawon_solve.argtypes
        L.fawon_solve_fast.restype = ctypes.c_int
        L.fawon_root_hash.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.POINTER(ctypes.c_int64)]
        L.fawon_root_hash.restype = ctypes.c_int64
        _lib = L
    return _lib


# ---------------------------------------------------------------- exporting a game
def _card(c):
    return SUITNUM[c.suit] * 13 + c.rank - 1 + (64 if c.face_down else 0)


def _cards(cards):
    return [len(cards)] + [_card(c) for c in cards]


def _node(op, body):
    return [op, len(body) + 2] + body


def _move_code(c):
    if c is None:
        return _node(0, [])
    if isinstance(c, (C.AndSubTree, C.OrSubTree)):
        kids = [_move_code(k) for k in c.subtrees]
        return _node(1 if isinstance(c, C.AndSubTree) else 2, [len(kids)] + [x for k in kids for x in k])
    t = type(c).__name__
    if t == "DestEmptyCondition":
        return _node(10, [])
    if t in ("DestSizeCondition", "StackSizeCondition"):
        return _node(11 if t[0] == "D" else 18, [OPS[str(c.math_op.value)], int(c.threshold)])
    if t in ("SrcSuitCondition", "DestSuitCondition"):
        return _node(12 if t[0] == "S" else 14, [sum(1 << SUITNUM[s] for s in set(c.suits))])
    if t in ("SrcRankCondition", "DestRankCondition"):
        return _node(13 if t[0] == "S" else 15, [sum(1 << r for r in set(c.ranks))])
    if t == "DestSrcSuitCondition":
        return _node(16, [SUIT_MODE[str(c.mode.value)]])
    if t == "DestSrcRankCondition":
        return _node(17, [RANK_MODE[str(c.mode.value)]])
    if t == "StackSuitCondition":
        return _node(19, [SUIT_MODE[str(c.mode.value)]])
    if t == "StackRankCondition":
        return _node(20, [RANK_MODE[str(c.mode.value)]])
    raise Unsupported(t)


def _pile_code(c, types):
    if c is None:
        return _node(0, [])
    if isinstance(c, (C.AndSubTree, C.OrSubTree)):
        kids = [_pile_code(k, types) for k in c.subtrees]
        return _node(1 if isinstance(c, C.AndSubTree) else 2, [len(kids)] + [x for k in kids for x in k])
    t = type(c).__name__
    names = [types[n] for n in c.pilenames if n != "DRAW"]
    head = [1 if str(c.mode.value) == "ALL" else 0, 1 if "DRAW" in c.pilenames else 0]
    if t == "PileEmptyCondition":
        return _node(30, head + [len(names)] + names)
    if t == "PileSizeCondition":
        return _node(31, head + [OPS[str(c.math_op.value)], int(c.threshold), len(names)] + names)
    raise Unsupported(t)


def export(game):
    """(int32 list, int64 list, pile tags) describing the dealt game for the C++ core."""
    if game.auto_move_conditions or game.auto_move_stack_conditions:
        raise Unsupported("$auto rules")
    o = P.PLUTONIUM_DEFAULT
    hz = P._StackHasher()
    names = list(game.name_to_piles.keys())
    types = {n: i for i, n in enumerate(names)}
    i32, tags = [len(names)], []
    piles = [(n, p) for n in names for p in game.name_to_piles[n]]
    i32.append(len(piles))
    gidx = {}
    for k, (n, p) in enumerate(piles):
        gidx[id(p)] = k
        tags.append(f"{n}[{p.ind}]")
        i32 += [types[n]] + _cards(p.cards)
    dp = game.draw_pile
    if dp is None:
        i32.append(0)
    elif isinstance(dp, B.DealPile):
        i32 += [1] + _cards(dp.cards)
        tgt = [types[n] for n in dp.target_names if n in types]  # Python-set order, as the engine deals
        i32 += [len(tgt)] + tgt
    else:
        i32 += [2, dp.draw_count, -1 if dp.view_count is None else dp.view_count,
                -1 if dp.max_redeals is None else dp.max_redeals, dp.redeals]
        i32 += _cards(dp.backpile) + _cards(dp.cards) + _cards(dp.drawn)
    i32.append(len(game.move_conditions))
    for (s, t), cond in game.move_conditions.items():
        code = _move_code(P._compile_cond(cond, C, o["order"]))
        i32 += [-1 if s == "DRAW" else types[s], types[t], 0, len(code)] + code
    i32.append(len(game.move_stack_conditions))
    for (s, t), cond in game.move_stack_conditions.items():
        req = P._required_run_leaves(cond)
        pairs = []
        for leaf in req:
            pairs += [0, SUIT_MODE[str(leaf.mode.value)]] if isinstance(leaf, C.StackSuitCondition) else [1, RANK_MODE[str(leaf.mode.value)]]
        code = _move_code(P._compile_cond(cond, C, o["order"], req))
        i32 += [types[s], types[t], len(req)] + pairs + [len(code)] + code
    win = _pile_code(P._compile_cond(game.win_conditions, C, True), types)
    i32 += [len(win)] + win
    if game.draw_conditions is not None:
        dc = _pile_code(P._compile_cond(game.draw_conditions, C, True), types)
        i32 += [1, len(dc)] + dc
    else:
        i32.append(0)
    s = B.Hashable.get_str_hash
    i64 = [hz.MOD, hz.B1, hz.PMAX, s(game.name), s("Draw Pile (DEAL)"), s("Draw Pile (ROTATE)")] + [s(n) for n in names]
    return i32, i64, tags


def _arrays(i32, i64):
    return (ctypes.c_int32 * len(i32))(*i32), (ctypes.c_int64 * len(i64))(*i64)


def root_hash(game):
    i32, i64, _ = export(game)
    a, b = _arrays(i32, i64)
    return lib().fawon_root_hash(a, len(i32), b)


def solve_native(game, max_moves=2000, time_limit=10.0, fast=True):
    """Same results as player_api.solve_plutonium, computed by the C++ core."""
    fallback = None
    if not available():
        fallback = "C++ library not built"
    else:
        try:
            i32, i64, tags = export(game)
        except Unsupported as e:
            fallback = f"unsupported by the C++ core ({e})"
    if fallback:  # identical results, Python speed
        return dict(P.solve_plutonium(game, max_moves, time_limit), algo="neutronium", fallback=fallback)
    a, b = _arrays(i32, i64)
    nout = 4 + 4 * (max_moves + 2)
    out = (ctypes.c_int32 * nout)()
    import time
    t0 = time.time()
    fn = lib().fawon_solve_fast if fast else lib().fawon_solve
    status = fn(a, len(i32), b, out, nout, max_moves, float(time_limit))
    secs = round(time.time() - t0, 2)
    if status < 0:
        raise RuntimeError("fawon core rejected the exported game")
    moves, searched, n = out[1], out[2], out[3]
    tag = lambda k: "DRAW" if k < 0 else tags[k]
    path = []
    for j in range(n):
        kind, src, dst, i = out[4 + 4 * j: 8 + 4 * j]
        path.append("draw" if kind == 0 else f"move {tag(src)} {tag(dst)}" if kind == 1 else f"move_stack {tag(src)}:{i} {tag(dst)}")
    r = dict(win=status == 1, moves=moves, exhausted=status == 0, path=path, seconds=secs, algo="neutronium")
    if status == 1 and moves > 0:
        r["searched"] = searched
    if status in (2, 3):
        r["reason"] = STATUS[status]
    return r


if __name__ == "__main__" and sys.argv[1:] == ["build"]:
    build()
