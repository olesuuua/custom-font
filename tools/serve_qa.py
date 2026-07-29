#!/usr/bin/env python3
"""Serve redraw and Bold QA pages and persist hash-bound review decisions."""
from __future__ import annotations

import argparse, csv, json, os, tempfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "qa"
VALID_STATUSES = {"pass", "almost_done", "needs_rework"}
CONFIGS = {
    "redraw": {
        "report": QA_ROOT / "assets" / "redraw-report.csv",
        "meta": None,
        "decisions": QA_ROOT / "redraw-manual-decisions.json",
        "version": "redraw-manual-review-v1",
        "source_key": "source_hash", "candidate_key": "candidate_hash",
    },
    "bold": {
        "report": QA_ROOT / "assets" / "bold-report.csv",
        "meta": QA_ROOT / "assets" / "bold-report.json",
        "decisions": QA_ROOT / "bold-manual-decisions.json",
        "version": "bold-manual-review-v4.1",
        "source_key": "regular_hash", "candidate_key": "bold_hash",
    },
}

def rows_for(mode):
    path = CONFIGS[mode]["report"]
    if not path.exists(): return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["glyph"]: row for row in csv.DictReader(handle)}

def decisions_for(mode):
    config = CONFIGS[mode]; path = config["decisions"]
    if not path.exists(): return {"version": config["version"], "decisions": {}}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("decisions", {}), dict):
        raise ValueError("invalid {} decisions document".format(mode))
    value.setdefault("version", config["version"]); value.setdefault("decisions", {})
    return value

def atomic_write(mode, value):
    target = CONFIGS[mode]["decisions"]
    descriptor, temporary = tempfile.mkstemp(prefix=mode+"-decisions-", suffix=".json", dir=str(QA_ROOT))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

class ReviewHandler(SimpleHTTPRequestHandler):
    server_version = "OlesuasQA/4.1"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(QA_ROOT), **kwargs)
    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(payload)
    def api_route(self):
        path = urlparse(self.path).path
        if path.startswith("/api/bold/"): return "bold", path[len("/api/bold/"):]
        if path.startswith("/api/"): return "redraw", path[len("/api/"):]
        return None, None
    def do_GET(self):
        mode, route = self.api_route()
        if mode == "bold" and route == "health":
            meta_path = CONFIGS["bold"]["meta"]
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
            decisions = decisions_for("bold")
            self.send_json(HTTPStatus.OK, {
                "project": "olesuas-hand-bold-qa",
                "server_version": self.server_version,
                "metrics_version": meta.get("version"),
                "decision_version": decisions.get("version"),
                "build_id": meta.get("build_id"),
                "regular_sfd_hash": meta.get("regular_sfd_hash"),
                "base_sfd_hash": meta.get("base_sfd_hash"),
                "bold_sfd_hash": meta.get("bold_sfd_hash"),
            })
            return
        if mode and route == "review":
            try: self.send_json(HTTPStatus.OK, decisions_for(mode))
            except (OSError, ValueError) as error: self.send_json(500, {"error": str(error)})
            return
        if mode and route == "report":
            meta = {}; meta_path = CONFIGS[mode]["meta"]
            if meta_path and meta_path.exists(): meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            self.send_json(HTTPStatus.OK, {"glyphs": list(rows_for(mode).values()), "meta": meta}); return
        super().do_GET()
    def do_PUT(self):
        mode, route = self.api_route()
        if not mode or not route.startswith("review/"):
            self.send_json(404, {"error": "unknown endpoint"}); return
        glyph = unquote(route[len("review/"):]); rows = rows_for(mode)
        if glyph not in rows: self.send_json(404, {"error": "unknown glyph"}); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096: raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8")); status = payload.get("status")
            if status not in VALID_STATUSES: raise ValueError("invalid status")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)}); return
        config=CONFIGS[mode]; row=rows[glyph]; values=decisions_for(mode)
        if mode == "bold" and status == "pass" and row.get("manual_blockers"):
            self.send_json(409, {"error": "Pass is blocked until manual blockers are repaired: " + row["manual_blockers"]}); return
        decision={"status": status, config["source_key"]: row[config["source_key"]], config["candidate_key"]: row[config["candidate_key"]], "updated_at": datetime.now(timezone.utc).isoformat()}
        if mode == "bold":
            decision["base_hash"] = row["base_hash"]
            decision["metrics_version"] = "bold-metrics-v4.1"
        values["decisions"][glyph]=decision; atomic_write(mode, values)
        self.send_json(200, {"glyph": glyph, **decision})
    def do_DELETE(self):
        mode, route = self.api_route()
        if not mode or not route.startswith("review/"):
            self.send_json(404, {"error": "unknown endpoint"}); return
        glyph=unquote(route[len("review/"):])
        if glyph not in rows_for(mode): self.send_json(404, {"error": "unknown glyph"}); return
        values=decisions_for(mode)
        if mode == "bold": values["decisions"].pop(glyph, None)
        else:
            row=rows_for(mode)[glyph]; config=CONFIGS[mode]
            values["decisions"][glyph]={"status":"needs_rework",config["source_key"]:row[config["source_key"]],config["candidate_key"]:row[config["candidate_key"]],"updated_at":datetime.now(timezone.utc).isoformat()}
        atomic_write(mode, values); self.send_json(200,{"glyph":glyph,"status":"unreviewed" if mode=="bold" else "needs_rework"})
    def end_headers(self):
        if self.path.split("?",1)[0].endswith((".ttf", ".json", ".csv")): self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def log_message(self, format_string, *args): print("{} - {}".format(self.address_string(), format_string % args), flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8000); args=parser.parse_args()
    if args.host not in ("127.0.0.1","localhost","::1"): parser.error("the review server is localhost-only")
    server=ThreadingHTTPServer((args.host,args.port),ReviewHandler); print("Font QA: http://{}:{}/".format(args.host,args.port),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__ == "__main__": main()
