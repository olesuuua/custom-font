#!/usr/bin/env python3
"""Atomically record explicit user visual approvals for structurally clean glyphs."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"

APPROVED_CODEPOINTS = {
    0x0042,  # B
    0x0398,  # Theta
    0x03A0,  # Pi
    0x20A9,  # won
    0x0052,  # R
    0x0062,  # b
    0x0067,  # g
    0x041F,  # Cyrillic Pe
    0x00BE,  # three quarters
    0x03A6,  # Phi
    0x03C4,  # tau
    0x03C6,  # phi
    0x0025,  # percent
}


def atomic_write(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.stem + "-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_codepoint = {
        int(row["codepoint"]): row for row in rows if row.get("codepoint")
    }
    payload = json.loads(DECISIONS.read_text(encoding="utf-8-sig"))
    changed = []
    timestamp = datetime.now(timezone.utc).isoformat()
    for codepoint in sorted(APPROVED_CODEPOINTS):
        row = by_codepoint[codepoint]
        glyph = row["glyph"]
        defects = (
            int(row["bold_self_intersections"])
            + int(row["bold_invalid_handles"])
            + int(row["bold_open_contours"])
        )
        if defects:
            raise SystemExit(f"{glyph} has a real contour defect and cannot be overridden")
        decision = payload["decisions"].get(glyph, {})
        decision.update(
            {
                "status": "pass",
                "regular_hash": row["regular_hash"],
                "base_hash": row["base_hash"],
                "bold_hash": row["bold_hash"],
                "metrics_version": "bold-metrics-v6",
                "updated_at": timestamp,
                "manual_override": True,
                "override_reason": "explicit-user-visual-approval-2026-07-29",
                "overridden_findings": row["manual_blockers"],
            }
        )
        payload["decisions"][glyph] = decision
        changed.append(glyph)
    atomic_write(DECISIONS, payload)
    print("passed={}: {}".format(len(changed), ", ".join(changed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
