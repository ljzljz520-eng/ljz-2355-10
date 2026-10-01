# -*- coding: utf-8 -*-
"""规则服务：对外提供检索、闭包解释、历史回放、缓存与报告。

缓存身份:
  key = 闭包内容哈希(sha256:...)。预编译包与按查询组合共用编译缓存；
  相同闭包 -> 同一身份 -> 同一编译产物；不同闭包即使规则文本只差一字也不复用。
"""
from .compiler import (resolve_closure, compile_closure, evaluate,
                       closure_hash, content_hash, normalize_rule)
from .indexing import search as idx_search, rebuild_index, index_status
from .graph import IllegalRecursion, DependencyError
from .evaluator import RuleEngineError
import json


class RuleService:
    def __init__(self, repo):
        self.repo = repo
        self._cache = {}   # hash -> compiled

    # ---------- 编译（带身份缓存） ----------
    def compile_for_package(self, package_id: str):
        snap = self.repo.get_package_snapshot(package_id)
        if snap is None:
            raise RuleEngineError("PACKAGE_NOT_FOUND",
                                  f"补丁包不存在或未预编译: {package_id}")
        rules = [normalize_rule(r) for r in snap["rules"]]
        return self._compile_snapshot(rules, snap["policies"], {
            "as_of": snap["released_at"],
            "rules": rules, "policies": snap["policies"]})

    def compile_for_snapshot_hash(self, hash_: str):
        snap = self.repo.get_snapshot(hash_)
        if snap is None:
            raise RuleEngineError("SNAPSHOT_NOT_FOUND",
                                  f"闭包快照不存在: {hash_}")
        rules = [normalize_rule(r) for r in snap["rules"]]
        return self._compile_snapshot(rules, snap["policies"], {
            "as_of": snap["as_of"], "rules": rules,
            "policies": snap["policies"]})

    def compile_as_of(self, as_of: str):
        rules, policies, blob, active = resolve_closure(self.repo, as_of)
        compiled = self._compile_snapshot(rules, policies, blob)
        return compiled, active

    def _compile_snapshot(self, rules, policies, blob):
        digest = closure_hash(blob)
        if digest in self._cache:
            return self._cache[digest]
        compiled = compile_closure(rules, policies, blob)
        # 身份自检：重算哈希必须一致，杜绝缓存错配
        if compiled["hash"] != digest:
            raise RuleEngineError(
                "CACHE_IDENTITY_MISMATCH",
                f"缓存身份不一致: 解析 {digest} != 编译 {compiled['hash']}")
        self._cache[digest] = compiled
        return compiled

    def cache_stats(self):
        return {"entries": len(self._cache),
                "hashes": sorted(self._cache.keys())}

    def verify_precompiled(self):
        """预编译身份校验：用 released_at 重新按查询组合一遍闭包，
        其闭包身份必须与发布时冻结的快照一致——同一时间点同一规则集+策略，
        无论“预编译包”还是“按查询组合”都必须得到同一身份（缓存可共享）。"""
        out = []
        for row in self.repo.conn.execute(
                "SELECT package_id,released_at FROM packages ORDER BY released_at"):
            pkg_id = row["package_id"]
            snap = self.repo.get_package_snapshot(pkg_id)
            live_rules, live_policies, blob, active = resolve_closure(
                self.repo, snap["released_at"])
            frozen_blob = {
                "as_of": snap["released_at"],
                "rules": [normalize_rule(r) for r in snap["rules"]],
                "policies": snap["policies"]}
            frozen = compile_closure(snap["rules"], snap["policies"], frozen_blob)
            live = compile_closure(live_rules, live_policies, blob)
            out.append({
                "package_id": pkg_id, "released_at": snap["released_at"],
                "snapshot_closure_hash": frozen["hash"],
                "query_closure_hash": live["hash"],
                "rule_count": len(snap["rules"]),
                "policy_count": len(snap["policies"]),
                "active_packages_at_release": active,
                "identity_ok": frozen["hash"] == live["hash"],
            })
        return out

    # ---------- 解释局面 ----------
    def explain_example(self, example_id: str):
        row = self.repo.conn.execute(
            "SELECT * FROM examples WHERE example_id=?", (example_id,)).fetchone()
        if not row:
            raise RuleEngineError("EXAMPLE_NOT_FOUND", f"比赛示例不存在: {example_id}")
        compiled = self.compile_for_snapshot_hash(row["snapshot_hash"])
        inputs = json.loads(row["inputs"])
        trace = evaluate(compiled, inputs)
        expected = json.loads(row["expected"])
        corrections = []
        for cr in self.repo.conn.execute(
                "SELECT * FROM example_corrections WHERE example_id=?",
                (example_id,)).fetchall():
            corrections.append({
                "errata_id": cr["errata_id"],
                "corrected_hash": cr["corrected_hash"],
                "recomputed": json.loads(cr["recomputed"]),
                "note": cr["note"]})
        return {
            "example_id": example_id, "title": row["title"],
            "played_at": row["played_at"], "inputs": inputs,
            "frozen_hash": row["snapshot_hash"],
            "expected_published": expected,
            "recomputed": trace["outputs"],
            "matches_published": trace["outputs"] == expected,
            "trace": trace,
            "corrections": corrections,
            "note": row["note"]}

    def explain_custom(self, inputs: dict, package_id: str = None,
                       as_of: str = None, snapshot_hash: str = None,
                       node_budget: int = 200_000):
        """规则服务解释指定补丁（或时间点/快照）下的示例局面。"""
        scope = None
        if snapshot_hash:
            compiled = self.compile_for_snapshot_hash(snapshot_hash)
            scope = {"kind": "snapshot", "ref": snapshot_hash}
        elif package_id:
            compiled = self.compile_for_package(package_id)
            scope = {"kind": "package", "ref": package_id}
        elif as_of:
            compiled, active = self.compile_as_of(as_of)
            scope = {"kind": "as_of", "ref": as_of, "active_packages": active}
        else:
            cur = self.repo.current_package()
            compiled = self.compile_for_package(cur)
            scope = {"kind": "current_package", "ref": cur}
        trace = evaluate(compiled, inputs, node_budget=node_budget)
        return {"scope": scope, "closure_hash": compiled["hash"],
                "inputs": inputs, "trace": trace,
                "rule_summary": self._rule_summary(compiled)}

    def _rule_summary(self, compiled):
        groups = []
        for g in compiled["plan"]:
            groups.append({
                "rules": g.rule_ids,
                "bounded": g.bounded,
                "max_iter": g.max_iter if g.bounded else None})
        return {
            "hash": compiled["hash"],
            "rule_count": len(compiled["rules"]),
            "aggregators": [a["id"] for a in compiled["aggregators"]],
            "policies": compiled["policies"],
            "plan": groups,
            "outputs": compiled["outputs"],
            "rules": [
                {"rule_id": rid, "title": d["title"], "category": d["category"],
                 "source": d["source"], "slot": d["slot"],
                 "conflict_group": d["conflict_group"],
                 "valid_from": d.get("valid_from"),
                 "package_id": d.get("package_id"),
                 "refs": d["refs"]}
                for rid, d in sorted(compiled["rules"].items())],
        }

    # ---------- 检索 ----------
    def search(self, q, as_of=None, doc_type=None, include_historical=False):
        return idx_search(self.repo, q, as_of=as_of, doc_type=doc_type,
                          include_historical=include_historical)

    def reindex(self, fail_after=None):
        return rebuild_index(self.repo, fail_after=fail_after)

    def index_status(self):
        return index_status(self.repo)

    # ---------- 报告 ----------
    def build_report(self, package_id=None, as_of=None, snapshot_hash=None,
                     inputs=None, node_budget=200_000):
        """导出报告：规则包摘要 + 局面输入 + 逐步分数来源 + 诊断。"""
        inputs = inputs or {}
        diag = None
        explanation = None
        try:
            explanation = self.explain_custom(
                inputs, package_id=package_id, as_of=as_of,
                snapshot_hash=snapshot_hash, node_budget=node_budget)
        except RuleEngineError as e:
            diag = {"code": e.code, "message": str(e), "detail": e.detail}
        except (IllegalRecursion, DependencyError) as e:
            diag = {"code": type(e).__name__, "message": str(e)}

        report = {
            "generated_for": {"package_id": package_id, "as_of": as_of,
                              "snapshot_hash": snapshot_hash},
            "situation_input": inputs,
            "diagnostic": diag,
        }
        if explanation:
            report["rule_package_summary"] = explanation["rule_summary"]
            report["closure_hash"] = explanation["closure_hash"]
            report["score"] = explanation["trace"]["outputs"]
            report["score_steps"] = explanation["trace"]["steps"]
            report["node_count"] = explanation["trace"]["node_count"]
            report["node_budget"] = explanation["trace"]["node_budget"]
        else:
            # 即便无法求解，也附上闭包规则包摘要（若能解析）
            try:
                if snapshot_hash:
                    comp = self.compile_for_snapshot_hash(snapshot_hash)
                elif package_id:
                    comp = self.compile_for_package(package_id)
                elif as_of:
                    comp, _ = self.compile_as_of(as_of)
                else:
                    comp = self.compile_for_package(self.repo.current_package())
                report["rule_package_summary"] = self._rule_summary(comp)
                report["closure_hash"] = comp["hash"]
            except Exception as e2:  # 编译期非法递归等
                report["package_compile_error"] = str(e2)
        return report
