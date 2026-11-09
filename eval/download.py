"""Download the public PDFs used by the evaluation: `python -m eval.download`.

The files aren't committed to the repo (third-party content); this fetches them on demand.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx

PDF_DIR = Path(__file__).parent / "pdfs"
SOURCES = {
    # Vaswani et al., "Attention Is All You Need" (arXiv:1706.03762v7).
    "attention.pdf": "https://arxiv.org/pdf/1706.03762v7",
    # Lewis et al., "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks" (arXiv:2005.11401v4).
    "rag.pdf": "https://arxiv.org/pdf/2005.11401v4",
}


def main() -> int:
    PDF_DIR.mkdir(exist_ok=True)
    for name, url in SOURCES.items():
        path = PDF_DIR / name
        if path.exists() and path.stat().st_size > 10_000:
            print(f"ok   {name} (cached)")
            continue
        r = httpx.get(url, follow_redirects=True, timeout=120, headers={"User-Agent": "DocPilot-eval/1.0"})
        r.raise_for_status()
        if not r.content.startswith(b"%PDF"):
            print(f"FAIL {name}: not a PDF", file=sys.stderr)
            return 1
        path.write_bytes(r.content)
        print(
            f"got  {name} {len(r.content) / 1e6:.1f} MB sha256={hashlib.sha256(r.content).hexdigest()[:12]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
