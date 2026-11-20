// Fawon core: the "find a win" depth-first search in portable C++17 (no platform-specific code, no CPU intrinsics).
// It reproduces the Python engine + plutonium search exactly: same move order, same 30-bit position hash,
// same win / move-cap / exhausted outcomes. Python parses the SGDL, deals the cards and hands this code the
// dealt position and the compiled rules as a flat integer array (see ../native.py for the format).
//
// Build:
//   Windows (MSVC):  cl /O2 /LD /EHsc /std:c++17 fawon_core.cpp /Fe:fawon_core.dll
//   Linux / macOS:   g++ -O2 -shared -fPIC -std=c++17 fawon_core.cpp -o libfawon_core.so   (or clang++)
#include <chrono>
#include <cstdint>
#include <unordered_set>
#include <vector>

#if defined(_WIN32)
#define FAWON_API extern "C" __declspec(dllexport)
#else
#define FAWON_API extern "C" __attribute__((visibility("default")))
#endif

typedef uint8_t Card;  // bits 0-5: suit*13 + (rank-1) with suits C=0 D=1 H=2 S=3 (engine order); bit 6: face down
static inline int suit_of(Card c) { return (c & 63) / 13; }
static inline int rank_of(Card c) { return (c & 63) % 13 + 1; }
static inline bool is_down(Card c) { return (c & 64) != 0; }
static inline Card face_up(Card c) { return c & 63; }
static inline int red(int s) { return s == 1 || s == 2; }  // diamonds, hearts

enum Op { OP_TRUE = 0, OP_AND = 1, OP_OR = 2,
          DEST_EMPTY = 10, DEST_SIZE = 11, SRC_SUIT = 12, SRC_RANK = 13, DEST_SUIT = 14, DEST_RANK = 15,
          DS_SUIT = 16, DS_RANK = 17, STK_SIZE = 18, STK_SUIT = 19, STK_RANK = 20,
          PILE_EMPTY = 30, PILE_SIZE = 31 };

struct Mv { int8_t kind; int16_t src, dst; int16_t i; };  // kind 0 draw, 1 move, 2 stack; src -1 = DRAW

struct State {
    std::vector<std::vector<Card>> piles;
    std::vector<Card> dcards, back, drawn;  // draw pile: DEAL uses dcards; ROTATE uses all three
    int redeals = 0;
};

struct Rule { int src, dst; std::vector<int> code; std::vector<std::pair<int, int>> pairs; };

struct Ctx {
    uint64_t MOD, B1, PMAX, game_h, deal_h, rot_h;
    std::vector<uint64_t> type_h;
    std::vector<std::vector<int>> type_piles;
    std::vector<int> pile_type;
    int draw_kind = 0;  // 0 none, 1 deal, 2 rotate
    std::vector<int> deal_targets;
    int draw_count = 1, view_count = -1, max_redeals = -1;
    std::vector<Rule> moves, stacks;
    std::vector<int> win, drawcond;
    bool has_drawcond = false;
};

static inline bool cmp(int op, int v, int t) {
    switch (op) { case 0: return v == t; case 1: return v > t; case 2: return v < t; case 3: return v >= t; default: return v <= t; }
}
static inline bool pair_suit(int mode, Card a, Card b) {
    int sa = suit_of(a), sb = suit_of(b);
    if (mode == 0) return red(sa) != red(sb);
    if (mode == 1) return red(sa) == red(sb);
    return sa == sb;
}
static inline bool pair_rank(int mode, Card a, Card b) {
    int ra = rank_of(a), rb = rank_of(b);
    switch (mode) { case 0: return ra + 1 == rb; case 1: return ra == rb + 1; case 2: return ra == rb;
                    case 3: return ra + rb == 13; default: return ra + rb == 14; }
}

