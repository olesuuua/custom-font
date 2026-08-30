#!/usr/bin/env python3
"""Generate the read-only data bundle used when qa/italic.html is opened directly."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "output" / "italic-v10-redraw" / "italic-v10-glyph-report.csv"
DECISIONS = ROOT / "qa" / "italic-v10-manual-decisions.json"
OUTPUT = ROOT / "qa" / "assets" / "italic-report.js"


def main() -> None:
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        filename = f"{int(row['index']):03d}-{row['glyph']}.svg"
        row["static_source_svg"] = (
            f"../output/italic-v10-redraw/v2-source-glyph-svg/{filename}"
            if row.get("source_svg") else ""
        )
        row["static_candidate_svg"] = (
            f"../output/italic-v10-redraw/v10-final-font-glyph-svg/{filename}"
            if row.get("candidate_svg") else ""
        )
    review = json.loads(DECISIONS.read_text(encoding="utf-8")) if DECISIONS.exists() else {
        "version": "italic-v10-manual-review-v1", "decisions": {}
    }
    payload = {"report": {"glyphs": rows}, "review": review}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "window.ITALIC_QA_STATIC = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT} ({len(rows)} glyphs)")


if __name__ == "__main__":
    main()
