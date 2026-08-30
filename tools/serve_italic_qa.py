#!/usr/bin/env python3
"""Serve the Italic glyph reviewer and persist hash-bound decisions."""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "qa"
REPORT = ROOT / "output" / "italic-v10-redraw" / "italic-v10-glyph-report.csv"
FAMILY_REPORT = ROOT / "output" / "italic-v10-redraw" / "family-registry.json"
DECISIONS = QA_ROOT / "italic-v10-manual-decisions.json"
SOURCE_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v2-review.ttf"
REVIEW_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v10-redraw-review.ttf"
QA_SOURCE_TTF = QA_ROOT / "assets" / "italic-v2-source.ttf"
QA_REVIEW_TTF = QA_ROOT / "assets" / "italic-v10-review.ttf"
VALID_STATUSES = {"pass", "almost_done", "needs_rework"}
STATUS_RANK = {"pass": 0, "almost_done": 1, "needs_rework": 2}
ARTIFACT_DIRECTORIES = {
    "source_svg": "v2-source-glyph-svg",
    "candidate_svg": "v10-final-font-glyph-svg",
}


def load_rows():
    if not REPORT.exists():
        return {}
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        return {row["glyph"]: row for row in csv.DictReader(handle)}


def artifact_for(row, field):
    allowed = (ROOT / "output" / "italic-v10-redraw").resolve()
    supplied = Path(row.get(field, "")).resolve() if row.get(field) else None
    if supplied and allowed in supplied.parents and supplied.exists():
        return supplied
    directory = ARTIFACT_DIRECTORIES[field]
    filename = f"{int(row['index']):03d}-{row['glyph']}.svg"
    fallback = allowed / directory / filename
    return fallback if fallback.exists() else None


def empty_decisions():
    return {"version": "italic-v10-manual-review-v1", "decisions": {}}


def load_decisions():
    if not DECISIONS.exists():
        return empty_decisions()
    values = json.loads(DECISIONS.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or not isinstance(values.get("decisions", {}), dict):
        raise ValueError("invalid Italic decisions document")
    return values


def atomic_write(values):
    QA_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="italic-decisions-", suffix=".json", dir=QA_ROOT)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, DECISIONS)
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "generate_italic_static_qa.py")],
            cwd=str(ROOT), check=True, stdout=subprocess.DEVNULL,
        )
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def current_decision(row, decisions):
    decision = decisions.get(row["glyph"])
    if not decision:
        return None
    if decision.get("source_hash") != row["source_hash"] or decision.get("candidate_hash") != row["candidate_hash"]:
        return None
    return decision


def identical_group(glyph, rows):
    if glyph not in rows:
        return {glyph}
    group = {
        name for name, row in rows.items()
        if row.get("editable_hash") == rows[glyph].get("editable_hash")
        and row.get("editable_hash")
    }
    if FAMILY_REPORT.exists():
        registry = json.loads(FAMILY_REPORT.read_text(encoding="utf-8"))
        targets = registry.get("targets", {})
        relevant = []
        for name, spec in targets.items():
            family = spec.get("family", "")
            if family.startswith("latin-cyrillic") or family == "detected-reused-outline":
                relevant.append((name, spec))
        matched_keys = {
            (spec.get("family"), spec.get("donor"))
            for name, spec in relevant
            if glyph in {name, spec.get("donor")}
        }
        for name, spec in relevant:
            if (spec.get("family"), spec.get("donor")) in matched_keys:
                group.update(member for member in (name, spec.get("donor")) if member in rows)
    return group or {glyph}


def propagate_existing_identical_decisions():
    rows = load_rows()
    if not rows or not DECISIONS.exists():
        return
    values = load_decisions()
    decisions = values.setdefault("decisions", {})
    visited = set()
    changed = False
    for glyph in rows:
        if glyph in visited:
            continue
        group = identical_group(glyph, rows)
        visited.update(group)
        valid = [
            current_decision(rows[name], decisions)
            for name in group
        ]
        valid = [decision for decision in valid if decision and decision.get("status") in STATUS_RANK]
        if not valid:
            continue
        status = max((decision["status"] for decision in valid), key=STATUS_RANK.get)
        now = datetime.now(timezone.utc).isoformat()
        for name in group:
            row = rows[name]
            old = current_decision(row, decisions)
            if old and old.get("status") == status:
                continue
            decisions[name] = {
                "status": status, "source_hash": row["source_hash"],
                "candidate_hash": row["candidate_hash"], "updated_at": now,
            }
            changed = True
    if changed:
        atomic_write(values)


class Handler(SimpleHTTPRequestHandler):
    server_version = "ItalicV10QA/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(QA_ROOT), **kwargs)

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/report":
            self.send_json(HTTPStatus.OK, {"glyphs": list(load_rows().values())})
            return
        if path == "/api/review":
            self.send_json(HTTPStatus.OK, load_decisions())
            return
        for prefix, field in (
            ("/api/source/", "source_svg"),
            ("/api/candidate/", "candidate_svg"),
        ):
            if path.startswith(prefix):
                glyph = unquote(path[len(prefix):])
                row = load_rows().get(glyph)
                artifact = artifact_for(row, field) if row else None
                allowed = (ROOT / "output" / "italic-v10-redraw").resolve()
                if not artifact or allowed not in artifact.parents or not artifact.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = artifact.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mimetypes.guess_type(artifact.name)[0] or "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        if path == "/":
            self.path = "/italic.html"
        super().do_GET()

    def do_PUT(self):
        prefix = "/api/review/"
        path = urlparse(self.path).path
        if not path.startswith(prefix):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        glyph = unquote(path[len(prefix):])
        row = load_rows().get(glyph)
        if not row:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown glyph"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            status = payload.get("status")
            if status not in VALID_STATUSES:
                raise ValueError("invalid status")
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        rows = load_rows()
        values = load_decisions()
        decisions = values.setdefault("decisions", {})
        group = identical_group(glyph, rows)
        statuses = [status]
        for member in group:
            existing = current_decision(rows[member], decisions)
            if existing and existing.get("status") in STATUS_RANK:
                statuses.append(existing["status"])
        propagated_status = max(statuses, key=STATUS_RANK.get)
        updated = {}
        now = datetime.now(timezone.utc).isoformat()
        for member in sorted(group):
            member_row = rows[member]
            decision = {
                "status": propagated_status,
                "source_hash": member_row["source_hash"],
                "candidate_hash": member_row["candidate_hash"],
                "updated_at": now,
            }
            decisions[member] = decision
            updated[member] = decision
        atomic_write(values)
        self.send_json(HTTPStatus.OK, {
            "glyph": glyph, **updated[glyph], "updated": updated,
            "propagated_to": sorted(group - {glyph}),
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8015)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the review server is localhost-only")
    QA_REVIEW_TTF.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_TTF.exists(): shutil.copy2(SOURCE_TTF, QA_SOURCE_TTF)
    if REVIEW_TTF.exists(): shutil.copy2(REVIEW_TTF, QA_REVIEW_TTF)
    propagate_existing_identical_decisions()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Italic QA: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