// Node layout: [op, total_len, ...]. AND/OR: [op, len, nchildren, child...]. Leaves: [op, len, params...].
static bool eval_move(const int* c, Card s, const std::vector<Card>& dc, const Card* st, int stn) {
    switch (c[0]) {
    case OP_TRUE: return true;
    case OP_AND: { const int* k = c + 3; for (int j = 0; j < c[2]; ++j) { if (!eval_move(k, s, dc, st, stn)) return false; k += k[1]; } return true; }
    case OP_OR: { const int* k = c + 3; for (int j = 0; j < c[2]; ++j) { if (eval_move(k, s, dc, st, stn)) return true; k += k[1]; } return false; }
    case DEST_EMPTY: return dc.empty();
    case DEST_SIZE: return cmp(c[2], (int)dc.size(), c[3]);
    case SRC_SUIT: return (c[2] >> suit_of(s)) & 1;
    case SRC_RANK: return (c[2] >> rank_of(s)) & 1;
    case DEST_SUIT: return !dc.empty() && ((c[2] >> suit_of(dc.back())) & 1);
    case DEST_RANK: return !dc.empty() && ((c[2] >> rank_of(dc.back())) & 1);
    case DS_SUIT: return !dc.empty() && pair_suit(c[2], dc.back(), s);
    case DS_RANK: return !dc.empty() && pair_rank(c[2], dc.back(), s);
    case STK_SIZE: return cmp(c[2], stn, c[3]);
    case STK_SUIT: for (int i = 1; i < stn; ++i) if (!pair_suit(c[2], st[i - 1], st[i])) return false; return true;
    case STK_RANK: for (int i = 1; i < stn; ++i) if (!pair_rank(c[2], st[i - 1], st[i])) return false; return true;
    }
    return false;
}

static int draw_size(const Ctx& X, const State& S) {
    return X.draw_kind == 2 ? (int)(S.dcards.size() + S.back.size() + S.drawn.size()) : (int)S.dcards.size();
}

// PILE_EMPTY: [op, len, all, hasdraw, ntypes, types...]; PILE_SIZE: [op, len, all, hasdraw, cmp, thr, ntypes, types...]
static bool eval_pile(const Ctx& X, const int* c, const State& S) {
    switch (c[0]) {
    case OP_TRUE: return true;
    case OP_AND: { const int* k = c + 3; for (int j = 0; j < c[2]; ++j) { if (!eval_pile(X, k, S)) return false; k += k[1]; } return true; }
    case OP_OR: { const int* k = c + 3; for (int j = 0; j < c[2]; ++j) { if (eval_pile(X, k, S)) return true; k += k[1]; } return false; }
    case PILE_EMPTY: case PILE_SIZE: {
        bool all = c[2] != 0, hasdraw = c[3] != 0;
        int op = 0, thr = 0; const int* t = c + 4;
        if (c[0] == PILE_SIZE) { op = c[4]; thr = c[5]; t = c + 6; }
        int nt = t[0];
        bool any_true = false, all_true = true;
        for (int j = 0; j < nt; ++j)
            for (int p : X.type_piles[t[1 + j]]) {
                int n = (int)S.piles[p].size();
                bool v = c[0] == PILE_EMPTY ? n == 0 : cmp(op, n, thr);
                any_true |= v; all_true &= v;
            }
        if (hasdraw) {
            bool v = X.draw_kind != 0 && (c[0] == PILE_EMPTY ? draw_size(X, S) == 0 : cmp(op, draw_size(X, S), thr));
            any_true |= v; all_true &= v;
        }
        return all ? all_true : any_true;
    }
    }
    return false;
}

static uint64_t poly(const Ctx& X, const std::vector<Card>& v) {
    uint64_t n = 0;
    for (Card c : v) n = (n * X.B1 + (uint64_t)((c & 63) * 2 + (is_down(c) ? 1 : 0))) % X.MOD;
    return n;
}

