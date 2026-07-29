#!/usr/bin/env python3
"""Archive the exact reviewed Bold v3 state before v4 outline changes."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "checkpoints" / "bold-v3-reviewed"
ARTIFACTS = {
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-ready-to-pass.json": ROOT / "qa" / "bold-ready-to-pass.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if (TARGET / "manifest.json").exists():
        raise SystemExit("Bold v3 reviewed archive already exists")
    TARGET.mkdir(parents=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        shutil.copy2(source, TARGET / name)
        hashes[name] = sha256(TARGET / name)
    manifest = {
        "version": "bold-v3-reviewed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_hashes": hashes,
    }
    (TARGET / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Archived {} artifacts".format(len(hashes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
