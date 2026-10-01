# -*- coding: utf-8 -*-
"""规则闭包解析与编译。

闭包(closure): 在某个时间点 as_of，对每个规则键(rule_key)选出恰好一条
生效版本(valid_from <= as_of < valid_to)，连同该时间点生效的冲突策略，
构成一个不可变的“规则闭包”。闭包内容做规范化哈希 => 缓存身份。

* 预编译补丁规则包: 发布时按该包 released_at 解析闭包并编译，产物按哈希复用；
* 按查询组合: 传入任意 as_of 即时解析+编译；
两者走同一 compile_closure，保证等价性；哈希不同即不同身份，缓存不串。
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from .parser import parse, collect_refs, collect_inputs
from .graph import build_plan, DependencyError, IllegalRecursion
from .evaluator import Evaluator, RuleEngineError
from . import ast_nodes as ast


class ClosureError(RuleEngineError):
    pass


@dataclass
class ResolvedRule:
    key: str
    rule_id: str
    package_id: str
    title: str
    category: str
    source: str
    slot: Optional[str]
    conflict_group: Optional[str]
    applicable_source: Optional[str]
    valid_from: str
    valid_to: Optional[str]
    fixpoint: Optional[dict]
    refs: List[str]


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def closure_hash(canonical_blob: dict) -> str:
    # 完整 64 位摘要作为缓存身份，避免截断碰撞导致不同闭包串用缓存
    return "sha256:" + hashlib.sha256(
        _canonical(canonical_blob).encode("utf-8")).hexdigest()


def content_hash(rules: list, policies: dict) -> str:
    """规则内容身份：与解析方式(package/as_of)无关。
    预编译包与按查询组合只要规则版本集合与策略一致，内容身份就相同；
    完整闭包身份还包含 scope/active_packages，用于回放防串包。"""
    payload = {"rules": sorted(
        [{k: r.get(k) for k in RULE_FIELDS} for r in rules],
        key=lambda x: x["rule_id"]),
        "policies": policies}
    return "content:" + hashlib.sha256(
        _canonical(payload).encode("utf-8")).hexdigest()


RULE_FIELDS = ("rule_id", "key", "package_id", "title", "category", "summary",
               "source", "slot", "conflict_group", "applicable_when",
               "fixpoint", "priority", "valid_from", "valid_to")


def normalize_rule(r: dict) -> dict:
    """规范化规则行：决定闭包身份的唯一字段集合（排序后参与哈希）。"""
    r = dict(r)
    r["key"] = r.get("key") or r.get("rule_key")
    out = {k: r.get(k) for k in RULE_FIELDS}
    return out


def resolve_closure(repo, as_of: str, package_id: Optional[str] = None):
    """从关系库解析时间点 as_of 的规则闭包。

    package_id 给定时，直接取该补丁包快照中冻结的规则集（历史回放路径）；
    否则按生效区间动态组合（预编译/查询路径）。
    返回 (resolved_rules, policies, canonical_blob, active_packages)。
    """
    if package_id is not None:
        snap = repo.get_package_snapshot(package_id)
        if snap is None:
            raise ClosureError("PACKAGE_NOT_FOUND", f"补丁包不存在: {package_id}")
        rules = [normalize_rule(r) for r in snap["rules"]]
        policies = snap["policies"]
        # 闭包身份只取决于时间点 + 规则集 + 策略集；
        # “以补丁包发布快照取闭包”还是“按时间点动态组合”只是解析路径(scope)。
        blob = {
            "as_of": snap["released_at"], "rules": rules, "policies": policies}
        return rules, policies, blob, [package_id]

    active = repo.active_packages(as_of)
    if not active:
        raise ClosureError(
            "NO_ACTIVE_PACKAGE", f"时间点 {as_of} 没有任何已生效补丁包")

    rows = repo.rule_versions_as_of(as_of)
    for r in rows:
        r["key"] = r.get("key") or r.get("rule_key")
    by_key: Dict[str, list] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)

    chosen: Dict[str, dict] = {}
    for key, cands in by_key.items():
        if len(cands) > 1:
            # 生效范围重叠：按 priority 裁决；priority 相同则无法唯一求解
            top = max(c["priority"] for c in cands)
            winners = [c for c in cands if c["priority"] == top]
            if len(winners) > 1:
                labels = [f"{c['rule_id']}@{c['valid_from']}"
                          f"(pkg {c['package_id']}, priority {c['priority']})"
                          for c in cands]
                raise ClosureError(
                    "OVERLAP_AMBIGUOUS",
                    f"规则 {key} 在 {as_of} 生效区间重叠且优先级相同，无法唯一求解: {labels}",
                    {"rule_key": key, "candidates": labels})
            chosen[key] = winners[0]
        else:
            chosen[key] = cands[0]

    policies = repo.policies_as_of(as_of)
    rules = [normalize_rule(r) for r in chosen.values()]
    rules.sort(key=lambda r: r["rule_id"])
    blob = {
        "as_of": as_of,
        "rules": rules,
        "policies": policies,
    }
    return rules, policies, blob, active


def compile_closure(resolved_rules: List[dict], policies: dict,
                    canonical_blob: dict):
    """解析规则源码、建依赖图、生成求值计划与槽位汇总器，返回编译产物。"""
    parsed: Dict[str, dict] = {}
    keys_by_id = {}
    # 展开策略载荷：repo 快照中形如 {"mode":..., "payload": {...}}
    norm_policies = {}
    for group, pol in (policies or {}).items():
        if "payload" in pol and ("winner" not in pol and "order" not in pol):
            norm_policies[group] = {**pol["payload"], "mode": pol["mode"]}
        else:
            norm_policies[group] = dict(pol)
    policies = norm_policies
    for r in resolved_rules:
        astree = parse(r["source"])
        refs = collect_refs(astree)
        applicable_ast = None
        if r.get("applicable_when"):
            applicable_ast = parse(r["applicable_when"])
            bad = collect_refs(applicable_ast)
            if bad:
                raise DependencyError(
                    f"规则 {r['rule_id']} 的生效条件不得引用其他规则（发现 {bad}），"
                    "条件只能依赖局面输入 input(...)")
        fp = r.get("fixpoint")
        if isinstance(fp, str):
            fp = json.loads(fp)
        parsed[r["rule_id"]] = {
            "ast": astree,
            "refs": refs,
            "meta": {"fixpoint": fp},
            "title": r.get("title", r["rule_id"]),
            "source": r["source"],
            "slot": r.get("slot"),
            "conflict_group": r.get("conflict_group"),
            "applicable_ast": applicable_ast,
            "category": r.get("category", ""),
            "key": r.get("key"),
            "package_id": r.get("package_id"),
            "valid_from": r.get("valid_from"),
        }
        keys_by_id[r["rule_id"]] = r.get("key")

    # 槽位汇总器：每个 slot 一个合成规则，贡献者按 id 排序
    slot_members: Dict[str, List[str]] = {}
    for rid, d in parsed.items():
        if d["slot"]:
            slot_members.setdefault(d["slot"], []).append(rid)

    aggregators = []
    agg_ids = set()
    for slot in sorted(slot_members):
        contributors = sorted(slot_members[slot])
        agg_id = f"__slot_{slot}"
        agg_ids.add(agg_id)
        aggregators.append({
            "id": agg_id, "title": f"槽位汇总：{slot}",
            "slot": slot, "agg": "slot_sum",
            "contributors": contributors,
        })

    # 引用重写表: 槽位名 -> 聚合器；规则逻辑键(key) -> 闭包内实际版本 id。
    # 这样旧公式引用 ref('kill') / ref('combo_ping') 时，
    # 总会拿到“同一闭包内”的配套版本，杜绝旧角色数值配新公式。
    key_to_id = {d["key"]: rid for rid, d in parsed.items() if d.get("key")}
    slot_to_agg = {a["slot"]: a["id"] for a in aggregators}
    ref_map = {**key_to_id, **slot_to_agg}
    for rid, d in parsed.items():
        rewritten = []
        for ref in d["refs"]:
            if ref in parsed:           # 已写死为某版本 id
                rewritten.append(ref)
            elif ref in ref_map:
                rewritten.append(ref_map[ref])
            else:
                raise DependencyError(
                    f"规则 {rid} 引用了闭包中不存在的规则键/槽位: {ref}")
        d["refs"] = rewritten

    # 汇总器依赖其贡献者：建最终计划（贡献者在前，聚合器在拓扑最后）
    final_plan = build_plan({
        **{rid: {"refs": d["refs"], "meta": d["meta"]}
           for rid, d in parsed.items()},
        **{a["id"]: {"refs": a["contributors"], "meta": {}}
           for a in aggregators},
    })

    # 输出：没有被任何规则引用的“根”规则 + 所有槽位聚合器
    referenced = set()
    for d in parsed.values():
        for x in d["refs"]:
            referenced.add(x)
    outputs = sorted(
        agg_ids | {rid for rid in parsed if rid not in referenced})

    digest = closure_hash(canonical_blob)
    chash = content_hash(resolved_rules, policies)
    # 为每个规则建立 AST 内 ref 逻辑名 -> 实际目标 id 的解析表（按 collect_refs 顺序）
    for rid, d in parsed.items():
        original = collect_refs(d["ast"])
        d["ref_resolve"] = dict(zip(original, d["refs"]))
    return {
        "hash": digest,
        "content_hash": chash,
        "rules": parsed,
        "plan": final_plan,
        "aggregators": aggregators,
        "policies": policies or {},
        "outputs": outputs,
        "canonical": canonical_blob,
    }


def evaluate(compiled, inputs: dict, node_budget: int = 200_000):
    ev = Evaluator(compiled, inputs, node_budget=node_budget)
    result, steps = ev.run()
    trace = {
        "hash": compiled["hash"],
        "outputs": _jsonable(result),
        "steps": [_step_json(s) for s in steps],
        "node_count": ev.node_count,
        "node_budget": node_budget,
    }
    return trace


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return round(v, 10)
    return v


def _step_json(s):
    return _jsonable({
        "rule_id": s.rule_id, "title": s.title, "kind": s.kind,
        "formula": s.formula, "value": s.value, "applicable": s.applicable,
        "inputs": s.inputs, "refs": s.refs,
        "iterations": s.iterations, "converged": s.converged,
        "rounds": s.rounds, "note": s.note,
    })
