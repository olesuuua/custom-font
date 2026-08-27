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
REPORT = ROOT / "output" / "italic-import" / "italic-glyph-report.csv"
DECISIONS = QA_ROOT / "italic-manual-decisions.json"
REVIEW_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-review.ttf"
QA_REVIEW_TTF = QA_ROOT / "assets" / "italic-review.ttf"
VALID_STATUSES = {"pass", "almost_done", "needs_rework"}
ARTIFACT_DIRECTORIES = {
    "source_svg": "exact-source-glyph-svg",
    "compact_svg": "xopp-candidate-glyph-svg",
    "candidate_svg": "final-font-glyph-svg",
}


def load_rows():
    if not REPORT.exists():
        return {}
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        return {row["glyph"]: row for row in csv.DictReader(handle)}


def artifact_for(row, field):
    allowed = (ROOT / "output" / "italic-import").resolve()
    supplied = Path(row.get(field, "")).resolve() if row.get(field) else None
    if supplied and allowed in supplied.parents and supplied.exists():
        return supplied
    directory = ARTIFACT_DIRECTORIES[field]
    filename = f"{int(row['index']):03d}-{row['glyph']}.svg"
    fallback = allowed / directory / filename
    return fallback if fallback.exists() else None


def empty_decisions():
    return {"version": "italic-manual-review-v1", "decisions": {}}


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


class Handler(SimpleHTTPRequestHandler):
    server_version = "ItalicQA/1.0"

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
            ("/api/compact/", "compact_svg"),
            ("/api/candidate/", "candidate_svg"),
        ):
            if path.startswith(prefix):
                glyph = unquote(path[len(prefix):])
                row = load_rows().get(glyph)
                artifact = artifact_for(row, field) if row else None
                allowed = (ROOT / "output" / "italic-import").resolve()
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
        values = load_decisions()
        decision = {
            "status": status,
            "source_hash": row["source_hash"],
            "candidate_hash": row["candidate_hash"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        values.setdefault("decisions", {})[glyph] = decision
        atomic_write(values)
        self.send_json(HTTPStatus.OK, {"glyph": glyph, **decision})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8015)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the review server is localhost-only")
    if REVIEW_TTF.exists():
        QA_REVIEW_TTF.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REVIEW_TTF, QA_REVIEW_TTF)
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