static uint64_t game_hash(const Ctx& X, const State& S) {
    uint64_t num = X.game_h;
    if (X.draw_kind == 1) {
        uint64_t dh = (poly(X, S.dcards) * X.deal_h + X.PMAX) % X.MOD;
        num = (num * X.PMAX + dh) % X.MOD;
    } else if (X.draw_kind == 2) {
        uint64_t dh = X.rot_h;
        dh = (dh * X.PMAX + poly(X, S.back)) % X.MOD;
        dh = (dh * X.PMAX + poly(X, S.dcards)) % X.MOD;
        dh = (dh * X.PMAX + poly(X, S.drawn)) % X.MOD;
        num = (num * X.PMAX + dh) % X.MOD;
    }
    num = (num * X.PMAX) % X.MOD;
    for (size_t p = 0; p < S.piles.size(); ++p)
        num = (num + (poly(X, S.piles[p]) * X.type_h[X.pile_type[p]] + X.PMAX) % X.MOD) % X.MOD;
    return num;
}

static bool can_draw(const Ctx& X, const State& S) {
    if (X.draw_kind == 0) return false;
    if (X.has_drawcond && !eval_pile(X, X.drawcond.data(), S)) return false;
    if (X.draw_kind == 1) return !S.dcards.empty();
    return !S.back.empty() || X.max_redeals < 0 || S.redeals < X.max_redeals;
}

static void legal_moves(const Ctx& X, const State& S, std::vector<Mv>& out) {
    out.clear();
    if (can_draw(X, S)) out.push_back({0, -1, -1, 0});
    for (const Rule& r : X.moves) {
        auto try_src = [&](int src, const std::vector<Card>& cards) {
            if (cards.empty() || is_down(cards.back())) return;
            Card top = cards.back();
            for (int dst : X.type_piles[r.dst])
                if (dst != src && eval_move(r.code.data(), top, S.piles[dst], nullptr, 0))
                    out.push_back({1, (int16_t)src, (int16_t)dst, 0});
        };
        if (r.src < 0) { if (X.draw_kind != 0) try_src(-1, S.dcards); }
        else for (int src : X.type_piles[r.src]) try_src(src, S.piles[src]);
    }
    std::vector<int> starts;
    for (const Rule& r : X.stacks) {
        for (int src : X.type_piles[r.src]) {
            const std::vector<Card>& cards = S.piles[src];
            int n = (int)cards.size();
            starts.clear();
            for (int i = n - 2; i >= 0; --i) {
                if (is_down(cards[i])) break;
                bool ok = true;
                for (auto& pr : r.pairs)
                    if (!(pr.first == 0 ? pair_suit(pr.second, cards[i], cards[i + 1]) : pair_rank(pr.second, cards[i], cards[i + 1]))) { ok = false; break; }
                if (!ok) break;
                starts.push_back(i);
            }
            if (starts.empty()) continue;
            for (int dst : X.type_piles[r.dst]) {
                if (dst == src) continue;
                for (int i : starts)
                    if (eval_move(r.code.data(), cards[i], S.piles[dst], cards.data() + i, n - i))
                        out.push_back({2, (int16_t)src, (int16_t)dst, (int16_t)i});
            }
        }
    }
}

static void apply(const Ctx& X, State& S, const Mv& m) {
    if (m.kind == 0) {
        if (X.draw_kind == 1) {
            for (int t : X.deal_targets)
                for (int p : X.type_piles[t])
                    if (!S.dcards.empty()) { Card c = S.dcards.back(); S.dcards.pop_back(); S.piles[p].push_back(face_up(c)); }
        } else if (!S.back.empty()) {
            size_t k = (size_t)X.draw_count < S.back.size() ? (size_t)X.draw_count : S.back.size();
            for (size_t j = 0; j < k; ++j) {
                S.dcards.push_back(face_up(S.back.front()));
                S.back.erase(S.back.begin());
                if (X.view_count >= 0 && (int)S.dcards.size() > X.view_count) { S.drawn.push_back(S.dcards.front()); S.dcards.erase(S.dcards.begin()); }
            }
        } else {
            S.redeals += 1;
            std::vector<Card> nb = S.drawn;
            nb.insert(nb.end(), S.dcards.begin(), S.dcards.end());
            for (Card& c : nb) c = (Card)(c | 64);
            S.back.swap(nb); S.dcards.clear(); S.drawn.clear();
        }
        return;
    }
    std::vector<Card>& dst = S.piles[m.dst];
    if (m.kind == 1) {
        if (m.src < 0) { dst.push_back(S.dcards.back()); S.dcards.pop_back(); return; }  // draw pile: no flip
        std::vector<Card>& src = S.piles[m.src];
        dst.push_back(src.back()); src.pop_back();
        if (!src.empty()) src.back() = face_up(src.back());
        return;
    }
    std::vector<Card>& src = S.piles[m.src];
    dst.insert(dst.end(), src.begin() + m.i, src.end());
    src.resize(m.i);
    if (!src.empty()) src.back() = face_up(src.back());
}

