#!/usr/bin/env python3
"""Atomically bind all Bold decisions to v5.1 and pass approved Batch 1 glyphs."""
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
READY = ROOT / "qa" / "bold-ready-to-pass.json"
APPROVED = {
    "two",
    "uni042F",
    "brokenbar",
    "minute",
    "second",
    "uni21BA",
    "dagger",
}
METRICS_VERSION = "bold-metrics-v5.1"
DECISION_VERSION = "bold-manual-review-v5.1"


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
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    payload = json.loads(DECISIONS.read_text(encoding="utf-8-sig"))
    if len(rows) != 340 or len(payload.get("decisions", {})) != 340:
        raise SystemExit("expected 340 report rows and decisions")
    timestamp = datetime.now(timezone.utc).isoformat()
    for glyph, decision in payload["decisions"].items():
        row = rows[glyph]
        decision.update({
            "regular_hash": row["regular_hash"],
            "base_hash": row["base_hash"],
            "bold_hash": row["bold_hash"],
            "metrics_version": METRICS_VERSION,
        })
        if glyph in APPROVED:
            decision.update({
                "status": "pass",
                "updated_at": timestamp,
                "manual_override": True,
                "override_reason": "explicit-user-visual-approval-bold-v5-batch1",
                "overridden_findings": row["manual_blockers"],
            })
    payload["version"] = DECISION_VERSION
    payload["metrics_version"] = METRICS_VERSION
    counts = {}
    for decision in payload["decisions"].values():
        counts[decision["status"]] = counts.get(decision["status"], 0) + 1
    expected = {"pass": 226, "almost_done": 60, "needs_rework": 54}
    if counts != expected:
        raise SystemExit("unexpected migrated counts: " + repr(counts))
    atomic_write(DECISIONS, payload)
    ready = json.loads(READY.read_text(encoding="utf-8-sig"))
    ready["version"] = "bold-ready-to-pass-v5.1"
    ready["metrics_version"] = METRICS_VERSION
    ready["migrated_at"] = timestamp
    for glyph, value in ready.get("glyphs", {}).items():
        row = rows[glyph]
        value["regular_hash"] = row["regular_hash"]
        value["base_hash"] = row["base_hash"]
        value["bold_hash"] = row["bold_hash"]
    atomic_write(READY, ready)
    print("decisions=340; pass=226; almost=60; rework=54")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
