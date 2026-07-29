#!/usr/bin/env python
"""Create the overlap-cleaned Regular source used exclusively for Bold."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "fontforge" / "redrawn.sfd"
TARGET = ROOT / "fontforge" / "regular-bold-base.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-base-cleanup.csv"
META = ROOT / "qa" / "assets" / "bold-base-cleanup.json"
BATCH = ROOT / "tools" / "prepare_bold_base_batch.py"
FFPYTHON = Path(r"C:\Program Files\FontForgeBuilds\bin\ffpython.exe")
FIELDS = [
    "glyph", "codepoint", "status", "backend", "source_hash", "cleaned_hash",
    "source_points", "cleaned_points", "point_delta", "changed", "idempotent",
    "iou_64", "iou_128", "source_components_32", "cleaned_components_32",
    "source_counters_32", "cleaned_counters_32", "source_components_64",
    "cleaned_components_64", "source_counters_64", "cleaned_counters_64",
    "source_components_128", "cleaned_components_128", "source_counters_128",
    "cleaned_counters_128", "source_components_256", "cleaned_components_256",
    "source_counters_256", "cleaned_counters_256", "removed_micro_regions",
    "source_intersections", "cleaned_intersections", "source_open_contours",
    "cleaned_open_contours", "source_invalid_handles", "cleaned_invalid_handles",
    "failure_reasons",
]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def glyph_count(path):
    return sum(
        line.startswith("StartChar: ")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ffpython", type=Path, default=FFPYTHON)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if TARGET.exists() and not args.force:
        raise SystemExit("Refusing to overwrite {} without --force".format(TARGET))
    if not args.ffpython.is_file():
        raise SystemExit("FontForge Python was not found: {}".format(args.ffpython))
    source_hash = sha256(SOURCE)
    shutil.copy2(SOURCE, TARGET)
    count = glyph_count(SOURCE)
    rows = []
    with tempfile.TemporaryDirectory(prefix="bold-base-batches-", dir=str(ROOT / "qa")) as temp:
        work = Path(temp)
        for start in range(0, count, args.batch_size):
            end = min(start + args.batch_size, count)
            row_file = work / "rows-{:04d}.json".format(start)
            subprocess.run(
                [str(args.ffpython), str(BATCH), str(SOURCE), str(TARGET),
                 str(start), str(end), str(work / "glyphs"), str(row_file)],
                check=True,
            )
            rows.extend(json.loads(row_file.read_text(encoding="utf-8")))
            print("Cleaned and validated glyphs {}-{} of {}".format(start + 1, end, count))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    blockers = [row["glyph"] for row in rows if row["status"] == "base_cleanup_blocked"]
    changed = [row["glyph"] for row in rows if row.get("changed") == "true"]
    micro = [row["glyph"] for row in rows if int(row.get("removed_micro_regions") or 0) > 0]
    payload = {
        "version": "bold-base-cleanup-v2",
        "source_path": "fontforge/redrawn.sfd",
        "base_path": "fontforge/regular-bold-base.sfd",
        "source_sha256": source_hash,
        "base_sha256": sha256(TARGET),
        "glyph_count": len(rows),
        "changed_count": len(changed),
        "micro_artifact_removed_count": len(micro),
        "blocked_glyphs": blockers,
        "blocked_count": len(blockers),
        "build_ready": not blockers,
    }
    META.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
