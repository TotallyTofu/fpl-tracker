"""Self-consistency probe for the name index (FIX.MD §0.2 / §10.2).

Re-run against the live DB after any change to app/signals/names.py:

    ..\\..\\.venv\\Scripts\\python scripts\\check_name_resolution.py

A player's own web_name must resolve to that player — or to nobody when the
name is genuinely shared (class A: 2+ players with the same web_name). It must
NEVER resolve to a different player. Prints three counts:
total / wrong / missing; the goal is N / 0 / 0 (reached after A2 + A9.1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.signals import names as N  # noqa: E402


def main() -> int:
    idx = N.get_name_index()
    rows = N._load_player_rows()
    shared = {k for k, v in idx["by_web"].items() if len(v) > 1}
    wrong, missing = [], []
    for r in rows:
        hits = {h["player_id"] for h in N.resolve_candidates(r["web_name"], idx)}
        if hits == {r["id"]}:
            continue
        if hits and r["id"] not in hits:
            wrong.append((r["web_name"], sorted(hits)))
        elif not hits and N._flat(r["web_name"]) not in shared:
            missing.append(r["web_name"])
    print(f"{len(rows)} players: {len(wrong)} wrong, {len(missing)} missing")
    for web, ids in wrong[:20]:
        print(f"  WRONG   {web!r} -> {ids}")
    for web in missing[:20]:
        print(f"  MISSING {web!r}")
    return 0 if not wrong and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())