# -*- coding: utf-8 -*-
"""检索索引。

* 索引构建采用“影子表 + 事务内交换”：先写 search_index_stage，
  全部成功后在单事务中删旧表/改名；中途任何异常（含注入的中断）
  都不会污染已发布的 search_index，服务继续可用。
* 搜索默认“当前版”（取 current_package 时刻仍生效的规则），
  但保留历史过滤 as_of：可检索任意历史时间点的规则版本。
"""
import json

STAGE_DDL = """
CREATE TABLE IF NOT EXISTS search_index_stage (
    doc_type TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    title    TEXT NOT NULL,
    body     TEXT NOT NULL,
    tags     TEXT NOT NULL DEFAULT '',
    package_id TEXT,
    valid_from TEXT,
    valid_to   TEXT,
    PRIMARY KEY (doc_type, doc_id)
);
"""


def _staging_ready(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_index_stage'"
    ).fetchone()
    return row is not None


def rebuild_index(repo, fail_after=None):
    """原子重建索引。fail_after: 写入 N 行后抛出异常以模拟“索引更新中断”。

    返回写入文档数。
    """
    conn = repo.conn
    conn.execute("DROP TABLE IF EXISTS search_index_stage")
    conn.execute("DROP TABLE IF EXISTS search_index_tmp_old")
    conn.execute(STAGE_DDL)
    conn.commit()

    docs = _collect_docs(repo)
    written = 0
    for d in docs:
        conn.execute(
            "INSERT INTO search_index_stage(doc_type,doc_id,title,body,tags,"
            "package_id,valid_from,valid_to) VALUES(?,?,?,?,?,?,?,?)",
            (d["doc_type"], d["doc_id"], d["title"], d["body"], d["tags"],
             d.get("package_id"), d.get("valid_from"), d.get("valid_to")))
        written += 1
        if fail_after is not None and written == fail_after:
            # 模拟崩溃：回滚影子表写入，已发布索引完好
            conn.rollback()
            raise IndexRebuildInterrupted(
                fail_after, len(docs),
                "索引更新在写入影子表阶段中断；线上 search_index 未受影响，可直接重试。")
    conn.commit()

    # 原子交换
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE search_index RENAME TO search_index_tmp_old")
        conn.execute("ALTER TABLE search_index_stage RENAME TO search_index")
        conn.execute("DROP TABLE search_index_tmp_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written


class IndexRebuildInterrupted(Exception):
    def __init__(self, done, total, message):
        self.done = done
        self.total = total
        super().__init__(message)


def index_status(repo):
    conn = repo.conn
    live = conn.execute("SELECT COUNT(*) n FROM search_index").fetchone()["n"]
    stage_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_index_stage'"
    ).fetchone()
    stage = None
    if stage_row:
        stage = conn.execute("SELECT COUNT(*) n FROM search_index_stage").fetchone()["n"]
    return {"live_docs": live,
            "staging_rows": stage,
            "staging_present": stage_row is not None}


def _collect_docs(repo):
    docs = []
    for r in repo.rule_versions_as_of(repo.get_meta("now")):
        docs.append({
            "doc_type": "rule", "doc_id": r["rule_id"],
            "title": r["title"], "body": f"{r['summary']} {r['source']}",
            "tags": f"rule {r['category']} {r['rule_key']} {r['package_id']}",
            "package_id": r["package_id"],
            "valid_from": r["valid_from"], "valid_to": r["valid_to"]})
    # 历史版本也建文档，但通过 tags/package 区分；默认搜索按当前生效过滤
    all_rules = repo.conn.execute("SELECT * FROM rule_versions").fetchall()
    live_ids = {d["doc_id"] for d in docs}
    for r in all_rules:
        if r["rule_id"] in live_ids:
            continue
        docs.append({
            "doc_type": "rule", "doc_id": r["rule_id"],
            "title": r["title"] + "（历史版本）",
            "body": f"{r['summary']} {r['source']}",
            "tags": f"rule historical {r['category']} {r['rule_key']} {r['package_id']}",
            "package_id": r["package_id"],
            "valid_from": r["valid_from"], "valid_to": r["valid_to"]})
    for p in repo.conn.execute("SELECT * FROM packages").fetchall():
        docs.append({
            "doc_type": "package", "doc_id": p["package_id"],
            "title": f"补丁 {p['package_id']} · {p['title']}",
            "body": f"{p['kind']} released {p['released_at']} {p['description']}",
            "tags": f"package {p['kind']} {p['package_id']}",
            "package_id": p["package_id"], "valid_from": p["released_at"]})
    for e in repo.conn.execute("SELECT * FROM errata").fetchall():
        docs.append({
            "doc_type": "errata", "doc_id": e["errata_id"],
            "title": f"勘误 {e['errata_id']} · {e['title']}",
            "body": e["body"],
            "tags": f"errata {e['severity']} {e['relation']} target:{e['target_key']}",
            "valid_from": e["published_at"]})
    for x in repo.conn.execute("SELECT * FROM examples").fetchall():
        docs.append({
            "doc_type": "example", "doc_id": x["example_id"],
            "title": f"比赛示例 · {x['title']}",
            "body": f"played {x['played_at']} snapshot {x['snapshot_hash']} {x['note']}",
            "tags": "example match frozen", "valid_from": x["played_at"]})
    return docs


def search(repo, q: str, as_of: str = None, doc_type: str = None,
           include_historical: bool = False, limit: int = 50):
    """检索。默认 as_of=当前（now），只返回该时刻生效的当前版规则；
    include_historical=True 或显式 as_of 时启用历史过滤。
    """
    now = repo.get_meta("now")
    current_pkg = repo.get_meta("current_package")
    terms = [t for t in (q or "").lower().split() if t]
    rows = repo.conn.execute("SELECT * FROM search_index").fetchall()
    out = []

    def alive(r, t):
        return r["valid_from"] <= t and (r["valid_to"] is None or r["valid_to"] > t)

    for r in rows:
        if doc_type and r["doc_type"] != doc_type:
            continue
        hay = f"{r['title']} {r['body']} {r['tags']}".lower()
        if terms and not all(t in hay for t in terms):
            continue
        tagged_hist = "historical" in (r["tags"] or "")
        is_hist = tagged_hist
        if r["doc_type"] == "rule":
            if as_of:
                # 历史过滤：只保留该时间点真正在生效区间内的版本
                if not alive(r, as_of):
                    continue
                is_hist = False
            elif include_historical:
                pass  # 显式要求包含全部历史版本
            else:
                # 默认当前版：按 now 的实际生效区间判定（过期热修也会被排除）
                if not alive(r, now):
                    continue
                is_hist = False
        elif as_of and r["valid_from"] and r["valid_from"] > as_of:
            continue
        out.append({
            "doc_type": r["doc_type"], "doc_id": r["doc_id"],
            "title": r["title"], "snippet": r["body"][:160],
            "tags": r["tags"], "package_id": r["package_id"],
            "valid_from": r["valid_from"], "valid_to": r["valid_to"],
            "historical": is_hist})
    # 相关性：标题命中优先，其次按时间倒序
    def score(d):
        t = d["title"].lower()
        return (0 if any(k in t for k in terms) else 1,
                d.get("valid_from") or "")
    out.sort(key=score)
    return out[:limit]
