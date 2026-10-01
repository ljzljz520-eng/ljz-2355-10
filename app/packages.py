"""Rule closure construction: dependency validation + frozen packages.

Two production strategies are supported and deliberately compared:

* ``precompiled`` - one validated, content-addressed package per patch
  (built ahead of time; answering a query is a single indexed lookup);
* ``query`` - closure assembled on demand from explicit roots; identical
  requests map to the same cache identity, and the frozen content hash is
  still computed, so a query closure is byte-identical to a precompiled one
  whenever they cover the same rules.

A package FREEZES THE ENTIRE CLOSURE: not just a character's numeric value but
also every scoring formula, map condition, modifier and parameter it reaches.
Replaying at an old anchor can therefore never silently pick a new formula.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .graph import classify_cycles, direct_ref_targets, extract_edges
from .versions import (RuleVersionError, anchor_coord, coords_for,
                       overlapping_versions, resolve_rule)


def _expr_dependency_rules(version: dict) -> list[str]:
    deps: list[str] = []
    if version.get("expr"):
        deps.extend(direct_ref_targets(version["expr"]))
    for key in ("requires", "modifies"):
        deps.extend(version.get("params", {}).get(key, []))
    return sorted(set(deps))


def closure_fingerprint(anchor_patch: str, coord: int, roots: list[str],
                        frozen: dict) -> str:
    payload = {
        "anchor_patch": anchor_patch,
        "anchor_coord": coord,
        "roots": sorted(roots),
        "versions": {
            rule_id: {"version_id": v["id"], "version_label": v["version_label"],
                      "expr": v.get("expr"), "params": v.get("params", {})}
            for rule_id, v in sorted(frozen.items())
        },
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _query_cache_key(coord: int, roots: Iterable[str], mode: str) -> str:
    raw = json.dumps([coord, sorted(roots), mode], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def build_closure(conn, anchor_patch: str, roots: list[str], mode: str = "query",
                  day_slot: int | None = None, tolerate_absent_roots: bool = False,
                  persist: bool = True) -> dict:
    roots = sorted(set(roots))
    coord = _anchor_coord(conn, anchor_patch, day_slot)
    diagnostics: list[dict] = []
    frozen: dict[str, dict] = {}
    rule_kinds: dict[str, str] = {}

    def kind_of(rule_id: str) -> str:
        if rule_id not in rule_kinds:
            row = conn.execute("SELECT kind FROM rules WHERE id=?", (rule_id,)).fetchone()
            rule_kinds[rule_id] = row["kind"] if row else "unknown"
        return rule_kinds[rule_id]

    queue = list(roots)
    seen: set[str] = set()
    while queue:
        rule_id = queue.pop(0)
        if rule_id in seen:
            continue
        seen.add(rule_id)
        try:
            ver = resolve_rule(conn, rule_id, coord)
        except RuleVersionError as exc:
            is_root = rule_id in roots
            diag = {**exc.to_diag(), "root": is_root}
            if is_root and tolerate_absent_roots and exc.code == "NOT_INTRODUCED":
                diag["severity"] = "info"
                diagnostics.append(diag)
                continue
            diagnostics.append(diag)
            continue
        frozen[rule_id] = ver
        for dep in _expr_dependency_rules(ver):
            if dep not in seen:
                queue.append(dep)

    adj: dict[str, list[str]] = {r: [] for r in frozen}
    bounded_edges: set[tuple[str, str]] = set()
    for rule_id, ver in frozen.items():
        targets = set(_expr_dependency_rules(ver))
        rows = conn.execute(
            "SELECT target_rule_id FROM rule_dependencies WHERE source_version=?",
            (ver["id"],)).fetchall()
        targets.update(r["target_rule_id"] for r in rows)
        adj[rule_id] = sorted(t for t in targets if t in frozen)
        if ver.get("expr"):
            for e in extract_edges(ver["expr"]):
                if e["bounded"] and e["target"] == rule_id:
                    bounded_edges.add((rule_id, rule_id))

    diagnostics.extend(classify_cycles(adj, bounded_edges))
    closure_rules = set(frozen)
    diagnostics.extend(d for d in overlapping_versions(conn)
                       if d["rule_id"] in closure_rules)

    status = "ok" if not [d for d in diagnostics if d.get("severity") == "error"] else "error"

    frozen_slim = {
        rule_id: {
            "rule_id": rule_id,
            "kind": kind_of(rule_id),
            "version_id": v["id"],
            "version_label": v["version_label"],
            "patch_id": v["patch_id"],
            "status": v["status"],
            "body": v["body"],
            "expr": v.get("expr"),
            "params": v.get("params", {}),
            "effective_interval": [v["effective_start_patch"], v["effective_end_patch"]],
            "effective_interval_coord": [v["effective_start_seq"], v["effective_end_seq"]],
        }
        for rule_id, v in sorted(frozen.items())
    }
    package_id = closure_fingerprint(anchor_patch, coord, roots, frozen)
    by_kind: dict[str, int] = {}
    for rid in frozen_slim:
        by_kind[frozen_slim[rid]["kind"]] = by_kind.get(frozen_slim[rid]["kind"], 0) + 1
    stats = {
        "rule_count": len(frozen_slim),
        "root_count": len(roots),
        "edge_count": sum(len(v) for v in adj.values()),
        "bounded_fixed_points": len(bounded_edges),
        "by_kind": by_kind,
    }
    cache_key = _query_cache_key(coord, roots, mode)
    cache_hit = False
    if persist:
        existing = conn.execute("SELECT package_id FROM package_cache WHERE cache_key=?",
                                (cache_key,)).fetchone()
        cache_hit = existing is not None and existing["package_id"] == package_id
        conn.execute(
            "INSERT OR IGNORE INTO rule_packages "
            "(package_id, anchor_patch, anchor_coord, kind, root_rule_ids_json, "
            " rule_version_ids_json, expressions_json, status, diagnostics_json, stats_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (package_id, anchor_patch, coord, mode, json.dumps(roots),
             json.dumps(sorted(v["version_id"] for v in frozen_slim.values())),
             json.dumps(frozen_slim, ensure_ascii=False), status,
             json.dumps(diagnostics, ensure_ascii=False),
             json.dumps(stats, ensure_ascii=False)))
        conn.execute("INSERT OR REPLACE INTO package_cache(cache_key, package_id) VALUES (?,?)",
                     (cache_key, package_id))
        conn.commit()

    return {
        "package_id": package_id,
        "cache_key": cache_key,
        "cache_hit": cache_hit,
        "mode": mode,
        "anchor_patch": anchor_patch,
        "anchor_coord": coord,
        "day_slot": day_slot,
        "roots": roots,
        "status": status,
        "diagnostics": diagnostics,
        "frozen": frozen_slim,
        "stats": stats,
    }


def _anchor_coord(conn, anchor_patch: str, day_slot: int | None = None) -> int:
    seq = conn.execute("SELECT seq FROM patches WHERE id=?", (anchor_patch,)).fetchone()["seq"]
    return anchor_coord(seq, day_slot)


def precompiled_roots_at(conn, coord: int) -> list[str]:
    rows = conn.execute(
        "SELECT r.id AS rule_id, rv.effective_start_seq AS s, rv.effective_end_seq AS e "
        "FROM rules r JOIN rule_versions rv ON rv.rule_id=r.id "
        "WHERE r.kind='scoring' AND rv.status!='draft'"
    ).fetchall()
    roots: set[str] = set()
    for row in rows:
        e = row["e"] if row["e"] is not None else 10**9
        if row["s"] is not None and row["s"] <= coord < e:
            roots.add(row["rule_id"])
    return sorted(roots)


def precompile_patch(conn, anchor_patch: str, day_slot: int | None = None) -> dict:
    coord = _anchor_coord(conn, anchor_patch, day_slot)
    roots = precompiled_roots_at(conn, coord)
    return build_closure(conn, anchor_patch, roots, mode="precompiled", day_slot=day_slot)


def precompile_all(conn) -> list[dict]:
    return [precompile_patch(conn, r["id"])
            for r in conn.execute("SELECT id FROM patches ORDER BY seq").fetchall()]


def get_package(conn, package_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM rule_packages WHERE package_id=?",
                       (package_id,)).fetchone()
    if not row:
        return None
    return {
        "package_id": row["package_id"],
        "anchor_patch": row["anchor_patch"],
        "anchor_coord": row["anchor_coord"],
        "kind": row["kind"],
        "roots": json.loads(row["root_rule_ids_json"]),
        "status": row["status"],
        "diagnostics": json.loads(row["diagnostics_json"]),
        "frozen": json.loads(row["expressions_json"]),
        "stats": json.loads(row["stats_json"]),
    }


def list_packages(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT package_id, anchor_patch, anchor_coord, kind, status, stats_json, "
        "root_rule_ids_json FROM rule_packages ORDER BY rowid"
    ).fetchall()
    return [{
        "package_id": r["package_id"],
        "anchor_patch": r["anchor_patch"],
        "anchor_coord": r["anchor_coord"],
        "kind": r["kind"],
        "status": r["status"],
        "roots": json.loads(r["root_rule_ids_json"]),
        "stats": json.loads(r["stats_json"]),
    } for r in rows]


def errata_touching(conn, version_ids: list[int]) -> list[dict]:
    if not version_ids:
        return []
    marks = ",".join("?" * len(version_ids))
    rows = conn.execute(
        f"SELECT e.id, e.patch_id, e.note, b.rule_id AS rule_id, "
        f"       b.version_label AS bad_label, f.version_label AS fixed_label, "
        f"       e.bad_version, e.fixed_version "
        f"FROM errata e "
        f"JOIN rule_versions b ON b.id=e.bad_version "
        f"JOIN rule_versions f ON f.id=e.fixed_version "
        f"WHERE e.bad_version IN ({marks}) ORDER BY e.id",
        version_ids).fetchall()
    return [dict(r) for r in rows]
