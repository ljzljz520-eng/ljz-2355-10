# -*- coding: utf-8 -*-
"""HTTP 服务（仅标准库）。

路由:
  GET  /api/meta                         当前版本/时间/缓存
  GET  /api/packages                     补丁包列表 + 预编译身份校验
  GET  /api/packages/{id}/summary        规则包摘要
  GET  /api/search?q=&as_of=&type=&all=  检索（默认当前版，可历史过滤）
  GET  /api/examples                     比赛示例列表（含勘误修正关系）
  GET  /api/examples/{id}                冻结闭包回放 + 逐步分数来源
  POST /api/explain                      指定补丁/as_of/快照解释自定义局面
  POST /api/report                       导出报告（规则包摘要+局面输入+追踪/诊断）
  POST /api/admin/reindex?fail_after=N   原子重建索引（可模拟中断）
  GET  /api/admin/index-status
  GET  /                                 前端单页
"""
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from .repository import Repo
from .seed import seed, NOW
from .service import RuleService
from .evaluator import RuleEngineError
from .graph import IllegalRecursion, DependencyError
from .indexing import IndexRebuildInterrupted

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def _diag(exc):
    if isinstance(exc, RuleEngineError):
        return 409, {"code": exc.code, "message": str(exc), "detail": exc.detail}
    if isinstance(exc, (IllegalRecursion, DependencyError)):
        return 422, {"code": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, IndexRebuildInterrupted):
        return 202, {"code": "INDEX_REBUILD_INTERRUPTED",
                     "message": str(exc), "done": exc.done, "total": exc.total}
    return 500, {"code": "INTERNAL", "message": str(exc),
                 "trace": traceback.format_exc()}


