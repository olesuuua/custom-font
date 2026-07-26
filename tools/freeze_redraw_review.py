#!/usr/bin/env python3
"""Freeze the completed redraw review and normalize explicit rework decisions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "qa" / "assets" / "redraw-report.csv"
DECISIONS = ROOT / "qa" / "redraw-manual-decisions.json"
CHECKPOINT = ROOT / "checkpoints" / "redraw-reviewed-v1"
ARTIFACTS = {
    "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
    "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
    "redraw-review.sfd": ROOT / "fontforge" / "redraw-review.sfd",
    "redraw-review.ttf": ROOT / "qa" / "assets" / "redraw-review.ttf",
    "redraw-report.csv": REPORT,
    "redraw-manual-decisions.json": DECISIONS,
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="replace an existing checkpoint")
    args = parser.parse_args()

    missing = [str(path) for path in ARTIFACTS.values() if not path.is_file()]
    if missing:
        raise SystemExit("Missing reviewed artifacts: " + ", ".join(missing))

    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with DECISIONS.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    decisions = payload.setdefault("decisions", {})
    now = datetime.now(timezone.utc).isoformat()
    added = []
    for row in rows:
        if row["automatic_pass"].lower() == "true" or row["glyph"] in decisions:
            continue
        decisions[row["glyph"]] = {
            "status": "needs_rework",
            "source_hash": row["source_hash"],
            "candidate_hash": row["candidate_hash"],
            "updated_at": now,
        }
        added.append(row["glyph"])
    payload["schema_version"] = max(2, int(payload.get("schema_version", 1)))
    payload["reviewed_snapshot"] = "redraw-reviewed-v1"
    atomic_json(DECISIONS, payload)

    if CHECKPOINT.exists():
        if not args.force:
            raise SystemExit(f"Checkpoint already exists: {CHECKPOINT}")
        shutil.rmtree(CHECKPOINT)
    CHECKPOINT.mkdir(parents=True)
    for name, source in ARTIFACTS.items():
        shutil.copy2(source, CHECKPOINT / name)

    locked = {}
    repair = {}
    for row in rows:
        decision = decisions.get(row["glyph"], {})
        status = decision.get("status", "automatic_pass" if row["automatic_pass"].lower() == "true" else "needs_rework")
        item = {"candidate_hash": row["candidate_hash"], "source_hash": row["source_hash"], "status": status}
        if row["automatic_pass"].lower() == "true" or status == "pass":
            locked[row["glyph"]] = item
        else:
            repair[row["glyph"]] = item
    manifest = {
        "version": "redraw-reviewed-v1",
        "created_at": now,
        "glyph_count": len(rows),
        "automatic_pass_count": sum(row["automatic_pass"].lower() == "true" for row in rows),
        "manual_pass_count": sum(value.get("status") == "pass" for value in decisions.values()),
        "almost_done_count": sum(value.get("status") == "almost_done" for value in decisions.values()),
        "needs_rework_count": sum(value.get("status") == "needs_rework" for value in decisions.values()),
        "newly_tagged_needs_rework": added,
        "locked_glyphs": locked,
        "repair_queue": repair,
        "artifact_hashes": {name: file_hash(CHECKPOINT / name) for name in ARTIFACTS},
    }
    atomic_json(CHECKPOINT / "manifest.json", manifest)
    print(json.dumps({k: manifest[k] for k in ("glyph_count", "automatic_pass_count", "manual_pass_count", "almost_done_count", "needs_rework_count")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
