# -*- coding: utf-8 -*-
"""关系库: 规则版本、依赖、生效区间、冲突策略、勘误、比赛示例、
闭包快照与检索索引（SQLite）。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    package_id   TEXT PRIMARY KEY,        -- 如 1.1.1
    kind         TEXT NOT NULL,           -- release / hotfix / test
    released_at  TEXT NOT NULL,           -- UTC ISO，同日补丁靠时刻分先后
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'released'
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id        TEXT PRIMARY KEY,      -- 版本行唯一 id
    rule_key       TEXT NOT NULL,         -- 跨版本稳定的规则键
    package_id     TEXT NOT NULL,
    title          TEXT NOT NULL,
    category       TEXT NOT NULL,         -- character / map / scoring / item
    summary        TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL,
    slot           TEXT,
    conflict_group TEXT,
    applicable_when TEXT,
    fixpoint       TEXT,                  -- JSON: max_iter/tol/init
    priority       INTEGER NOT NULL DEFAULT 0,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,                  -- NULL 表示开放区间
    ord            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rv_key_time ON rule_versions(rule_key, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_rv_pkg ON rule_versions(package_id);

CREATE TABLE IF NOT EXISTS rule_deps (
    from_rule_id TEXT NOT NULL,
    to_target    TEXT NOT NULL,           -- 规则 id 或槽位名
    dep_kind     TEXT NOT NULL DEFAULT 'ref'
);
CREATE INDEX IF NOT EXISTS idx_deps_from ON rule_deps(from_rule_id);
CREATE INDEX IF NOT EXISTS idx_deps_to ON rule_deps(to_target);

CREATE TABLE IF NOT EXISTS conflict_policies (
    policy_id      TEXT PRIMARY KEY,
    conflict_group TEXT NOT NULL,
    package_id     TEXT NOT NULL,
    mode           TEXT NOT NULL,         -- winner / priority
    payload        TEXT NOT NULL,         -- JSON
    priority       INTEGER NOT NULL DEFAULT 0,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT
);

CREATE TABLE IF NOT EXISTS errata (
    errata_id      TEXT PRIMARY KEY,
    published_at   TEXT NOT NULL,
    target_key     TEXT,                  -- 被修正的规则键
    target_example TEXT,                  -- 被标注的比赛示例
    severity       TEXT NOT NULL DEFAULT 'clarification',
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    relation       TEXT NOT NULL DEFAULT 'corrects'
);

CREATE TABLE IF NOT EXISTS examples (
    example_id    TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    played_at     TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,          -- 冻结整套规则闭包
    inputs        TEXT NOT NULL,          -- JSON 局面输入
    expected      TEXT NOT NULL,          -- JSON 原始已发布结果（勘误不改写）
    published     INTEGER NOT NULL DEFAULT 1,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS example_corrections (
    example_id       TEXT NOT NULL,
    errata_id        TEXT NOT NULL,
    corrected_hash   TEXT NOT NULL,       -- 修正版闭包身份
    recomputed       TEXT NOT NULL,       -- JSON 用修正闭包重算结果
    note             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (example_id, errata_id)
);

CREATE TABLE IF NOT EXISTS closure_snapshots (
    hash        TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,            -- package / query
    ref_label   TEXT NOT NULL,            -- package_id 或 示例 id/as_of
    as_of       TEXT NOT NULL,
    rules       TEXT NOT NULL,            -- JSON 冻结规则行
    policies    TEXT NOT NULL             -- JSON
);

CREATE TABLE IF NOT EXISTS search_index (
    doc_type TEXT NOT NULL,               -- rule / package / erratum / example
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


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repo:
    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- meta ----------
    def get_meta(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    # ---------- 时间点解析 ----------
    def active_packages(self, as_of):
        rows = self.conn.execute(
            "SELECT package_id FROM packages WHERE released_at<=? AND status='released'"
            " ORDER BY released_at", (as_of,)).fetchall()
        return [r["package_id"] for r in rows]

    def current_package(self, now=None):
        now = now or utcnow()
        row = self.conn.execute(
            "SELECT package_id FROM packages WHERE released_at<=? AND status='released'"
            " ORDER BY released_at DESC LIMIT 1", (now,)).fetchone()
        return row["package_id"] if row else None

    def rule_versions_as_of(self, as_of):
        rows = self.conn.execute(
            "SELECT * FROM rule_versions "
            "WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
            (as_of, as_of)).fetchall()
        return [dict(r) for r in rows]

    def policies_as_of(self, as_of):
        rows = self.conn.execute(
            "SELECT * FROM conflict_policies "
            "WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
            (as_of, as_of)).fetchall()
        out = {}
        # 同组多条：priority 高者胜（快照场景在写入时已确定）
        rows = sorted(rows, key=lambda r: (r["priority"], r["valid_from"]))
        for r in rows:
            # 直接展开裁决载荷，使“按查询组合”和“快照回放”看到同一种规范形态
            out[r["conflict_group"]] = {
                "mode": r["mode"], **json.loads(r["payload"]),
                "policy_id": r["policy_id"], "package_id": r["package_id"]}
        return out

    # ---------- 快照 ----------
    def save_snapshot(self, hash_, scope, ref_label, as_of, rules, policies):
        self.conn.execute(
            "INSERT OR IGNORE INTO closure_snapshots"
            "(hash,scope,ref_label,as_of,rules,policies) VALUES(?,?,?,?,?,?)",
            (hash_, scope, ref_label, as_of,
             json.dumps(rules, ensure_ascii=False, sort_keys=True),
             json.dumps(policies, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()

    def get_snapshot(self, hash_):
        row = self.conn.execute(
            "SELECT * FROM closure_snapshots WHERE hash=?", (hash_,)).fetchone()
        if not row:
            return None
        return {"hash": row["hash"], "scope": row["scope"],
                "ref_label": row["ref_label"], "as_of": row["as_of"],
                "rules": json.loads(row["rules"]),
                "policies": json.loads(row["policies"])}

    def get_package_snapshot(self, package_id):
        row = self.conn.execute(
            "SELECT * FROM closure_snapshots WHERE scope='package' AND ref_label=?",
            (package_id,)).fetchone()
        if not row:
            return None
        rules = json.loads(row["rules"])
        policies = json.loads(row["policies"])
        return {"released_at": row["as_of"], "rules": rules, "policies": policies}
