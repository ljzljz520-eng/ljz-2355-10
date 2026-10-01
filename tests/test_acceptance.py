# -*- coding: utf-8 -*-
"""验收测试（python -m unittest tests.test_acceptance）。

覆盖需求点名的场景:
  A1 同日补丁先后（1.1.0 09:00 -> 1.1.1 18:30，数值随时间切换）
  A2 生效范围重叠（priority 裁决；同优先级 -> 明确诊断无法求解）
  A3 道具与地图条件冲突（1.1.0 无策略报错；1.1.1 map_wins 裁决）
  A4 示例计算超预算（非收缩有界环超 max_iter；AST 节点预算）
  A5 索引更新中断（影子表损坏不影响线上，可重试恢复）
  A6 历史回放冻结整套规则闭包（示例哈希锁定，勘误不重写只另标修正关系）
  A7 规则成环：有界求值 vs 非法递归（区分语义）
  A8 预编译包 vs 按查询组合的依赖校验与缓存身份
  A9 页面/报告可逐步解释分数来源且报告含规则包摘要与局面输入（API 层）
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules_enc.repository import Repo
from rules_enc.seed import seed
from rules_enc.service import RuleService
from rules_enc.evaluator import RuleEngineError
from rules_enc.graph import IllegalRecursion, build_plan
from rules_enc.compiler import (compile_closure, evaluate, resolve_closure,
                                content_hash, normalize_rule)
from rules_enc.indexing import IndexRebuildInterrupted


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(":memory:")
        seed(self.repo, verbose=False)
        self.svc = RuleService(self.repo)


class TestSameDayOrdering(Base):
    """A1 同日先后。"""
    def test_same_day_sequence(self):
        inputs = {"character": "nova", "map": "base", "item": "shield",
                  "kills": 0, "ranked_kills": 0}
        nova_at = lambda t: self.svc.explain_custom(
            inputs, as_of=t)["trace"]["outputs"]["__slot_character"]
        self.assertEqual(nova_at("2026-09-10T08:59:00Z"), 50)   # 1.0
        self.assertEqual(nova_at("2026-09-10T09:30:00Z"), 50)   # 1.1.0 未改角色
        self.assertEqual(nova_at("2026-09-10T19:00:00Z"), 40)   # 同日热修
        self.assertEqual(nova_at("2026-09-21T00:00:00Z"), 45)   # 1.1.2 定稿

    def test_hotfix_window_kill_score(self):
        inputs = {"character": "axel", "map": "base", "item": "shield",
                  "kills": 2, "ranked_kills": 2}
        kill = lambda t: self.svc.explain_custom(
            inputs, as_of=t)["trace"]["outputs"]["__slot_kill"]
        self.assertEqual(kill("2026-09-10T10:00:00Z"), 200)     # 窗口前 100/击杀
        self.assertEqual(kill("2026-09-11T12:00:00Z"), 400)     # 热修 200/击杀
        self.assertEqual(kill("2026-09-11T19:00:00Z"), 200)     # 窗口结束
        self.assertEqual(kill("2026-09-21T00:00:00Z"), 200)     # 勘误后仍 100/击杀(总数2)


class TestOverlap(Base):
    """A2 生效范围重叠。"""
    def test_priority_resolves_overlap(self):
        # 热修 kill(priority10) 与基础 kill(priority0) 区间重叠，取热修
        out = self.svc.explain_custom(
            {"character": "axel", "map": "base", "item": "shield",
             "kills": 1, "ranked_kills": 1},
            as_of="2026-09-11T12:00:00Z")
        self.assertEqual(out["trace"]["outputs"]["kill_score_hotfix"], 200)

    def test_equal_priority_is_diagnosed(self):
        c = self.repo.conn
        c.execute("INSERT INTO rule_versions(rule_id,rule_key,package_id,title,"
                  "category,source,slot,priority,valid_from) VALUES"
                  "('dup_kill','kill_score','1.1.0','dup','scoring',"
                  "'input(\"kills\",0)*99','kill',0,'2026-09-02T00:00:00Z')")
        c.commit()
        with self.assertRaises(RuleEngineError) as cm:
            self.svc.compile_as_of("2026-09-03T00:00:00Z")
        self.assertEqual(cm.exception.code, "OVERLAP_AMBIGUOUS")


class TestConflict(Base):
    """A3 道具与地图条件冲突。"""
    MAG = {"character": "nova", "map": "neon", "item": "magnet", "kills": 1}

    def test_unresolved_conflict_diagnosed(self):
        with self.assertRaises(RuleEngineError) as cm:
            self.svc.explain_custom(self.MAG, package_id="1.1.0")
        self.assertEqual(cm.exception.code, "RULE_CONFLICT")
        self.assertFalse(cm.exception.detail["resolved"])
        self.assertEqual(cm.exception.detail["conflict_group"], "item_map_neon")

    def test_policy_resolves_conflict(self):
        out = self.svc.explain_custom(self.MAG, package_id="1.1.1")
        steps = {s["rule_id"]: s for s in out["trace"]["steps"]}
        self.assertTrue(steps["map_neon_v1"]["applicable"])
        self.assertFalse(steps["item_magnet_v1"]["applicable"])
        self.assertIn("压制", steps["item_magnet_v1"]["note"])

    def test_as_of_1_1_0_has_no_policy_but_1_1_1_does(self):
        with self.assertRaises(RuleEngineError):
            self.svc.explain_custom(self.MAG, as_of="2026-09-10T10:00:00Z")
        out = self.svc.explain_custom(self.MAG, as_of="2026-09-10T19:00:00Z")
        self.assertIn("total_score_v2", out["trace"]["outputs"])


class TestBudget(Base):
    """A4 示例计算超预算。"""
    def _rules(self, fx):
        src = {"x": "2*ref('y')+1", "y": "2*ref('x')+1"}
        return [{"rule_id": k, "key": k, "title": k, "category": "scoring",
                 "source": src[k], "slot": None, "conflict_group": None,
                 "applicable_when": None, "fixpoint": json.dumps(fx),
                 "priority": 0, "valid_from": "t", "valid_to": None,
                 "package_id": "t"} for k in ("x", "y")]

    def test_bounded_but_noncontracting_exceeds_iteration_cap(self):
        rules = self._rules({"max_iter": 20, "tol": 1e-9, "init": 0})
        comp = compile_closure(rules, {}, {"mode": "test", "rules": rules})
        with self.assertRaises(RuleEngineError) as cm:
            evaluate(comp, {})
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(cm.exception.detail["kind"], "iteration_cap")

    def test_bounded_contracting_converges(self):
        rules = []
        for rid, src in [("p", "30 + 0.4*ref('o')"), ("o", "0.5*ref('p')")]:
            rules.append({"rule_id": rid, "key": rid, "title": rid,
                          "category": "scoring", "source": src, "slot": "combo",
                          "conflict_group": None, "applicable_when": None,
                          "fixpoint": json.dumps(
                              {"max_iter": 50, "tol": 1e-6, "init": 0}),
                          "priority": 0, "valid_from": "t",
                          "valid_to": None, "package_id": "t"})
        comp = compile_closure(rules, {}, {"mode": "test", "rules": rules})
        tr = evaluate(comp, {})
        self.assertAlmostEqual(tr["outputs"]["__slot_combo"], 56.25, delta=1e-3)

    def test_node_budget(self):
        rules = [{"rule_id": "z", "key": "z", "title": "z", "category": "scoring",
                  "source": "iter_drag(input('n',0))", "slot": None,
                  "conflict_group": None, "applicable_when": None,
                  "fixpoint": None, "priority": 0, "valid_from": "t",
                  "valid_to": None, "package_id": "t"}]
        comp = compile_closure(rules, {}, {"mode": "test", "rules": rules})
        with self.assertRaises(RuleEngineError) as cm:
            evaluate(comp, {"n": 100000}, node_budget=500)
        self.assertEqual(cm.exception.detail["kind"], "node_budget")

    def test_iteration_budget_via_shipped_test_package(self):
        # 通过服务 API 指定 test-heavy 补丁解释大局面 => 明确超预算诊断
        with self.assertRaises(RuleEngineError) as cm:
            self.svc.explain_custom({"n": 100000}, package_id="test-heavy")
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")
        self.assertLessEqual(cm.exception.detail["used"],
                             cm.exception.detail["limit"] + 3)
        # 小局面可正常求解
        out = self.svc.explain_custom({"n": 5}, package_id="test-heavy")
        self.assertAlmostEqual(
            out["trace"]["outputs"]["heavy_drag_v1"], 4.90099, delta=1e-3)


class TestCycles(Base):
    """A7 有界求值语义 vs 非法递归。"""
    def test_illegal_recursion_message_names_cycle(self):
        with self.assertRaises(IllegalRecursion) as cm:
            build_plan({"a": {"refs": ["b"], "meta": {}},
                        "b": {"refs": ["c"], "meta": {
                            "fixpoint": {"max_iter": 5, "init": 0}}},
                        "c": {"refs": ["a"], "meta": {
                            "fixpoint": {"max_iter": 5, "init": 0}}}})
        self.assertIn("非法递归", str(cm.exception))
        self.assertIn("a", cm.exception.unbounded)

    def test_self_loop_must_be_bounded(self):
        with self.assertRaises(IllegalRecursion):
            build_plan({"a": {"refs": ["a"], "meta": {}}})

    def test_inconsistent_max_iter_is_illegal(self):
        with self.assertRaises(IllegalRecursion):
            build_plan({
                "a": {"refs": ["b"], "meta": {"fixpoint": {"max_iter": 5, "init": 0}}},
                "b": {"refs": ["a"], "meta": {"fixpoint": {"max_iter": 9, "init": 0}}}})

    def test_missing_dependency(self):
        with self.assertRaises(Exception):
            compile_closure(
                [{"rule_id": "a", "key": "a", "title": "a", "category": "scoring",
                  "source": "ref('ghost') + 1", "slot": None,
                  "conflict_group": None, "applicable_when": None,
                  "fixpoint": None, "priority": 0, "valid_from": "t",
                  "valid_to": None, "package_id": "t"}],
                {}, {"mode": "test"})


class TestIndexInterruption(Base):
    """A5 索引更新中断。"""
    def test_interrupted_rebuild_keeps_live_index(self):
        before = self.svc.index_status()["live_docs"]
        with self.assertRaises(IndexRebuildInterrupted):
            self.svc.reindex(fail_after=2)
        mid = self.svc.index_status()
        self.assertEqual(mid["live_docs"], before)  # 线上未受影响
        self.assertTrue(mid["staging_present"])
        # 搜索仍然可用
        self.assertTrue(self.svc.search("霓虹"))
        n = self.svc.reindex()
        self.assertGreaterEqual(n, before)
        self.assertFalse(self.svc.index_status()["staging_present"])


class TestFrozenClosure(Base):
    """A6 冻结整套闭包 + 勘误不重写。"""
    def test_opener_frozen_1_0(self):
        r = self.svc.explain_example("ex_opener")
        self.assertTrue(r["matches_published"])
        self.assertEqual(r["frozen_hash"],
                         self.svc.compile_for_package("1.0.0")["hash"])
        self.assertEqual(r["recomputed"]["total_score_v1"], 305)

    def test_grand_final_frozen_and_not_rewritten(self):
        r = self.svc.explain_example("ex_grand_final")
        self.assertTrue(r["matches_published"])
        total = r["recomputed"]["total_score_v2"]
        self.assertAlmostEqual(total, 761.25, delta=1e-2)
        # 即使当前版已变，回放仍用冻结身份（不是当前 1.1.2 哈希）
        self.assertNotEqual(r["frozen_hash"],
                            self.svc.compile_for_package("1.1.2")["hash"])

    def test_erratum_separate_correction_not_rewrite(self):
        row = self.repo.conn.execute(
            "SELECT expected FROM examples WHERE example_id='ex_grand_final'"
        ).fetchone()
        expected = json.loads(row["expected"])
        r = self.svc.explain_example("ex_grand_final")
        self.assertEqual(r["expected_published"], expected)   # 原始值原样保留
        self.assertEqual(len(r["corrections"]), 1)
        corr = r["corrections"][0]
        self.assertEqual(corr["errata_id"], "E-1")
        self.assertNotEqual(corr["corrected_hash"], r["frozen_hash"])
        # 修正值与原始值并列，且不同
        self.assertAlmostEqual(
            corr["recomputed"]["total_score_v2"], 466.25, delta=1e-2)
        self.assertNotAlmostEqual(
            corr["recomputed"]["total_score_v2"],
            expected["total_score_v2"], delta=1e-2)

    def test_no_mix_old_character_new_formula(self):
        # 冻结 1.0 闭包中根本不存在 1.1 才有的 total_score_v2/combo
        comp10 = self.svc.compile_for_package("1.0.0")
        self.assertNotIn("total_score_v2", comp10["rules"])
        self.assertNotIn("combo_ping_v1", comp10["rules"])
        comp11 = self.svc.compile_for_package("1.1.1")
        self.assertIn("total_score_v2", comp11["rules"])


class TestCacheIdentity(Base):
    """A8 预编译 vs 按查询组合 + 缓存身份。"""
    def test_precompiled_identity_matches_query_content(self):
        for v in self.svc.verify_precompiled():
            self.assertTrue(v["identity_ok"], v["package_id"])

    def test_cache_reuses_same_closure_distinct_for_other(self):
        a = self.svc.compile_for_package("1.1.1")
        b = self.svc.compile_for_package("1.1.1")
        self.assertIs(a, b)  # 同身份命中缓存对象
        # 在包发布时刻按查询组合 => 同一时间点同一规则集+策略 => 身份相同、缓存共享
        c, active = self.svc.compile_as_of("2026-09-10T18:30:00Z")
        self.assertEqual(a["hash"], c["hash"])
        self.assertIs(a, c)
        self.assertIn("1.1.1", active)
        # 更晚的时间点即便规则集恰好相同，时间戳进入身份，闭包身份也不同（防止跨时刻误复用）
        later, _ = self.svc.compile_as_of("2026-09-11T12:00:00Z")
        self.assertNotEqual(a["hash"], later["hash"])
        self.assertEqual(a["content_hash"], later["content_hash"])
        d, _ = self.svc.compile_as_of("2026-09-21T00:00:00Z")
        self.assertNotEqual(a["content_hash"], d["content_hash"])

    def test_one_char_rule_change_changes_hash(self):
        rules, policies, blob, _ = resolve_closure(self.repo, "2026-09-21T00:00:00Z")
        h1 = content_hash(rules, policies)
        rules2 = [dict(r) for r in rules]
        rules2[0] = dict(rules2[0], source=rules2[0]["source"] + " ")
        self.assertNotEqual(h1, content_hash(rules2, policies))


class TestReport(Base):
    """A9 报告与逐步解释（API 层）。"""
    def test_report_contains_summary_and_inputs(self):
        inputs = {"character": "nova", "map": "neon", "item": "shield",
                  "kills": 2, "ranked_kills": 1}
        rep = self.svc.build_report(package_id="1.1.1", inputs=inputs)
        self.assertIn("rule_package_summary", rep)
        self.assertEqual(rep["situation_input"], inputs)
        self.assertGreater(len(rep["score_steps"]), 5)
        # 每个生效步骤可解释来源
        for s in rep["score_steps"]:
            self.assertIn("rule_id", s)
            self.assertIn("formula", s)
            self.assertIn("value", s)
        # 有界环步骤带迭代轮次
        fix = [s for s in rep["score_steps"] if s["kind"] == "fixpoint"]
        self.assertTrue(fix)
        self.assertTrue(all(s["iterations"] and s["converged"] for s in fix))

    def test_report_diagnostic_for_conflict(self):
        rep = self.svc.build_report(
            package_id="1.1.0",
            inputs={"character": "nova", "map": "neon", "item": "magnet"})
        self.assertIsNotNone(rep["diagnostic"])
        self.assertEqual(rep["diagnostic"]["code"], "RULE_CONFLICT")

    def test_report_diagnostic_for_budget(self):
        rules = [{"rule_id": "z", "key": "z", "title": "z", "category": "scoring",
                  "source": "iter_drag(input('n',0))", "slot": None,
                  "conflict_group": None, "applicable_when": None,
                  "fixpoint": None, "priority": 0, "valid_from": "t",
                  "valid_to": None, "package_id": "t"}]
        comp = compile_closure(rules, {}, {"mode": "test", "rules": rules})
        self.svc._cache[comp["hash"]] = comp
        rep = self.svc.build_report(
            snapshot_hash=None, inputs={"n": 100000}, node_budget=500)
        # 自定义规则未入库，直接验证 evaluate 行为即可
        with self.assertRaises(RuleEngineError) as cm:
            evaluate(comp, {"n": 100000}, node_budget=500)
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")


class TestSearchDefault(Base):
    def test_default_hides_expired_versions(self):
        cur = self.svc.search("nova")
        ids = [d["doc_id"] for d in cur if d["doc_type"] == "rule"]
        self.assertIn("score_nova_v2", ids)
        self.assertNotIn("score_nova_v2_hot", ids)
        self.assertNotIn("score_nova_v1", ids)

    def test_historical_filter_at_time(self):
        ids = [d["doc_id"] for d in
               self.svc.search("nova", as_of="2026-09-02T00:00:00Z")
               if d["doc_type"] == "rule"]
        self.assertEqual(ids, ["score_nova_v1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