static int run_search(const Ctx& X, const State& S, int32_t* out, int nout, int max_moves, double time_limit);
static int run_search_fast(const Ctx& X, const State& S, int32_t* out, int nout, int max_moves, double time_limit);

// ---------------------------------------------------------------- input decoding
struct Reader {
    const int32_t* p; int n, i = 0;
    int next() { return i < n ? p[i++] : 0; }
    std::vector<int> block() { int len = next(); std::vector<int> v(p + i, p + i + len); i += len; return v; }
    std::vector<Card> cards() { int len = next(); std::vector<Card> v; v.reserve(len); for (int j = 0; j < len; ++j) v.push_back((Card)next()); return v; }
};

static bool decode(const int32_t* in, int nin, const int64_t* in64, Ctx& X, State& S);

// Self-check: hash of the decoded starting position (must equal the engine's Game.get_efficient_hash()).
FAWON_API int64_t fawon_root_hash(const int32_t* in, int nin, const int64_t* in64) {
    Ctx X; State S;
    if (!decode(in, nin, in64, X, S)) return -1;
    return (int64_t)game_hash(X, S);
}

// out: [status (0 exhausted, 1 win, 2 move cap, 3 time limit, -1 bad input), moves, searched, pathlen, path (kind, src, dst, i)...]
FAWON_API int fawon_solve(const int32_t* in, int nin, const int64_t* in64, int32_t* out, int nout, int max_moves, double time_limit) {
    Ctx X; State S;
    if (!decode(in, nin, in64, X, S)) { out[0] = -1; return -1; }
    return run_search(X, S, out, nout, max_moves, time_limit);
}

// Same search and results; positions are hashed incrementally and only built when not seen before.
FAWON_API int fawon_solve_fast(const int32_t* in, int nin, const int64_t* in64, int32_t* out, int nout, int max_moves, double time_limit) {
    Ctx X; State S;
    if (!decode(in, nin, in64, X, S)) { out[0] = -1; return -1; }
    return run_search_fast(X, S, out, nout, max_moves, time_limit);
}

static bool decode(const int32_t* in, int nin, const int64_t* in64, Ctx& X, State& S) {
    X.MOD = (uint64_t)in64[0]; X.B1 = (uint64_t)in64[1]; X.PMAX = (uint64_t)in64[2];
    X.game_h = (uint64_t)in64[3]; X.deal_h = (uint64_t)in64[4]; X.rot_h = (uint64_t)in64[5];
    Reader R{in, nin};
    int ntypes = R.next();
    X.type_piles.resize(ntypes);
    for (int t = 0; t < ntypes; ++t) X.type_h.push_back((uint64_t)in64[6 + t]);
    int npiles = R.next();
    for (int p = 0; p < npiles; ++p) {
        int type = R.next();
        X.pile_type.push_back(type); X.type_piles[type].push_back(p);
        S.piles.push_back(R.cards());
    }
    X.draw_kind = R.next();
    if (X.draw_kind == 1) { S.dcards = R.cards(); X.deal_targets = R.block(); }
    else if (X.draw_kind == 2) {
        X.draw_count = R.next(); X.view_count = R.next(); X.max_redeals = R.next(); S.redeals = R.next();
        S.back = R.cards(); S.dcards = R.cards(); S.drawn = R.cards();
    }
    for (int pass = 0; pass < 2; ++pass) {
        int nr = R.next();
        for (int k = 0; k < nr; ++k) {
            Rule r; r.src = R.next(); r.dst = R.next();
            int np = R.next();
            for (int j = 0; j < np; ++j) { int kind = R.next(); int mode = R.next(); r.pairs.push_back({kind, mode}); }
            r.code = R.block();
            (pass == 0 ? X.moves : X.stacks).push_back(r);
        }
    }
    X.win = R.block();
    X.has_drawcond = R.next() != 0;
    if (X.has_drawcond) X.drawcond = R.block();
    return R.i == nin;
}

