"""Generate a synthetic multi-page PDF for smoke tests: `python scripts/make_sample_pdf.py out.pdf 300`.

Page N contains a unique fact ("The checkpoint code for section N is CK-<N*7>.") so tests can
check that a citation opens the right page.
"""

from __future__ import annotations

import sys
import textwrap

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

TOPICS = [
    "monetary policy transmission",
    "credit growth in the banking sector",
    "payment systems and UPI adoption",
    "inflation expectations of households",
    "financial inclusion programmes",
    "foreign exchange reserves",
    "non-performing assets and recovery",
    "digital lending guidelines",
    "climate risk and sustainable finance",
    "currency management and coin distribution",
]


def page_text(n: int) -> str:
    topic = TOPICS[n % len(TOPICS)]
    return (
        f"Section {n}: {topic.title()}. The checkpoint code for section {n} is CK-{n * 7}. "
        f"This section reviews {topic} during the year under review, summarising the main developments, "
        f"the policy measures taken and the outlook for the coming year. Indicator {n}.1 rose by {n % 9 + 1}.{n % 7} "
        f"percent while indicator {n}.2 eased to {(n * 3) % 50 + 10} basis points. The committee noted that "
        f"{topic} remained broadly stable, with risks tilted to the {'upside' if n % 2 else 'downside'}."
    )


def main(path: str, pages: int) -> None:
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle("DocPilot sample annual report")
    for n in range(1, pages + 1):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(56, 790, f"Sample Annual Report — page {n}")
        c.setFont("Helvetica", 11)
        y = 760
        for line in textwrap.wrap(page_text(n), 92):
            c.drawString(56, y, line)
            y -= 16
        c.showPage()
    c.save()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf", int(sys.argv[2]) if len(sys.argv) > 2 else 300)
