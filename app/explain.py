"""Rules service: explain concrete situations under a frozen rule closure.

Historical replay always goes through a *frozen package*: the example points at
a package_id whose contents were validated and content-hashed at publication.
The service never mixes an old character number with a new scoring formula.

Errata never rewrite a published example: the frozen score is returned as-is,
and the correction relation is surfaced separately (with an optional
"what would the corrected rules produce" advisory that is clearly labelled).
"""
from __future__ import annotations

import json

from .evaluator import evaluate
from .packages import (build_closure, errata_touching, get_package,
                       precompiled_roots_at)
from .versions import anchor_coord, coords_for

KIND_LABEL = {
    "scoring": "计分", "character": "角色", "map": "地图",
    "item": "道具", "mechanic": "机制",
}


def scoring_roots_for_situation(conn, situation: dict, coord: int) -> list[str]:
    roots = precompiled_roots_at(conn, coord)
    extra = situation.get("_extra_roots") or []
    return sorted(set(roots) | set(extra))


def explain_situation(conn, situation: dict, anchor_patch: str,
                      day_slot: int | None = None, roots: list[str] | None = None,
                      budget: int = 5_000, persist: bool = True) -> dict:
    coord = coords_for(conn, anchor_patch, day_slot)
    chosen_roots = sorted(set(roots)) if roots else scoring_roots_for_situation(
        conn, situation, coord)
    package = build_closure(conn, anchor_patch, chosen_roots, mode="query",
                            day_slot=day_slot, persist=persist)
    result = evaluate(package, situation, budget=budget)

    version_ids = sorted(v["version_id"] for v in package["frozen"].values())
    errata = errata_touching(conn, version_ids)

    if persist and result.get("score") is not None:
        conn.execute(
            "INSERT INTO explanations(package_id, situation_json, score, trace_json,"
            " diagnostics_json) VALUES (?,?,?,?,?)",
            (package["package_id"], json.dumps(situation, ensure_ascii=False),
             result["score"], json.dumps(result.get("contributions", []),
                                         ensure_ascii=False),
             json.dumps(result.get("diagnostics", []), ensure_ascii=False)))
        conn.commit()

    return {
        "anchor_patch": anchor_patch,
        "anchor_coord": coord,
        "day_slot": day_slot,
        "package_id": package["package_id"],
        "package_status": package["status"],
        "package_mode": package["mode"],
        "cache_hit": package["cache_hit"],
        "roots": chosen_roots,
        "score": result.get("score"),
        "contributions": result.get("contributions", []),
        "suppressed": result.get("suppressed", []),
        "steps_used": result.get("steps_used"),
        "budget": result.get("budget"),
        "diagnostics": result.get("diagnostics", []),
        "package_diagnostics": package["diagnostics"],
        "errata_notices": errata,
        "frozen_summary": package["stats"],
        "step_explanation": _step_explanation(package, result),
    }


def _step_explanation(package: dict, result: dict) -> list[dict]:
    """Human/UI friendly ordered breakdown of where the score came from."""
    steps = []
    running = 0
    for c in result.get("contributions", []):
        rid = c["rule_id"]
        ver = package["frozen"].get(rid, {})
        if not c.get("fired"):
            steps.append({
                "rule_id": rid, "kind": ver.get("kind"),
                "version": c.get("version_label"),
                "action": "skip", "delta": 0,
                "text": f"【{KIND_LABEL.get(ver.get('kind'), ver.get('kind'))}·{rid} "
                        f"{c.get('version_label')}】未触发：{c.get('reason')}",
            })
            continue
        delta = c["value"]
        running += delta
        extra = ""
        if (c.get("detail") or {}).get("iterations"):
            extra = f"（有界定点 {c['detail']['iterations']} 轮收敛）"
        steps.append({
            "rule_id": rid, "kind": ver.get("kind"),
            "version": c.get("version_label"),
            "action": "add", "delta": delta, "running_total": running,
            "text": f"【计分·{rid} {c.get('version_label')}】{c.get('reason')}，"
                    f"贡献 {delta:+d} 分{extra}，累计 {running} 分",
        })
    for d in result.get("diagnostics", []):
        if d.get("severity") == "warning":
            steps.append({
                "rule_id": d.get("rule_id"), "action": "conflict",
                "delta": 0, "text": d.get("message"),
            })
    if result.get("score") is not None:
        steps.append({
            "action": "total", "delta": 0, "running_total": result["score"],
            "text": f"最终分数 = {result['score']} 分"
                    f"（求值 {result.get('steps_used')} 步 / 预算 {result.get('budget')}）",
        })
    else:
        steps.append({
            "action": "failed",
            "text": "无法给出分数：存在未解决的规则诊断，详见 diagnostics。",
        })
    return steps


def list_examples(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM examples ORDER BY published_patch, id").fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "title": r["title"],
            "anchor_patch": r["anchor_patch"],
            "published_patch": r["published_patch"],
            "expected_score": r["expected_score"],
            "frozen_package_id": r["frozen_package_id"],
            "situation": json.loads(r["situation_json"]),
        })
    return out


