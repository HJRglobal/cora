"""Benchmark KB vector search: float fallback vs binary fast path.

Measures per-entity p50/p95 latency for both search paths and the recall@10
overlap of the fast path against the exact float ranking (the quality guard).
ASCII-only output (safe on a cp1252 host console).

No OpenAI / numpy dependency: query vectors are sampled from real stored chunk
embeddings (knowledge_vec_f32), so this runs free + offline and is reproducible.

Run BOTH paths in one shot (post-migration, both tables populated):
    .venv\\Scripts\\python.exe scripts\\bench_kb_search.py
    .venv\\Scripts\\python.exe scripts\\bench_kb_search.py --queries 15 --k 8

Quality guard: fails (exit 1) if any entity's mean recall@10 < the threshold
(default 0.80 == 8/10), matching the session acceptance criterion.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402
from cora.knowledge_base.store import KnowledgeBase  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def _f32_bytes_to_list(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b) // 4}f", b))


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark KB float vs binary search paths.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--queries", type=int, default=12, help="Sample queries per entity.")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--iters", type=int, default=5, help="Timing repeats per query.")
    ap.add_argument("--entities", default="", help="Comma list; default = top-by-count.")
    ap.add_argument("--recall-threshold", type=float, default=0.80)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    kb = KnowledgeBase(args.db)
    conn = kb._conn

    bin_ready = kb._is_bin_index_ready()
    f32_n = conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    bin_n = conn.execute("SELECT COUNT(*) FROM knowledge_vec_bin").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    print(f"DB: {args.db}")
    print(f"chunks={total:,}  bin={bin_n:,}  f32={f32_n:,}  bin_ready={bin_ready}")
    have_fast = bin_n > 0 and f32_n > 0
    if not have_fast:
        print("NOTE: binary index not populated -> only the float path is benchmarked "
              "(run the migration first to benchmark the fast path).")

    if args.entities.strip():
        entities = [e.strip() for e in args.entities.split(",") if e.strip()]
    else:
        entities = [
            r[0] for r in conn.execute(
                "SELECT entity, COUNT(*) c FROM knowledge_chunks "
                "GROUP BY entity ORDER BY c DESC LIMIT 6"
            ).fetchall()
        ]

    print(f"\n{'entity':<8} {'n':>4} {'float p50':>10} {'float p95':>10} "
          f"{'fast p50':>9} {'fast p95':>9} {'recall@10':>10}")
    print("-" * 70)

    worst_recall = 1.0
    any_fast = False
    for entity in entities:
        # Sample query vectors from this entity's stored float vectors.
        rows = conn.execute(
            "SELECT f.embedding FROM knowledge_vec_f32 f "
            "JOIN knowledge_chunks k ON k.chunk_id = f.chunk_id "
            "WHERE k.entity = ? LIMIT ?",
            (entity, args.queries),
        ).fetchall()
        if not rows:
            # f32 not populated for this entity (pre-migration) -> sample via knowledge_vec
            rows = conn.execute(
                "SELECT v.embedding FROM knowledge_vec v "
                "JOIN knowledge_chunks k ON k.chunk_id = v.chunk_id "
                "WHERE k.entity = ? LIMIT ?",
                (entity, args.queries),
            ).fetchall()
        qvecs = [_f32_bytes_to_list(r[0]) for r in rows]
        if not qvecs:
            continue

        # entity filter as the live path builds it (entity + FNDR for non-FNDR)
        ent_filter = (entity,) if entity == "FNDR" else (entity, "FNDR")

        float_ms, fast_ms, recalls = [], [], []
        for qv in qvecs:
            # float path
            t = time.perf_counter()
            fres = kb._search_float(qv, ent_filter, args.k, None, "", [])
            for _ in range(args.iters - 1):
                kb._search_float(qv, ent_filter, args.k, None, "", [])
            float_ms.append((time.perf_counter() - t) / args.iters * 1000)

            if have_fast:
                t = time.perf_counter()
                bres = kb._search_binary(qv, ent_filter, args.k, None, "", [])
                for _ in range(args.iters - 1):
                    kb._search_binary(qv, ent_filter, args.k, None, "", [])
                fast_ms.append((time.perf_counter() - t) / args.iters * 1000)

                fset = {r.chunk_id for r in fres}
                bset = {r.chunk_id for r in bres}
                if fset:
                    recalls.append(len(fset & bset) / len(fset))

        fp50, fp95 = _pct(float_ms, 50), _pct(float_ms, 95)
        if have_fast and recalls:
            any_fast = True
            xp50, xp95 = _pct(fast_ms, 50), _pct(fast_ms, 95)
            mrecall = sum(recalls) / len(recalls)
            worst_recall = min(worst_recall, mrecall)
            print(f"{entity:<8} {len(qvecs):>4} {fp50:>9.1f}m {fp95:>9.1f}m "
                  f"{xp50:>8.1f}m {xp95:>8.1f}m {mrecall:>10.2%}")
        else:
            print(f"{entity:<8} {len(qvecs):>4} {fp50:>9.1f}m {fp95:>9.1f}m "
                  f"{'-':>9} {'-':>9} {'-':>10}")

    kb.close()

    if any_fast:
        print("-" * 70)
        print(f"worst per-entity mean recall@10: {worst_recall:.2%} "
              f"(threshold {args.recall_threshold:.0%})")
        if worst_recall < args.recall_threshold:
            print("FAIL: recall below threshold -> raise _COARSE_MIN/_COARSE_MULT in "
                  "store.py and re-run.", file=sys.stderr)
            return 1
        print("OK: recall guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