static int run_search(const Ctx& X, const State& S, int32_t* out, int nout, int max_moves, double time_limit) {
    auto t0 = std::chrono::steady_clock::now();
    auto elapsed = [&] { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
    std::unordered_set<uint32_t> seen;
    seen.reserve((size_t)max_moves * 4 + 16);
    seen.insert((uint32_t)game_hash(X, S));
    auto finish = [&](int status, int moves, int searched, const std::vector<Mv>& path) {
        out[0] = status; out[1] = moves; out[2] = searched;
        int room = (nout - 4) / 4, n = (int)path.size() < room ? (int)path.size() : room;
        out[3] = n;
        for (int j = 0; j < n; ++j) { out[4 + 4 * j] = path[j].kind; out[5 + 4 * j] = path[j].src; out[6 + 4 * j] = path[j].dst; out[7 + 4 * j] = path[j].i; }
        return status;
    };
    std::vector<Mv> path;
    if (eval_pile(X, X.win.data(), S)) return finish(1, 0, 0, path);
    struct Frame { State s; std::vector<Mv> mv; size_t next; };
    std::vector<Frame> frames;
    frames.push_back({S, {}, 0});
    legal_moves(X, frames.back().s, frames.back().mv);
    int moves = 0;
    while (!frames.empty()) {
        if (elapsed() > time_limit) return finish(3, moves, moves, {});
        Frame& f = frames.back();
        if (f.next >= f.mv.size()) { frames.pop_back(); if (!path.empty()) path.pop_back(); continue; }
        Mv m = f.mv[f.next++];
        State child = f.s;
        apply(X, child, m);
        uint32_t h = (uint32_t)game_hash(X, child);
        if (!seen.insert(h).second) continue;
        moves += 1;
        path.push_back(m);
        if (eval_pile(X, X.win.data(), child)) return finish(1, (int)path.size(), moves, path);
        if (moves >= max_moves) return finish(2, moves, moves, {});
        frames.push_back({std::move(child), {}, 0});
        legal_moves(X, frames.back().s, frames.back().mv);
    }
    return finish(0, moves, moves, {});
}

// ---------------------------------------------------------------- fast search: incremental hashing, build only new positions
struct HState { State s; std::vector<uint64_t> pp; uint64_t H; };  // pp: per-pile card polynomial; H: position hash

static inline uint64_t fold(const Ctx& X, uint64_t n, Card c) { return (n * X.B1 + (uint64_t)((c & 63) * 2 + (is_down(c) ? 1 : 0))) % X.MOD; }
static inline uint64_t stack_h(const Ctx& X, uint64_t poly_, int p) { return (poly_ * X.type_h[X.pile_type[p]] + X.PMAX) % X.MOD; }

static uint64_t base_hash(const Ctx& X, const State& S) {  // everything except the stack piles' contributions
    uint64_t num = X.game_h;
    if (X.draw_kind == 1) num = (num * X.PMAX + (poly(X, S.dcards) * X.deal_h + X.PMAX) % X.MOD) % X.MOD;
    else if (X.draw_kind == 2) {
        uint64_t dh = X.rot_h;
        dh = (dh * X.PMAX + poly(X, S.back)) % X.MOD;
        dh = (dh * X.PMAX + poly(X, S.dcards)) % X.MOD;
        dh = (dh * X.PMAX + poly(X, S.drawn)) % X.MOD;
        num = (num * X.PMAX + dh) % X.MOD;
    }
    return (num * X.PMAX) % X.MOD;
}

static void rehash(const Ctx& X, HState& h) {
    h.pp.resize(h.s.piles.size());
    uint64_t num = base_hash(X, h.s);
    for (size_t p = 0; p < h.s.piles.size(); ++p) {
        h.pp[p] = poly(X, h.s.piles[p]);
        num = (num + stack_h(X, h.pp[p], (int)p)) % X.MOD;
    }
    h.H = num;
}

static int run_search_fast(const Ctx& X, const State& S0, int32_t* out, int nout, int max_moves, double time_limit) {
    auto t0 = std::chrono::steady_clock::now();
    auto elapsed = [&] { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
    auto finish = [&](int status, int moves, int searched, const std::vector<Mv>& path) {
        out[0] = status; out[1] = moves; out[2] = searched;
        int room = (nout - 4) / 4, n = (int)path.size() < room ? (int)path.size() : room;
        out[3] = n;
        for (int j = 0; j < n; ++j) { out[4 + 4 * j] = path[j].kind; out[5 + 4 * j] = path[j].src; out[6 + 4 * j] = path[j].dst; out[7 + 4 * j] = path[j].i; }
        return status;
    };
    std::unordered_set<uint32_t> seen;
    seen.reserve((size_t)max_moves * 4 + 16);
    struct Frame { HState h; std::vector<Mv> mv; size_t next; };
    std::vector<Frame> frames;
    frames.reserve(256);
    frames.push_back({{S0, {}, 0}, {}, 0});
    rehash(X, frames.back().h);
    seen.insert((uint32_t)frames.back().h.H);
    std::vector<Mv> path;
    if (eval_pile(X, X.win.data(), S0)) return finish(1, 0, 0, path);
    legal_moves(X, frames.back().h.s, frames.back().mv);
    int moves = 0;
    const uint64_t MOD = X.MOD;
    while (!frames.empty()) {
        if (elapsed() > time_limit) return finish(3, moves, moves, {});
        size_t fi = frames.size() - 1;
        if (frames[fi].next >= frames[fi].mv.size()) { frames.pop_back(); if (!path.empty()) path.pop_back(); continue; }
        Mv m = frames[fi].mv[frames[fi].next++];
        const HState& par = frames[fi].h;
        HState child;
        if (m.kind != 0 && m.src >= 0) {
            const std::vector<Card>& sc = par.s.piles[m.src];
            int n = (int)sc.size(), i = m.kind == 1 ? n - 1 : m.i;
            uint64_t ns = 0;
            for (int j = 0; j < i; ++j) ns = fold(X, ns, (j == i - 1) ? face_up(sc[j]) : sc[j]);
            uint64_t nd = par.pp[m.dst];
            for (int j = i; j < n; ++j) nd = fold(X, nd, sc[j]);
            uint64_t Hc = (par.H + 2 * MOD - stack_h(X, par.pp[m.src], m.src) - stack_h(X, par.pp[m.dst], m.dst)
                           + stack_h(X, ns, m.src) + stack_h(X, nd, m.dst)) % MOD;
            if (seen.count((uint32_t)Hc)) continue;  // never built
            child.s = par.s;
            apply(X, child.s, m);
            child.pp = par.pp; child.pp[m.src] = ns; child.pp[m.dst] = nd; child.H = Hc;
        } else {
            child.s = par.s;
            apply(X, child.s, m);
            rehash(X, child);
            if (seen.count((uint32_t)child.H)) continue;
        }
        seen.insert((uint32_t)child.H);
        moves += 1;
        path.push_back(m);
        if (eval_pile(X, X.win.data(), child.s)) return finish(1, (int)path.size(), moves, path);
        if (moves >= max_moves) return finish(2, moves, moves, {});
        frames.push_back({std::move(child), {}, 0});
        legal_moves(X, frames.back().h.s, frames.back().mv);
    }
    return finish(0, moves, moves, {});
}
