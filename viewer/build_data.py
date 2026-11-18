"""Extract the rule-reasoning logs into one compact data.json for the viewer.

Usage:  python build_data.py            (run from anywhere; paths are relative to this file)

Each run folder holds g<gen>_<idx>_<Game>.log files. Every log is a 3-message chat:
  system    - the shared SGDL design prompt (identical across files, stored once per run)
  user      - the current SGDL (+ compile-error comments) and its evaluation table
              (+ the distilled skill.md in skill runs)
  assistant - the model's reasoning, an optional ```insight``` block, and the revised SGDL
g<gen>_skill.log files hold the skill.md rewrite for that generation.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RUNS = [
    ("noskill", "No skill", "1782918622-llm-noskill-logs/1782918622"),
    ("skill", "Skill", "1782918989-llm-skill-logs/1782918989"),
    ("existing", "Skill, from existing games", "llm-skill-from-existing/1-test-fixed"),
]

FENCE = re.compile(r"```(\w*)[^\n]*\n(.*?)```", re.S)
ERR = re.compile(r"^#\s*ERROR when compiling this gdl:\s*\n((?:#.*\n?)*)", re.M)
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(Yes|No)\s*\|\s*(\d+)\s*\|\s*(Yes|No)\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|", re.M)
SKILL_INTRO = "Here are lessons learned from previous rounds"


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.load(f)


def deck_check(sgdl):
    """Return (deck_size, initial_sum) or None if it can't be parsed."""
    m = re.search(r"^\s*DECK\s+(\d+)\s+(\{[^}]*\}|\w+)", sgdl, re.M)
    if not m:
        return None
    suits = [s for s in re.split(r"[{},\s]+", m.group(2)) if s]
    deck = int(m.group(1)) * 13 * len(suits)
    sec = re.search(r"^\$initial\s*\n(.*?)(?=^\$)", sgdl, re.M | re.S)
    if not sec:
        return None
    total = 0
    for line in sec.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            total += int(parts[1])
    return deck, total


def flags_for(sgdl):
    out = []
    if "\t" in sgdl:
        out.append("tabs")
    dc = deck_check(sgdl)
    if dc and dc[1] < dc[0]:
        out.append(f"deck short {dc[1]}/{dc[0]}")
    elif dc and dc[1] > dc[0]:
        out.append(f"deck over {dc[1]}/{dc[0]}")
    # Overlapping (kind, src, dst) pairs across MOVE / MOVE_STACK rules
    seen, dups = set(), []
    pile_re = r"(\{[^}]*\}|\S+)"
    draw_bad = False
    for m in re.finditer(r"^(MOVE|MOVE_STACK)[ \t]+" + pile_re + r"[ \t]+" + pile_re, sgdl, re.M):
        kind = m.group(1)
        src = [s for s in re.split(r"[{},\s]+", m.group(2)) if s]
        dst = [s for s in re.split(r"[{},\s]+", m.group(3)) if s]
        if "DRAW" in dst or (kind == "MOVE_STACK" and "DRAW" in src):
            draw_bad = True
        for s in src:
            for t in dst:
                k = (kind, s, t)
                if k in seen and f"{s}→{t}" not in dups:
                    dups.append(f"{s}→{t}")
                seen.add(k)
    if dups:
        out.append("dup rule " + ", ".join(dups[:3]))
    init = re.search(r"^\$initial\s*\n(.*?)(?=^\$)", sgdl, re.M | re.S)
    has_draw_pile = bool(init and re.search(r"^\s*DRAW\s+\d+", init.group(1), re.M))
    body = sgdl[init.end():] if init else ""
    body = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
    refs_draw = bool(re.search(r"\bDRAW\b", body))
    if refs_draw and not has_draw_pile:
        out.append("DRAW undefined")
    if draw_bad:
        out.append("DRAW misuse")
    deal = bool(init and re.search(r"^\s*DRAW\s+\d+\s+DEAL\b", init.group(1), re.M))
    if deal and re.search(r"^MOVE[ \t]+(\{[^}]*\bDRAW\b[^}]*\}|DRAW)[ \t]", sgdl, re.M):
        out.append("DEAL draw as source")
    return out


def norm_error(msg):
    """Collapse pile names / numbers so similar errors group together."""
    if not msg:
        return None
    e = msg.strip()
    e = re.sub(r"Pilename (\w+) is not among valid piles for this part: \[.*\]",
               r"Pilename \1 is not among valid piles for this part", e)
    e = re.sub(r"Cannot recognize move type of \[.*\]", "Cannot recognize move type of [...]", e)
    e = re.sub(r"Suit not recognized: .*", "Suit not recognized: <x>", e)
    return e


