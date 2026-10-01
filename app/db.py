"""Relational storage for the game-rules encyclopedia.

Stores patches, rules (characters/maps/items/scoring...), rule versions with
their effective intervals, dependency edges, errata + correction relations,
published example situations (frozen inputs), compiled rule closures, and the
interruptible search index.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("RULES_DB", os.path.join(os.path.dirname(__file__), "..", "data", "rules.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS patches (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  title TEXT NOT NULL,
  released_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('character','map','item','scoring','mechanic')),
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rule_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT NOT NULL REFERENCES rules(id),
  version_label TEXT NOT NULL,
  patch_id TEXT NOT NULL REFERENCES patches(id),
  status TEXT NOT NULL CHECK (status IN ('draft','active','superseded','retired','corrected')),
  body TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  expr_json TEXT,
  effective_start_patch TEXT REFERENCES patches(id),
  effective_end_patch TEXT REFERENCES patches(id),
  effective_start_seq INTEGER NOT NULL,
  effective_end_seq INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_version INTEGER NOT NULL REFERENCES rule_versions(id) ON DELETE CASCADE,
  target_rule_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('refs','modifies','requires')),
  UNIQUE(source_version, target_rule_id, kind)
);

CREATE TABLE IF NOT EXISTS errata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patch_id TEXT NOT NULL REFERENCES patches(id),
  bad_version INTEGER NOT NULL REFERENCES rule_versions(id),
  fixed_version INTEGER NOT NULL REFERENCES rule_versions(id),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS examples (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  anchor_patch TEXT NOT NULL REFERENCES patches(id),
  published_patch TEXT NOT NULL REFERENCES patches(id),
  situation_json TEXT NOT NULL,
  frozen_package_id TEXT REFERENCES rule_packages(package_id),
  expected_score REAL,
  published_on TEXT NOT NULL DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS rule_packages (
  package_id TEXT PRIMARY KEY,
  anchor_patch TEXT NOT NULL REFERENCES patches(id),
  anchor_coord INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('precompiled','query')),
  root_rule_ids_json TEXT NOT NULL,
  rule_version_ids_json TEXT NOT NULL,
  expressions_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','error')),
  diagnostics_json TEXT NOT NULL DEFAULT '[]',
  stats_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS package_cache (
  cache_key TEXT PRIMARY KEY,
  package_id TEXT NOT NULL REFERENCES rule_packages(package_id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS index_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_tokens (
  token TEXT NOT NULL,
  rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
  tf INTEGER NOT NULL,
  PRIMARY KEY (token, rule_version_id)
);

CREATE TABLE IF NOT EXISTS explanations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id TEXT NOT NULL REFERENCES rule_packages(package_id),
  example_id TEXT REFERENCES examples(id),
  situation_json TEXT NOT NULL,
  score REAL,
  trace_json TEXT NOT NULL,
  diagnostics_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rv_rule_patch ON rule_versions(rule_id, patch_id);
CREATE INDEX IF NOT EXISTS idx_dep_source ON rule_dependencies(source_version);
CREATE INDEX IF NOT EXISTS idx_index_tokens_tok ON index_tokens(token);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def row_to_version(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "version_label": row["version_label"],
        "patch_id": row["patch_id"],
        "status": row["status"],
        "body": row["body"],
        "params": json.loads(row["params_json"] or "{}"),
        "expr": json.loads(row["expr_json"]) if row["expr_json"] else None,
        "effective_start_patch": row["effective_start_patch"],
        "effective_end_patch": row["effective_end_patch"],
        "effective_start_seq": row["effective_start_seq"],
        "effective_end_seq": row["effective_end_seq"],
    }
