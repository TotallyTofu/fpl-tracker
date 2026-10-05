"""Probe the real DB for top players + validate more alias candidates."""
import sqlite3
import sys
from unicodedata import normalize
import unicodedata

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = sqlite3.connect("data/fpl.db")
rows = conn.execute(
    "SELECT id, web_name, first_name, second_name, ep_next FROM players WHERE removed = 0 "
    "ORDER BY ep_next DESC LIMIT 60"
).fetchall()

def flat(s):
    s = normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join("".join(ch if ("a" <= ch <= "z" or ch == " ") else " " for ch in s).split())

print("TOP 60 by ep_next:")
for r in rows:
    print(f"  {r[0]:4} {r[1]:28} first={r[2]!r} ep={r[4]}")

# alias collision checker
full_names = {}
for r in conn.execute("SELECT id, web_name, first_name, second_name FROM players WHERE removed = 0"):
    for nm in (r[1], (r[2] or "") + " " + (r[3] or "")):
        f = flat(nm)
        if f:
            full_names.setdefault(f, set()).add(r[0])

CAND = {
    "cold palmer": "Palmer",
    "ollie watkins": "Watkins",
    "mo caicedo": "Caicedo",
    "big gabriel": "Gabriel",
    "gabi jesus": "G.Jesus",
    "gabby jesus": "G.Jesus",
    "martin odegaard": "Odegaard",
    "micky van de ven": "Van de Ven",
    "victor gyokeres": "Gyokeres",
    "kai havertz": "Havertz",
    "eberechi eze": "Eze",
    "ezri konsa": "Konsa",
    "antonee robinson": "Robinson",
    "joao gomes": "Joao Gomes",
    "joao pedro": "Joao Pedro",
    "matheus cunha": "Cunha",
    "bryan mbeumo": "Mbeumo",
    "yoane wissa": "Wissa",
    "jean philippe mateta": "Mateta",
    "dango ouattara": "Ouattara",
    "ilyas samar": "Samar",
}
print("\nCANDIDATE CHECK:")
for alias, target in CAND.items():
    fa, ft = flat(alias), flat(target)
    tid = by_web = None
    for r in conn.execute("SELECT id, web_name FROM players WHERE removed = 0"):
        if flat(r[1]) == ft:
            tid = r[0]
            break
    collides = sorted(full_names.get(fa, set()) - ({tid} if tid else set()))
    print(f"  {alias!r:24} -> {target!r:16} id={tid} collision={collides or 'none'}")

# --- A8 dev check: dead / ambiguous targets in the shipped pattern dicts -------
# name_index logs these at index-build time (log.debug / log.warning); this
# section is the on-demand version against the live pool.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from app.signals import names as _N  # noqa: E402

pool_by_web: dict[str, set] = {}
for r in conn.execute("SELECT id, web_name FROM players WHERE removed = 0"):
    pool_by_web.setdefault(_N._flat(r[1]), set()).add(r[0])


def _check(patterns: dict, label: str):
    print(f"\n{label}:")
    bad = 0
    for p, t in patterns.items():
        tids = pool_by_web.get(_N._flat(t), set())
        if not tids:
            print(f"  DEAD      {p!r} -> {t!r} (target not in pool)")
            bad += 1
        elif len(tids) > 1:
            print(f"  AMBIGUOUS {p!r} -> {t!r} (ids {sorted(tids)})")
            bad += 1
    if not bad:
        print("  OK — every target resolves to exactly one player")
    return bad


live_bad = _check({**_N.MISSPELLINGS, **_N.ALIASES}, "LIVE PATTERNS (folded into the scan index)")
_check(_N.DORMANT_PATTERNS, "DORMANT PATTERNS (expected dead)")