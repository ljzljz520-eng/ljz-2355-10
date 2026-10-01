"""Rule version resolution over effective intervals.

Intervals are stored as integer coordinates on the patch timeline:
``patch_seq * 10 + day_slot``. A normal patch anchors at ``seq*10``; two same-day
revisions occupy ``seq*10+1`` (morning) and ``seq*10+2`` (evening). This makes
"same-day, one after another" an ordinary interval boundary instead of an
ambiguous tie.
"""
from __future__ import annotations

from .db import row_to_version


def patches_ordered(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM patches ORDER BY seq").fetchall()]


def patch_seq_map(conn) -> tuple[dict[str, int], list[dict]]:
    pats = patches_ordered(conn)
    return {p["id"]: p["seq"] for p in pats}, pats


def anchor_coord(seq: int, day_slot: int | None = None) -> int:
    return seq * 10 + (day_slot or 0)


def coords_for(conn, patch_id: str, day_slot: int | None = None) -> int:
    seq = dict((r["id"], r["seq"]) for r in conn.execute("SELECT id, seq FROM patches"))[patch_id]
    return anchor_coord(seq, day_slot)


def resolve_rule(conn, rule_id: str, coord: int) -> dict:
    """Return the unique version effective at timeline coordinate ``coord``."""
    rows = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_id=? AND status!='draft'",
        (rule_id,),
    ).fetchall()
    effective = [
        row_to_version(r) for r in rows
        if (r["effective_start_seq"] is not None
            and r["effective_start_seq"] <= coord
            and (r["effective_end_seq"] is None or coord < r["effective_end_seq"]))
    ]
    if len(effective) > 1:
        ids = [v["id"] for v in effective]
        raise RuleVersionError(
            "OVERLAPPING_INTERVAL",
            f"规则 {rule_id} 在时间坐标 {coord} 有 {len(effective)} 个同时生效的版本 "
            f"(version ids {ids})：生效区间重叠，无法唯一解析。",
            rule_id=rule_id, version_ids=ids, coord=coord,
        )
    if effective:
        return effective[0]
    starts = sorted((r["effective_start_seq"], r) for r in rows
                    if r["effective_start_seq"] is not None)
    if starts and starts[0][0] > coord:
        raise RuleVersionError(
            "NOT_INTRODUCED",
            f"规则 {rule_id} 直到 {starts[0][1]['patch_id']} 才引入，"
            f"时间坐标 {coord} 时尚不存在。",
            rule_id=rule_id, introduced_at=starts[0][1]["patch_id"], coord=coord,
        )
    raise RuleVersionError(
        "RETIRED",
        f"规则 {rule_id} 在时间坐标 {coord} 没有生效版本（已下线或存在区间缺口）。",
        rule_id=rule_id, coord=coord,
    )


class RuleVersionError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def to_diag(self) -> dict:
        d = {"code": self.code, "severity": "error", "message": self.message}
        d.update(self.extra)
        return d


def overlapping_versions(conn) -> list[dict]:
    """Audit non-draft versions for pairwise interval overlaps (data integrity)."""
    rows = conn.execute(
        "SELECT * FROM rule_versions WHERE status NOT IN ('draft') "
        "AND effective_start_patch IS NOT NULL ORDER BY rule_id"
    ).fetchall()
    by_rule: dict[str, list[dict]] = {}
    for r in rows:
        by_rule.setdefault(r["rule_id"], []).append(row_to_version(r))
    overlaps: list[dict] = []
    for rule_id, vs in by_rule.items():
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                a, b = vs[i], vs[j]
                aend = a["effective_end_seq"] if a["effective_end_seq"] is not None else 10**9
                bend = b["effective_end_seq"] if b["effective_end_seq"] is not None else 10**9
                # closed-half intervals [start,end): adjacent versions may share
                # the switch coordinate without overlapping
                if a["effective_start_seq"] < bend and b["effective_start_seq"] < aend:
                    overlaps.append({
                        "code": "OVERLAPPING_INTERVAL",
                        "severity": "error",
                        "rule_id": rule_id,
                        "versions": [
                            {"version_id": a["id"], "label": a["version_label"],
                             "interval_coord": [a["effective_start_seq"], a["effective_end_seq"]],
                             "interval": [a["effective_start_patch"], a["effective_end_patch"]]},
                            {"version_id": b["id"], "label": b["version_label"],
                             "interval_coord": [b["effective_start_seq"], b["effective_end_seq"]],
                             "interval": [b["effective_start_patch"], b["effective_end_patch"]]},
                        ],
                        "message": f"规则 {rule_id} 的版本 {a['version_label']} 与 "
                                   f"{b['version_label']} 生效区间重叠。",
                    })
    return overlaps