def parse_user(text):
    m = FENCE.search(text)
    sgdl = m.group(2) if m else ""
    rest = text[m.end():] if m else text
    err = None
    em = ERR.search(sgdl)
    if em:
        err = " ".join(l.lstrip("#").strip() for l in em.group(1).splitlines() if l.strip()).strip()
        sgdl = sgdl[:em.start()].rstrip() + "\n"
    rows = [[int(r[0]), r[1] == "Yes", int(r[2]), r[3] == "Yes", float(r[4]), float(r[5])]
            for r in ROW.findall(rest)]
    summary = None
    sm = re.search(r"^(\d+/\d+ runs won.*)$", rest, re.M)
    if sm:
        summary = sm.group(1).strip()
    noeval = "No evaluation data is available" in rest
    has_skill = SKILL_INTRO in rest
    return sgdl, err, rows, summary, noeval, has_skill


def parse_assistant(text):
    insight, out = None, None
    blocks = list(FENCE.finditer(text))
    first = None
    for b in blocks:
        tag = b.group(1).lower()
        if tag == "insight":
            insight = b.group(2).strip()
            first = first if first is not None else b.start()
        elif tag == "sgdl":
            out = b.group(2)
            first = first if first is not None else b.start()
    if out is None and blocks:  # untagged fallback: last block
        out = blocks[-1].group(2)
        first = blocks[-1].start() if first is None else first
    reasoning = text[:first].strip() if first is not None else text.strip()
    return reasoning, insight, out


def outcome(err, noeval, rows):
    if err:
        return "error"
    if noeval or not rows:
        return "noeval"
    wins = sum(1 for r in rows if r[1])
    if wins == len(rows):
        moves = {r[2] for r in rows}
        return "flatwin" if len(moves) == 1 else "allwin"
    if wins > 0:
        return "somewin"
    ex = sum(1 for r in rows if r[3])
    if ex == len(rows):
        return "dead"
    if ex == 0:
        return "cap"
    return "mixedloss"


def build_run(key, label, rel):
    d = os.path.join(ROOT, rel)
    system = None
    games = {}
    skills = {}
    for f in sorted(glob.glob(os.path.join(d, "g*_*.log"))):
        b = os.path.basename(f)
        sm = re.match(r"g(\d+)_skill\.log$", b)
        if sm:
            j = load(f)
            _, _, so = None, None, None
            sk = None
            for bl in FENCE.finditer(j[-1]["content"]):
                if bl.group(1).lower() == "skill":
                    sk = bl.group(2).strip()
            n_ins = re.search(r"produced (\d+) insight", j[1]["content"])
            skills[int(sm.group(1))] = {
                "skill": sk,
                "reasoning": FENCE.split(j[-1]["content"])[0].strip() if sk else j[-1]["content"],
                "nInsights": int(n_ins.group(1)) if n_ins else None,
                "file": b,
            }
            continue
        m = re.match(r"g(\d+)_(\d+)_(.+)\.log$", b)
        if not m:
            continue
        gen, idx, name = int(m.group(1)), int(m.group(2)), m.group(3)
        j = load(f)
        roles = {x["role"]: x["content"] for x in j}
        if system is None:
            system = roles.get("system")
        sgdl, err, rows, summary, noeval, has_skill = parse_user(roles.get("user", ""))
        reasoning, insight, out = parse_assistant(roles.get("assistant", ""))
        squash = lambda s: "\n".join(l.rstrip() for l in (s or "").strip().splitlines())
        out_flags = flags_for(out or "")
        if out and squash(out) == squash(sgdl):
            out_flags.append("unchanged")
        games.setdefault(name, {})[gen] = {
            "file": b, "idx": idx,
            "in": sgdl, "err": err, "errKey": norm_error(err), "rows": rows, "summary": summary,
            "noeval": noeval, "skillShown": has_skill,
            "outcome": outcome(err, noeval, rows),
            "inFlags": flags_for(sgdl),
            "reasoning": reasoning, "insight": insight, "out": out or "",
            "outFlags": out_flags,
        }
    gens = sorted({g for v in games.values() for g in v})
    return {
        "key": key, "label": label, "path": rel, "system": system, "gens": gens,
        "games": games, "skills": {str(k): v for k, v in skills.items()},
    }


def main():
    data = {"runs": []}
    for key, label, rel in RUNS:
        if not os.path.isdir(os.path.join(ROOT, rel)):
            print("skip (missing):", rel)
            continue
        r = build_run(key, label, rel)
        n = sum(len(v) for v in r["games"].values())
        print(f"{label}: {len(r['games'])} games, gens {r['gens'][0]}-{r['gens'][-1]}, {n} revisions, {len(r['skills'])} skill files")
        data["runs"].append(r)
    out = os.path.join(HERE, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote", out, f"{os.path.getsize(out)/1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
