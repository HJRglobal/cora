"""Lexicon eval readout (Lexicon Flywheel S7).

Runs the golden set (tests/golden/lexicon_golden.yaml) through lexicon.resolve
and prints/persists the readout the rollout gates on:

  - resolution rate on seeded exact cases (target >= 90%)
  - ask-on-ambiguity rate (target 100%)
  - false resolutions: exact/ambiguous where the corpus demands a miss, or a
    silent pick where it demands an ask (HARD GATE: must be 0)

Usage:
    .venv\\Scripts\\python.exe scripts\\eval_lexicon.py --baseline
        -> writes data/evals/lexicon-eval-baseline.json (REQUIRED before the
           CORA_LEXICON=full flip; the design's write-the-baseline rule)
    .venv\\Scripts\\python.exe scripts\\eval_lexicon.py
        -> post readout to data/evals/lexicon-eval-YYYY-MM-DD.json

Read-only against the real stores; capture_cases are pytest-only (they need
tmp store sandboxes). Exit 1 when a hard gate fails, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import yaml  # noqa: E402

from cora import lexicon  # noqa: E402

_GOLDEN = _REPO_ROOT / "tests" / "golden" / "lexicon_golden.yaml"


def run_eval() -> dict:
    cases = (yaml.safe_load(_GOLDEN.read_text(encoding="utf-8"))
             .get("resolve_cases") or [])
    results = []
    seeded_total = seeded_hit = 0
    ask_total = ask_correct = 0
    false_resolutions = []
    for case in cases:
        r = lexicon.resolve(case["utterance"], case["entity"])
        expect = case["expect"]
        ok = False
        if expect == "exact":
            seeded_total += 1
            ok = r.status == "exact" and r.canonical == case["canonical"]
            seeded_hit += 1 if ok else 0
            if r.status in ("exact", "ambiguous") and not ok and r.canonical:
                false_resolutions.append(case["id"])
        elif expect == "ask":
            ask_total += 1
            ok = (r.status == "ambiguous" and not r.canonical
                  and {c.canonical for c in r.candidates} == set(case["candidates"]))
            ask_correct += 1 if ok else 0
            if r.status == "exact":
                false_resolutions.append(case["id"])  # silent pick on an ambiguity
        elif expect == "suggestion":
            ok = r.status == "suggestion" and not r.canonical
            if r.status in ("exact", "ambiguous"):
                false_resolutions.append(case["id"])
        elif expect == "miss":
            ok = r.status in ("miss", "suggestion") and not r.canonical
            if not ok:
                false_resolutions.append(case["id"])
        results.append({"id": case["id"], "status": r.status, "ok": ok})

    resolution_rate = round(seeded_hit / seeded_total, 4) if seeded_total else None
    ask_rate = round(ask_correct / ask_total, 4) if ask_total else None
    return {
        "date": date.today().isoformat(),
        "lexicon_level": lexicon.lexicon_level(),
        "cases": len(cases),
        "seeded_resolution_rate": resolution_rate,
        "ask_on_ambiguity_rate": ask_rate,
        "false_resolutions": false_resolutions,
        "gates": {
            "zero_false_resolutions": not false_resolutions,
            "ask_on_ambiguity_100pct": ask_rate == 1.0,
            "seeded_resolution_90pct": (resolution_rate or 0) >= 0.90,
        },
        "failures": [r for r in results if not r["ok"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="Write data/evals/lexicon-eval-baseline.json")
    args = parser.parse_args()
    readout = run_eval()
    out_dir = _REPO_ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = ("lexicon-eval-baseline.json" if args.baseline
            else f"lexicon-eval-{readout['date']}.json")
    (out_dir / name).write_text(json.dumps(readout, indent=2, ensure_ascii=False),
                                encoding="utf-8")
    print(json.dumps(readout, indent=2, ensure_ascii=False))
    print(f"\nartifact: {out_dir / name}")
    return 0 if all(readout["gates"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
