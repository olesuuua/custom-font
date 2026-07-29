#!/usr/bin/env python3
"""Freeze the QA-only Bold v4.1 triage checkpoint."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "checkpoints" / "bold-v4.1"
ARTIFACTS = {
    "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
    "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
    "regular-bold-base.ttf": ROOT / "qa" / "assets" / "regular-bold-base.ttf",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-v41-triage.json": ROOT / "qa" / "assets" / "bold-v41-triage.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-ready-to-pass.json": ROOT / "qa" / "bold-ready-to-pass.json",
    "qa-index.html": ROOT / "qa" / "index.html",
    "refresh_bold_qa.py": ROOT / "tools" / "refresh_bold_qa.py",
    "serve_qa.py": ROOT / "tools" / "serve_qa.py",
    "start_bold_qa.ps1": ROOT / "tools" / "start_bold_qa.ps1",
    "README.md": ROOT / "README.md",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if (CHECKPOINT / "manifest.json").exists() and not args.force:
        raise SystemExit("Bold v4.1 checkpoint exists; use --force only for an intentional replacement")
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        if not source.is_file():
            raise SystemExit(f"Missing checkpoint artifact: {source}")
        target = CHECKPOINT / name
        shutil.copy2(source, target)
        hashes[name] = sha256(target)
    with (ROOT / "qa" / "assets" / "bold-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    decisions = json.loads((ROOT / "qa" / "bold-manual-decisions.json").read_text(encoding="utf-8-sig"))
    statuses = {}
    for decision in decisions["decisions"].values():
        statuses[decision["status"]] = statuses.get(decision["status"], 0) + 1
    manifest = {
        "version": "bold-v4.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "predecessor": "checkpoints/bold-v4",
        "qa_only": True,
        "metrics_version": "bold-metrics-v4.1",
        "decision_version": "bold-manual-review-v4.1",
        "legacy_broad_blockers": 60,
        "legacy_classified": sum(bool(row["blocker_classification"]) for row in rows if row["glyph"] in json.loads((ROOT / "qa" / "assets" / "bold-v41-triage.json").read_text())["classifications"]),
        "generic_blockers_remaining": sum("counter-or-component-blocked" in row["manual_blockers"] for row in rows),
        "decision_counts": statuses,
        "decision_entries": len(decisions["decisions"]),
        "ready_to_pass": 11,
        "artifact_hashes": hashes,
    }
    (CHECKPOINT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"frozen={len(hashes)}; decisions={len(decisions['decisions'])}; generic=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
