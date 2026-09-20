"""Rail-2 differential over the ruled probe sets and (optionally) the LIVE News/Learn
corpus (Code #13 slice 6, cq-85b35413b020).

    .venv\\Scripts\\python.exe scripts\\run_f3e_blog_rail2_differential.py            # probes only: the SHIP gate
    .venv\\Scripts\\python.exe scripts\\run_f3e_blog_rail2_differential.py --live     # + public f3energy.com/blogs read
    .venv\\Scripts\\python.exe scripts\\run_f3e_blog_rail2_differential.py --live --out <path>

READ-ONLY. --live performs anonymous public GETs of https://f3energy.com/blogs/news
and /blogs/learn (index pages) and each article page through
shopify_client.fetch_public_page (3 MB ceiling, never raises), converts the HTML to
prose with preflight.html_to_text, splits sentences and runs BOTH rail-2 tests on
every sentence. It prints FP count before/after and holes caught before/after; it
writes nothing unless --out is given (an explicit path; never a default under logs/).
No Shopify Admin token, no KB, no LLM.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora.f3e_blog import preflight as pf  # noqa: E402
from cora.f3e_blog import rail2_harness as rh  # noqa: E402

BLOG_INDEXES = ("https://f3energy.com/blogs/news", "https://f3energy.com/blogs/learn")
_ARTICLE_LINK_RE = re.compile(r'href="(?:https://f3energy\.com)?(/blogs/(?:news|learn)/[a-z0-9][a-z0-9\-]{1,200})"', re.IGNORECASE)


def article_urls(index_html: str, index_url: str) -> list[str]:
    """Distinct article URLs linked from a blog index page (order preserved)."""
    seen: list[str] = []
    for m in _ARTICLE_LINK_RE.finditer(index_html or ""):
        path = m.group(1)
        if path.rstrip("/") in ("/blogs/news", "/blogs/learn"):
            continue
        url = "https://f3energy.com" + path
        if url not in seen:
            seen.append(url)
    return seen


def corpus_sentences(fetch, indexes=BLOG_INDEXES, *, max_articles: int = 200) -> tuple[list[str], dict]:
    """(sentences, meta) over the live blogs. `fetch(url) -> (status, text)`."""
    urls: list[str] = []
    for idx in indexes:
        status, html = fetch(idx)
        if status != 200 or not html:
            continue
        for u in article_urls(html, idx):
            if u not in urls:
                urls.append(u)
    urls = urls[:max_articles]
    sents: list[str] = []
    fetched = 0
    per_article: dict[str, int] = {}
    for u in urls:
        status, html = fetch(u)
        if status != 200 or not html:
            continue
        fetched += 1
        prose = pf.html_to_text(html)
        ss = pf.sentences(prose)
        per_article[u] = len(ss)
        sents.extend(ss)
    return sents, {"index_pages": list(indexes), "articles_linked": len(urls),
                   "articles_fetched": fetched, "per_article": per_article}


def main(argv: list[str] | None = None, *, fetch=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="also read the public News/Learn corpus")
    parser.add_argument("--out", help="write the report to this explicit path (default: stdout only)")
    parser.add_argument("--max-articles", type=int, default=200)
    args = parser.parse_args(argv)

    lines: list[str] = ["Rail-2 differential (Code #13 slice 6, cq-85b35413b020)", ""]
    verdict = rh.evaluate()
    lines.append("== SHIP GATE over the ruled probe sets ==")
    lines += verdict.summary_lines()
    lines.append("")
    lines.append("== probe table (class | probe | legacy | attribution) ==")
    for cls, probes in rh.CLAIMS_HOLE_PROBES.items():
        for s in probes:
            lg = "TRIP" if not rh.legacy_preflight(s).passed else "pass"
            at = "TRIP" if not rh.new_preflight(s).passed else "pass"
            lines.append(f"{cls:<30} {lg:<5} {at:<5} {s}")
    lines.append("")
    lines.append("== false-positive set (must pass attribution) ==")
    for s in rh.FALSE_POSITIVE_SET:
        lg = "TRIP" if not rh.legacy_preflight(s).passed else "pass"
        at = "TRIP" if not rh.new_preflight(s).passed else "pass"
        lines.append(f"{'fp_set':<30} {lg:<5} {at:<5} {s}")

    if args.live:
        if fetch is None:
            from cora.connectors import shopify_client  # noqa: PLC0415
            fetch = shopify_client.fetch_public_page
        sents, meta = corpus_sentences(fetch, max_articles=args.max_articles)
        diff = rh.differential(sents)
        lines.append("")
        lines.append("== LIVE corpus (public f3energy.com/blogs/news + /blogs/learn) ==")
        lines.append(f"articles linked {meta['articles_linked']} | fetched {meta['articles_fetched']}")
        lines += diff.summary_lines()
        for s in diff.only_legacy:
            lines.append("  released (legacy-only trip): " + s[:220])
        for s in diff.attribution_trips:
            lines.append("  still trips under attribution: " + s[:220])
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0 if verdict.ship else 3


if __name__ == "__main__":
    sys.exit(main())
