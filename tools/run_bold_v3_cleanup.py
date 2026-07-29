#!/usr/bin/env python
"""Run bounded batches of isolated Almost Done Bold cleanup repairs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOLD = ROOT / "fontforge" / "bold.sfd"
ARCHIVE = ROOT / "checkpoints" / "bold-v2-reviewed"
WORK = ROOT / "qa" / "bold-v3-cleanup-work"
REPORT = ROOT / "qa" / "assets" / "bold-v3-cleanup.json"
WORKER = ROOT / "tools" / "repair_bold_cleanup_worker.py"
APPLY = ROOT / "tools" / "apply_bold_protection_batch.py"
FFPYTHON = Path(r"C:\Program Files\FontForgeBuilds\bin\ffpython.exe")
EXCLUDED = {
    "quotesingle", "comma", "parenleft", "parenright",
    "eight", "Z", "Zeta", "uni20A6", "uni2116",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def targets():
    decisions = json.loads(
        (ARCHIVE / "bold-manual-decisions.json").read_text(encoding="utf-8-sig")
    )["decisions"]
    with (ARCHIVE / "bold-report.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    return sorted(
        name for name, decision in decisions.items()
        if decision.get("status") == "almost_done"
        and rows[name].get("manual_blockers") == "bold-cleanup-blocked"
        and name not in EXCLUDED
    )


def run_batch(start, count):
    names = targets()
    selected = names[start:start + count]
    WORK.mkdir(parents=True, exist_ok=True)
    results, candidates = [], []
    for index, name in enumerate(selected, start=start):
        output = WORK / "{:04d}-{}.sfd".format(index, name)
        report = WORK / "{:04d}-{}.json".format(index, name)
        completed = subprocess.run(
            [str(FFPYTHON), str(WORKER), str(BOLD), name,
             str(output), str(report)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {
            "glyph": name, "status": "blocked", "error": "worker-crashed"
        }
        data["worker_returncode"] = completed.returncode
        results.append(data)
        if completed.returncode == 0 and data.get("status") == "ok":
            candidates.append({"glyph": name, "sfd": str(output.resolve())})
    if candidates:
        manifest = WORK / "apply-{:04d}.json".format(start)
        manifest.write_text(json.dumps(candidates), encoding="utf-8")
        subprocess.run(
            [str(FFPYTHON), str(APPLY), str(BOLD), str(manifest)],
            check=True,
        )
    fragment = WORK / "batch-{:04d}.json".format(start)
    fragment.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "start": start, "requested": len(selected),
        "accepted": len(candidates),
        "blocked": len(selected) - len(candidates),
        "remaining": max(0, len(names) - start - len(selected)),
    }, sort_keys=True))


def finalize():
    names = targets()
    found = {}
    for path in sorted(WORK.glob("batch-*.json")):
        for row in json.loads(path.read_text(encoding="utf-8")):
            found[row["glyph"]] = row
    missing = [name for name in names if name not in found]
    if missing:
        raise SystemExit("Cleanup batches are incomplete: {}".format(", ".join(missing)))
    rows = [found[name] for name in names]
    payload = {
        "version": "bold-v3-isolated-cleanup",
        "reviewed_v2_bold_sha256": digest(ARCHIVE / "bold.sfd"),
        "bold_sha256": digest(BOLD),
        "target_count": len(names),
        "accepted_count": sum(row.get("status") == "ok" for row in rows),
        "blocked_count": sum(row.get("status") != "ok" for row in rows),
        "blocked_glyphs": [
            row["glyph"] for row in rows if row.get("status") != "ok"
        ],
        "glyphs": rows,
    }
    REPORT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "target_count": payload["target_count"],
        "accepted_count": payload["accepted_count"],
        "blocked_count": payload["blocked_count"],
    }, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.finalize:
        finalize()
    elif args.start is None:
        raise SystemExit("--start is required for a cleanup batch")
    else:
        run_batch(args.start, args.count)


if __name__ == "__main__":
    main()
