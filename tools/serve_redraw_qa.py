#!/usr/bin/env python3
"""Serve the focused redraw QA page and persist manual review decisions."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "qa"
REPORT = QA_ROOT / "assets" / "redraw-report.csv"
DECISIONS = QA_ROOT / "redraw-manual-decisions.json"
VALID_STATUSES = {"pass", "almost_done", "needs_rework"}


def load_rows():
    if not REPORT.exists():
        return {}
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        return {row["glyph"]: row for row in csv.DictReader(handle)}


def empty_decisions():
    return {"version": "redraw-manual-review-v1", "decisions": {}}


def load_decisions():
    if not DECISIONS.exists():
        return empty_decisions()
    with DECISIONS.open(encoding="utf-8") as handle:
        values = json.load(handle)
    if not isinstance(values, dict) or not isinstance(values.get("decisions", {}), dict):
        raise ValueError("invalid decisions document")
    values.setdefault("version", "redraw-manual-review-v1")
    values.setdefault("decisions", {})
    return values


def atomic_write(values):
    QA_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="redraw-decisions-", suffix=".json", dir=str(QA_ROOT)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, DECISIONS)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ReviewHandler(SimpleHTTPRequestHandler):
    server_version = "RedrawQA/1.0"

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
        if path == "/api/review":
            try:
                self.send_json(HTTPStatus.OK, load_decisions())
            except (OSError, ValueError) as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        if path == "/api/report":
            self.send_json(HTTPStatus.OK, {"glyphs": list(load_rows().values())})
            return
        super().do_GET()

    def do_PUT(self):
        prefix = "/api/review/"
        path = urlparse(self.path).path
        if not path.startswith(prefix):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        glyph = unquote(path[len(prefix):])
        rows = load_rows()
        if glyph not in rows:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown glyph"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        status = payload.get("status") if isinstance(payload, dict) else None
        if status not in VALID_STATUSES:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid status"})
            return
        row = rows[glyph]
        values = load_decisions()
        decision = {
            "status": status,
            "source_hash": row["source_hash"],
            "candidate_hash": row["candidate_hash"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        values["decisions"][glyph] = decision
        atomic_write(values)
        self.send_json(HTTPStatus.OK, {"glyph": glyph, **decision})

    def do_DELETE(self):
        prefix = "/api/review/"
        path = urlparse(self.path).path
        if not path.startswith(prefix):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        glyph = unquote(path[len(prefix):])
        if glyph not in load_rows():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown glyph"})
            return
        values = load_decisions()
        row = load_rows()[glyph]
        decision = {
            "status": "needs_rework",
            "source_hash": row["source_hash"],
            "candidate_hash": row["candidate_hash"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        values["decisions"][glyph] = decision
        atomic_write(values)
        self.send_json(HTTPStatus.OK, {"glyph": glyph, **decision})

    def log_message(self, format_string, *args):
        print("{} - {}".format(self.address_string(), format_string % args), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the review server is localhost-only")
    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    print("Redraw QA: http://{}:{}/".format(args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
