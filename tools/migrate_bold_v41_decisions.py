#!/usr/bin/env python3
"""Migrate unchanged Bold v4 decisions to the v4.1 metrics contract."""
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
METRICS_VERSION = "bold-metrics-v4.1"
DECISION_VERSION = "bold-manual-review-v4.1"


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
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
    decisions = payload.get("decisions", {})
    migrated = {}
    for glyph, decision in decisions.items():
        row = rows.get(glyph)
        if row is None:
            raise SystemExit(f"Decision glyph missing from report: {glyph}")
        for key in ("regular_hash", "base_hash", "bold_hash"):
            if decision.get(key) != row.get(key):
                raise SystemExit(f"Outline hash changed during metrics-only migration: {glyph} {key}")
        migrated[glyph] = dict(decision, metrics_version=METRICS_VERSION)
    if len(migrated) != 174:
        raise SystemExit(f"Expected 174 decisions, found {len(migrated)}")
    payload["version"] = DECISION_VERSION
    payload["metrics_version"] = METRICS_VERSION
    payload["migrated_at"] = datetime.now(timezone.utc).isoformat()
    payload["decisions"] = migrated
    atomic_json(DECISIONS, payload)

    ready = json.loads(READY.read_text(encoding="utf-8-sig"))
    if len(ready.get("glyphs", {})) != 11:
        raise SystemExit("Expected the frozen 11-glyph Ready-to-Pass queue")
    ready["metrics_version"] = METRICS_VERSION
    ready["version"] = "bold-ready-to-pass-v4.1"
    ready["migrated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(READY, ready)
    print(f"migrated={len(migrated)}; ready={len(ready['glyphs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
