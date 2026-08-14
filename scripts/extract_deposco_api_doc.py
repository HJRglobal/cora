"""S0 -- extract the Deposco V1 API doc to a greppable repo-local reference.

The Drive/text lanes truncate this PDF around p. 40, which is why the endpoint
contracts kept getting re-derived from memory. This writes the full 457 pages to
`data/reference/deposco-api-v1.txt` with `===== PAGE n =====` markers, so the page
numbers cited in the design doc stay usable as an index.

The output is GITIGNORED: it is a derived artifact (~577 KB) from a PDF that lives
in Drive, and this script is what makes it reproducible. Regenerate any time with:

    .venv\\Scripts\\python.exe scripts\\extract_deposco_api_doc.py

Uses pdfplumber, already a project dependency -- deliberately not adding pypdf for
a one-shot extraction.

Key sections (page numbers, for grepping the output):
    Overview / gateway ......... 23-26   Enterprise Inventory ....... 88-93
    Inventory API .............. 94-100  Order status ............... 184-196
    PO field reference ......... 217-230 (receipt lines p. 230: lot + expiry)
    Receipt advice ............. 252     Receipt line ............... 260-262
    Shipment ................... 363-396
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PDF = Path(
    r"G:\My Drive\HJR-Founder-OS\02-F3-Energy\projects"
    r"\2026-08_deposco-api-order-automation\2026-08-05_f3e_deposco-api-doc-v1.pdf"
)
DEFAULT_OUT = _REPO_ROOT / "data" / "reference" / "deposco-api-v1.txt"


def extract(src: Path, dst: Path) -> int:
    import pdfplumber  # imported here so --help works without the dependency

    dst.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    with pdfplumber.open(src) as pdf:
        total = len(pdf.pages)
        print(f"pages: {total}", flush=True)
        for index, page in enumerate(pdf.pages, start=1):
            chunks.append(f"\n===== PAGE {index} =====\n{page.extract_text() or ''}")
            if index % 50 == 0:
                print(f"  ...{index}/{total}", flush=True)
    dst.write_text("".join(chunks), encoding="utf-8")
    print(f"wrote {dst} ({dst.stat().st_size:,} bytes, {total} pages)")
    return total


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    src = Path(args[0]) if args else DEFAULT_PDF
    dst = Path(args[1]) if len(args) > 1 else DEFAULT_OUT
    if not src.exists():
        print(f"ERROR: source PDF not found: {src}", file=sys.stderr)
        return 2
    extract(src, dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