class App:
    def __init__(self, repo_path=":memory:"):
        self.repo = Repo(repo_path)
        seed(self.repo, verbose=False)
        self.service = RuleService(self.repo)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            u = urlparse(self.path)
            p, q = u.path.strip("/"), parse_qs(u.query)
            svc = app.service
            try:
                if p == "" :
                    return self._static("index.html", "text/html; charset=utf-8")
                if p.startswith("web/"):
                    return self._static(p.split("/", 1)[1], None)
                if p == "api/meta":
                    return self._send(200, {
                        "now": app.repo.get_meta("now"),
                        "current_package": app.repo.current_package(),
                        "cache": svc.cache_stats()})
                if p == "api/packages":
                    rows = app.repo.conn.execute(
                        "SELECT * FROM packages ORDER BY released_at").fetchall()
                    return self._send(200, {
                        "packages": [dict(r) for r in rows],
                        "identity_checks": svc.verify_precompiled()})
                if p.startswith("api/packages/") and p.endswith("/summary"):
                    pkg = p.split("/")[2]
                    compiled = svc.compile_for_package(pkg)
                    snap = app.repo.get_package_snapshot(pkg)
                    out = svc._rule_summary(compiled)
                    out["released_at"] = snap["released_at"]
                    return self._send(200, out)
                if p == "api/search":
                    return self._send(200, {"results": svc.search(
                        q.get("q", [""])[0],
                        as_of=q.get("as_of", [None])[0],
                        doc_type=q.get("type", [None])[0],
                        include_historical=q.get("all", ["0"])[0] in ("1", "true"))})
                if p == "api/examples":
                    rows = app.repo.conn.execute(
                        "SELECT * FROM examples ORDER BY played_at").fetchall()
                    data = []
                    for r in rows:
                        cr = app.repo.conn.execute(
                            "SELECT errata_id,corrected_hash,note FROM "
                            "example_corrections WHERE example_id=?",
                            (r["example_id"],)).fetchall()
                        data.append({**dict(r),
                                     "inputs": json.loads(r["inputs"]),
                                     "expected": json.loads(r["expected"]),
                                     "corrections": [dict(x) for x in cr]})
                    return self._send(200, {"examples": data})
                if p.startswith("api/examples/"):
                    return self._send(200, svc.explain_example(p.split("/")[2]))
                if p == "api/explain" and method == "POST":
                    b = self._body()
                    return self._send(200, svc.explain_custom(
                        b.get("inputs", {}), package_id=b.get("package_id"),
                        as_of=b.get("as_of"), snapshot_hash=b.get("snapshot_hash"),
                        node_budget=int(b.get("node_budget", 200_000))))
                if p == "api/report" and method == "POST":
                    b = self._body()
                    fmt = b.get("format", "json")
                    report = svc.build_report(
                        package_id=b.get("package_id"), as_of=b.get("as_of"),
                        snapshot_hash=b.get("snapshot_hash"),
                        inputs=b.get("inputs", {}),
                        node_budget=int(b.get("node_budget", 200_000)))
                    if fmt == "md":
                        md = _render_markdown(report).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "text/markdown; charset=utf-8")
                        self.send_header("Content-Length", str(len(md)))
                        self.end_headers()
                        self.wfile.write(md)
                        return
                    return self._send(200, report)
                if p == "api/admin/reindex" and method == "POST":
                    fa = q.get("fail_after", [None])[0]
                    n = svc.reindex(fail_after=int(fa) if fa else None)
                    return self._send(200, {"rebuilt": n,
                                            "status": svc.index_status()})
                if p == "api/admin/index-status":
                    return self._send(200, svc.index_status())
                self._send(404, {"code": "NOT_FOUND", "message": p})
            except Exception as e:
                code, payload = _diag(e)
                self._send(code, payload)

        def _static(self, name, ctype):
            path = os.path.join(WEB_DIR, name)
            if not os.path.isfile(path):
                return self._send(404, {"code": "NOT_FOUND"})
            data = open(path, "rb").read()
            if ctype is None:
                ctype = "application/octet-stream"
                if name.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                if name.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _render_markdown(report):
    lines = ["# 规则解释报告", ""]
    g = report.get("generated_for", {})
    lines += [f"- 补丁包: `{g.get('package_id') or '（未指定）'}`",
              f"- 时间点 as_of: `{g.get('as_of') or '（未指定）'}`",
              f"- 闭包快照: `{g.get('snapshot_hash') or '（未指定）'}`",
              f"- 闭包身份: `{report.get('closure_hash', '编译失败，无身份')}`", ""]
    lines += ["## 局面输入", "```json",
              json.dumps(report.get("situation_input", {}), ensure_ascii=False, indent=2),
              "```", ""]
    summ = report.get("rule_package_summary")
    if summ:
        lines += [f"## 规则包摘要（{summ['rule_count']} 条规则）",
                  f"- 内容身份 `{summ['hash']}`，输出根: {', '.join(summ['outputs'])}"]
        if summ["policies"]:
            lines.append(f"- 冲突策略: {', '.join(summ['policies'].keys())}")
        lines.append("")
        lines.append("| 规则 | 类别 | 槽位 | 公式 | 来源版本 |")
        lines.append("|---|---|---|---|---|")
        for r in summ["rules"]:
            lines.append(f"| {r['rule_id']} | {r['category']} | "
                         f"{r['slot'] or ''} | `{r['source']}` | {r['package_id']} |")
        lines.append("")
    if report.get("diagnostic"):
        d = report["diagnostic"]
        lines += ["## 诊断（无法求解）",
                  f"- 错误码: **{d['code']}**", f"- {d['message']}", ""]
    else:
        lines += ["## 最终分数", "```json",
                  json.dumps(report.get("score", {}), ensure_ascii=False, indent=2),
                  "```", "",
                  f"节点计数 {report.get('node_count')} / 预算 {report.get('node_budget')}",
                  "", "## 逐步分数来源"]
        for s in report.get("score_steps", []):
            mark = "✓" if s["applicable"] else "—"
            lines.append(f"### {mark} {s['title']} (`{s['rule_id']}`)")
            lines.append(f"- 类型: {s['kind']}; 值: **{s['value']}**")
            lines.append(f"- 公式: `{s['formula']}`")
            if s.get("inputs"):
                lines.append(f"- 输入: `{json.dumps(s['inputs'], ensure_ascii=False)}`")
            if s.get("refs"):
                lines.append(f"- 引用取值: `{json.dumps(s['refs'], ensure_ascii=False)}`")
            if s.get("note"):
                lines.append(f"- 备注: {s['note']}")
            if s.get("iterations"):
                lines.append(f"- 迭代 {s['iterations']} 轮收敛")
            lines.append("")
    return "\n".join(lines)


def main(host="127.0.0.1", port=8080, db_path=":memory:"):
    app = App(db_path)
    httpd = HTTPServer((host, port), make_handler(app))
    print(f"游戏规则百科已启动: http://{host}:{port}/  (db={db_path})")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=":memory:")
    a = ap.parse_args()
    main(a.host, a.port, a.db)
