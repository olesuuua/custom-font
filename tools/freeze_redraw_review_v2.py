#!/usr/bin/env python3
"""Freeze the authoritative redraw-v2 review as redraw-reviewed-v2."""

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
CHECKPOINT = ROOT / "checkpoints" / "redraw-reviewed-v2"
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


def valid_decision(row: dict, decisions: dict) -> dict | None:
    decision = decisions.get(row["glyph"])
    if not decision:
        return None
    if (decision.get("source_hash") != row["source_hash"]
            or decision.get("candidate_hash") != row["candidate_hash"]):
        return None
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    missing = [str(path) for path in ARTIFACTS.values() if not path.is_file()]
    if missing:
        raise SystemExit("Missing reviewed artifacts: " + ", ".join(missing))

    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0]) if rows else []
    with DECISIONS.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    decisions = payload.setdefault("decisions", {})
    now = datetime.now(timezone.utc).isoformat()

    locked, repair = {}, {}
    counts = {"automatic_pass": 0, "pass": 0, "almost_done": 0, "needs_rework": 0}
    for row in rows:
        decision = valid_decision(row, decisions)
        automatic = row.get("automatic_pass", "").lower() == "true"
        # An explicit review is authoritative even if a later metrics refresh also
        # calls the outline an automatic pass. This preserves review provenance.
        if decision and decision.get("status") in {"pass", "almost_done", "needs_rework"}:
            status = decision["status"]
        elif automatic:
            status = "automatic_pass"
        else:
            status = "needs_rework"
            decisions[row["glyph"]] = {
                "status": status,
                "source_hash": row["source_hash"],
                "candidate_hash": row["candidate_hash"],
                "updated_at": now,
            }
        counts[status] += 1
        row["manual_status"] = "unreviewed" if status == "automatic_pass" else status
        row["effective_status"] = (
            "automatic-pass" if status == "automatic_pass" else
            "manual-pass" if status == "pass" else status
        )
        row["current_review_status"] = status
        row["needs_manual_review"] = "false" if status in {"automatic_pass", "pass"} else "true"
        item = {
            "candidate_hash": row["candidate_hash"],
            "source_hash": row["source_hash"],
            "status": status,
        }
        (locked if status in {"automatic_pass", "pass"} else repair)[row["glyph"]] = item

    expected = {"automatic_pass": 201, "pass": 60, "almost_done": 19, "needs_rework": 54}
    if counts != expected:
        raise SystemExit("Review counts changed; expected {} but found {}".format(expected, counts))
    if len(rows) != 334 or len(locked) != 261 or len(repair) != 73:
        raise SystemExit("Unexpected review partition: rows={} locked={} repair={}".format(
            len(rows), len(locked), len(repair)))

    payload.update({
        "schema_version": max(2, int(payload.get("schema_version", 1))),
        "reviewed_snapshot": "redraw-reviewed-v2",
        "candidate_version": "redraw-v2",
    })
    atomic_json(DECISIONS, payload)
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)

    if CHECKPOINT.exists():
        if not args.force:
            raise SystemExit("Checkpoint already exists: {}".format(CHECKPOINT))
        shutil.rmtree(CHECKPOINT)
    CHECKPOINT.mkdir(parents=True)
    for name, source in ARTIFACTS.items():
        shutil.copy2(source, CHECKPOINT / name)
    manifest = {
        "version": "redraw-reviewed-v2",
        "created_at": now,
        "base_version": "redraw-v2",
        "glyph_count": len(rows),
        "automatic_pass_count": counts["automatic_pass"],
        "manual_pass_count": counts["pass"],
        "almost_done_count": counts["almost_done"],
        "needs_rework_count": counts["needs_rework"],
        "locked_glyphs": locked,
        "repair_queue": repair,
        "artifact_hashes": {name: file_hash(CHECKPOINT / name) for name in ARTIFACTS},
    }
    atomic_json(CHECKPOINT / "manifest.json", manifest)
    print(json.dumps({**counts, "locked": len(locked), "repair_queue": len(repair)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