def freeze_example(conn, example_id: str, budget: int = 5_000) -> dict:
    """Build (once) the frozen package for a published example and replay it."""
    row = conn.execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()
    if not row:
        return {"error": f"未知示例 {example_id}"}
    situation = json.loads(row["situation_json"])
    explanation = explain_situation(
        conn, situation, row["anchor_patch"],
        budget=min(budget, 10_000), persist=True)
    # freeze the closure even when its status is error: the exact version set
    # AND the compile-time diagnosis (e.g. ILLEGAL_RECURSION) are part of history
    if row["frozen_package_id"] is None and explanation.get("package_id"):
        conn.execute("UPDATE examples SET frozen_package_id=? WHERE id=?",
                     (explanation["package_id"], example_id))
        conn.commit()
    return {
        "example_id": example_id,
        "title": row["title"],
        "published_patch": row["published_patch"],
        "published_on": row["published_on"],
        "expected_score": row["expected_score"],
        "explanation": explanation,
        "replay_note": "已发布示例的局面与冻结闭包永不重写；勘误仅以修正关系关联。",
    }


def replay_frozen(conn, example_id: str) -> dict:
    """Replay using the stored frozen package (no recomputation of closure)."""
    row = conn.execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()
    if not row:
        return {"error": f"未知示例 {example_id}"}
    situation = json.loads(row["situation_json"])
    if not row["frozen_package_id"]:
        return freeze_example(conn, example_id)
    package = get_package(conn, row["frozen_package_id"])
    if package is None:
        return {"error": "冻结包缺失，历史回放被拒绝，拒绝回退到当前规则。"}
    result = evaluate(package, situation, budget=10_000)
    version_ids = sorted(v["version_id"] for v in package["frozen"].values())
    return {
        "example_id": example_id,
        "title": row["title"],
        "expected_score": row["expected_score"],
        "package_id": package["package_id"],
        "anchor_patch": package["anchor_patch"],
        "score": result.get("score"),
        "step_explanation": _step_explanation(package, result),
        "contributions": result.get("contributions", []),
        "diagnostics": result.get("diagnostics", []),
        "errata_notices": errata_touching(conn, version_ids),
        "replay_note": "使用发布时冻结的完整规则闭包回放，未引入任何后续补丁公式。",
    }


def corrected_replay_advisory(conn, example_id: str, anchor_patch: str = None) -> dict:
    """Optional: what the CURRENT/corrected rules would score (advisory only)."""
    row = conn.execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()
    if not row:
        return {"error": f"未知示例 {example_id}"}
    situation = {k: v for k, v in json.loads(row["situation_json"]).items()
                 if not k.startswith("_")}
    anchor = anchor_patch or conn.execute(
        "SELECT id FROM patches ORDER BY seq DESC LIMIT 1").fetchone()["id"]
    adv = explain_situation(conn, situation, anchor, persist=False)
    return {
        "advisory": True,
        "anchor_patch": anchor,
        "score": adv["score"],
        "note": "这只是勘误/现行规则下的参考分，不会改动已发布示例的冻结分。",
        "step_explanation": adv["step_explanation"],
    }


def export_report(conn, package_id: str, situation: dict | None = None,
                  example_id: str | None = None) -> dict:
    package = get_package(conn, package_id)
    if package is None:
        return {"error": f"未知规则包 {package_id}"}
    version_ids = sorted(v["version_id"] for v in package["frozen"].values())
    entries = []
    for rid, v in package["frozen"].items():
        entries.append({
            "rule_id": rid,
            "kind": v["kind"],
            "version_label": v["version_label"],
            "introduced_by_patch": v["patch_id"],
            "status": v["status"],
            "effective_interval": v["effective_interval"],
            "effective_interval_coord": v["effective_interval_coord"],
            "params": v["params"],
        })
    report = {
        "title": "规则包解释报告 / Rule Package Report",
        "package_id": package_id,
        "anchor": {"patch": package["anchor_patch"], "coord": package["anchor_coord"]},
        "kind": package["kind"],
        "roots": package["roots"],
        "package_summary": {
            **package["stats"],
            "status": package["status"],
            "rule_entries": len(entries),
        },
        "rule_package_digest": [
            {"kind": k, "count": n}
            for k, n in sorted(package["stats"].get("by_kind", {}).items())
        ],
        "frozen_rules": entries,
        "dependency_diagnostics": package["diagnostics"],
        "errata": errata_touching(conn, version_ids),
        "situation_input": situation,
        "example_id": example_id,
    }
    if situation is not None:
        result = evaluate(package, situation, budget=10_000)
        report["score"] = result.get("score")
        report["score_breakdown"] = _step_explanation(package, result)
        report["contributions"] = result.get("contributions", [])
        report["evaluation"] = {
            "steps_used": result.get("steps_used"),
            "budget": result.get("budget"),
            "diagnostics": result.get("diagnostics", []),
        }
    return report
