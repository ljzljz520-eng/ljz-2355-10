"""Stdlib HTTP server: JSON API + static frontend.

Run:  python -m app.server  (http://127.0.0.1:8080)
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import explain as explain_mod
from . import searchindex
from .db import connect, init_db
from .packages import (build_closure, get_package, list_packages,
                       precompile_all, precompile_patch, precompiled_roots_at)
from .seed import seed
from .versions import overlapping_versions, patches_ordered

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_conn_lock = threading.Lock()


def get_conn():
    conn = connect()
    init_db(conn)
    if seed(conn):
        precompile_all(conn)
        searchindex.start_build(conn, resume=False)
        # fully build the initial index synchronously
        while searchindex.build_batch(conn, limit=50)["status"] == "building":
            pass
        for ex in conn.execute("SELECT id FROM examples").fetchall():
            explain_mod.freeze_example(conn, ex["id"])
    return conn


def _json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers -----------------------------------------------------------

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _serve_static(self, name):
        if name == "/" or not name:
            name = "/index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, name.lstrip("/")))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(os.path.splitext(path)[1],
                                                        "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        with _conn_lock:
            conn = self.server.conn
            try:
                if path.startswith("/api/"):
                    return self._api_get(conn, path, qs)
                return self._serve_static(path)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e), "type": type(e).__name__}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        with _conn_lock:
            conn = self.server.conn
            try:
                return self._api_post(conn, parsed.path, self._read_json())
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e), "type": type(e).__name__}, 500)

    # -- API: GET ----------------------------------------------------------

    def _api_get(self, conn, path, qs):
        one = lambda k, d=None: qs.get(k, [d])[0]
        if path == "/api/patches":
            return _json(self, {"patches": patches_ordered(conn)})
        if path == "/api/rules":
            rows = conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM rule_versions rv "
                " WHERE rv.rule_id=r.id AND rv.status!='draft') AS versions "
                "FROM rules r ORDER BY r.kind, r.id").fetchall()
            return _json(self, {"rules": [dict(r) for r in rows]})
        if path == "/api/rules/versions":
            rule_id = one("rule_id")
            rows = conn.execute(
                "SELECT * FROM rule_versions WHERE rule_id=? ORDER BY id",
                (rule_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["params"] = json.loads(d.pop("params_json"))
                d["expr"] = json.loads(d.pop("expr_json")) if d.get("expr_json") else None
                out.append(d)
            return _json(self, {"versions": out})
        if path == "/api/search":
            anchor = one("anchor_patch") or None
            history = one("history") == "1"
            current_only = not history and anchor is None
            return _json(self, searchindex.search(
                conn, one("q", ""), anchor_patch=anchor,
                kind=one("kind") or None, current_only=current_only,
                limit=int(one("limit", "20"))))
        if path == "/api/index/status":
            return _json(self, searchindex.status(conn))
        if path == "/api/packages":
            return _json(self, {"packages": list_packages(conn)})
        if path == "/api/package":
            pkg = get_package(conn, one("id"))
            return _json(self, pkg or {"error": "not found"}, 200 if pkg else 404)
        if path == "/api/examples":
            return _json(self, {"examples": explain_mod.list_examples(conn)})
        if path == "/api/example/replay":
            return _json(self, explain_mod.replay_frozen(conn, one("id")))
        if path == "/api/example/explain":
            return _json(self, explain_mod.freeze_example(conn, one("id")))
        if path == "/api/example/advisory":
            return _json(self, explain_mod.corrected_replay_advisory(
                conn, one("id"), one("anchor_patch") or None))
        if path == "/api/errata":
            rows = conn.execute(
                "SELECT e.*, b.rule_id AS rule_id, b.version_label AS bad_label, "
                "       f.version_label AS fixed_label "
                "FROM errata e JOIN rule_versions b ON b.id=e.bad_version "
                "JOIN rule_versions f ON f.id=e.fixed_version ORDER BY e.id").fetchall()
            return _json(self, {"errata": [dict(r) for r in rows]})
        if path == "/api/diagnostics/overlaps":
            return _json(self, {"overlaps": overlapping_versions(conn)})
        if path == "/api/report":
            pkg_id = one("package_id")
            ex_id = one("example_id")
            situation = None
            if ex_id:
                row = conn.execute(
                    "SELECT situation_json FROM examples WHERE id=?", (ex_id,)).fetchone()
                situation = json.loads(row["situation_json"]) if row else None
            return _json(self, explain_mod.export_report(conn, pkg_id, situation, ex_id))
        return _json(self, {"error": f"未知接口 {path}"}, 404)

    # -- API: POST ---------------------------------------------------------

    def _api_post(self, conn, path, body):
        if path == "/api/explain":
            return _json(self, explain_mod.explain_situation(
                conn, body["situation"], body["anchor_patch"],
                day_slot=body.get("day_slot"), roots=body.get("roots"),
                budget=int(body.get("budget", 5000))))
        if path == "/api/closure":
            return _json(self, build_closure(
                conn, body["anchor_patch"], body.get("roots", []),
                mode=body.get("mode", "query"),
                day_slot=body.get("day_slot"),
                tolerate_absent_roots=bool(body.get("tolerate_absent_roots"))))
        if path == "/api/precompile":
            if body.get("all"):
                return _json(self, {"packages": precompile_all(conn)})
            return _json(self, precompile_patch(
                conn, body["anchor_patch"], day_slot=body.get("day_slot")))
        if path == "/api/precompiled-roots":
            from .versions import coords_for
            coord = coords_for(conn, body["anchor_patch"], body.get("day_slot"))
            return _json(self, {"roots": precompiled_roots_at(conn, coord), "coord": coord})
        if path == "/api/index/start":
            return _json(self, searchindex.start_build(conn, resume=bool(body.get("resume", True))))
        if path == "/api/index/batch":
            return _json(self, searchindex.build_batch(conn, int(body.get("limit", 5))))
        if path == "/api/index/interrupt":
            return _json(self, searchindex.interrupt(conn))
        if path == "/api/report":
            situation = body.get("situation")
            example_id = body.get("example_id")
            if situation is None and example_id:
                row = conn.execute(
                    "SELECT situation_json FROM examples WHERE id=?", (example_id,)).fetchone()
                situation = json.loads(row["situation_json"]) if row else None
            return _json(self, explain_mod.export_report(
                conn, body["package_id"], situation, example_id))
        return _json(self, {"error": f"未知接口 {path}"}, 404)


def main(port=8080):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.conn = get_conn()
    print(f"Rules encyclopedia on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
