"""Acceptance tests (stdlib unittest - no third-party dependencies).

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import explain as E
from app import searchindex
from app.db import connect, init_db
from app.evaluator import evaluate
from app.packages import (build_closure, get_package, precompile_all,
                          precompile_patch)
from app.seed import seed
from app.versions import RuleVersionError, overlapping_versions, resolve_rule

SIT_FAST = {
    "character": "char_ranger", "stats": {"kills": 3, "combo": 0},
    "match": {"duration_sec": 95}, "map": {"terrain": "ice"},
    "events": {"terrain_used": True}, "items": {"ice_charm": False},
}
SIT_SLOW = {
    "character": "char_guardian", "stats": {"kills": 1, "combo": 0},
    "match": {"duration_sec": 200}, "map": {"terrain": "dust"},
    "events": {"terrain_used": False}, "items": {"ice_charm": False},
}


class BaseCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.conn = connect(path)
        init_db(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + ext)
            except OSError:
                pass


class TestSameDayPatches(BaseCase):
    def test_morning_evening_versions(self):
        self.assertEqual(resolve_rule(self.conn, "kill_score", 41)["version_label"], "1.3.0")
        self.assertEqual(resolve_rule(self.conn, "kill_score", 42)["version_label"], "1.3.1")
        self.assertEqual(resolve_rule(self.conn, "kill_score", 40)["version_label"], "1.2.0")
        pm = build_closure(self.conn, "v1.3", ["kill_score"], day_slot=1, persist=False)
        pe = build_closure(self.conn, "v1.3", ["kill_score"], day_slot=2, persist=False)
        sit = {"stats": {"kills": 2}, "match": {}, "map": {}, "events": {}, "items": {}}
        self.assertEqual(evaluate(pm, sit)["score"], 220)
        self.assertEqual(evaluate(pe, sit)["score"], 250)
        self.assertNotEqual(pm["package_id"], pe["package_id"])


class TestOverlappingIntervals(BaseCase):
    def test_overlap_diagnosed_and_blocks_eval(self):
        self.conn.execute(
            "INSERT INTO rule_versions(rule_id, version_label, patch_id, status, body,"
            " params_json, expr_json, effective_start_patch, effective_end_patch,"
            " effective_start_seq, effective_end_seq) "
            "VALUES ('kill_score','rogue','v1.2','active','rogue','{}',NULL,"
            " 'v1.2',NULL,30,40)")
        self.conn.commit()
        self.assertTrue(any(d["rule_id"] == "kill_score"
                            for d in overlapping_versions(self.conn)))
        with self.assertRaises(RuleVersionError):
            resolve_rule(self.conn, "kill_score", 35)
        pkg = build_closure(self.conn, "v1.2", ["kill_score"], persist=False)
        self.assertEqual(pkg["status"], "error")
        self.assertIn("OVERLAPPING_INTERVAL",
                      [d["code"] for d in pkg["diagnostics"]])
        out = evaluate(pkg, SIT_FAST)
        self.assertIsNone(out["score"])
        self.assertIn("PACKAGE_INVALID", [d["code"] for d in out["diagnostics"]])

    def test_seed_has_no_overlaps(self):
        self.assertEqual(overlapping_versions(self.conn), [])


class TestConditionConflict(BaseCase):
    def _pkg(self):
        return build_closure(self.conn, "v1.2",
                             ["kill_score", "time_bonus", "terrain_bonus",
                              "item_ice_charm", "combo_chain"], persist=False)

    def test_item_beats_map_then_map_alone(self):
        sit = dict(SIT_FAST, items={"ice_charm": True})
        out = evaluate(self._pkg(), sit)
        self.assertEqual(out["score"], 400)  # 360 + 40, terrain 30 suppressed
        self.assertIn("CONDITION_CONFLICT_SUPPRESSED",
                      [d["code"] for d in out["diagnostics"]])
        suppressed = {s["rule_id"]: s for s in out["suppressed"]}
        self.assertEqual(suppressed["terrain_bonus"]["by"], "item_ice_charm")
        out2 = evaluate(self._pkg(), SIT_FAST)
        self.assertEqual(out2["score"], 390)  # 360 + 30 terrain
        self.assertFalse([d for d in out2["diagnostics"]
                          if d["code"] == "CONDITION_CONFLICT_SUPPRESSED"])


class TestBudgetAndBoundedIteration(BaseCase):
    def test_budget_exceeded(self):
        pkg = build_closure(self.conn, "v1.0", ["kill_score"], persist=False)
        sit = {"stats": {"kills": 1}, "match": {}, "map": {}, "events": {}, "items": {}}
        out = evaluate(pkg, sit, budget=1)
        self.assertIsNone(out["score"])
        self.assertEqual(out["diagnostics"][0]["code"], "EVALUATION_BUDGET_EXCEEDED")
        self.assertEqual(out["diagnostics"][0]["budget"], 1)

    def test_divergence_diagnosed(self):
        r = E.explain_situation(
            self.conn, {**SIT_SLOW, "_extra_roots": ["diverge_rule"]},
            "v1.0", roots=["kill_score", "diverge_rule"], persist=False)
        self.assertIsNone(r["score"])
        codes = [d["code"] for d in r["diagnostics"]]
        self.assertIn("BOUNDED_ITERATION_DIVERGES", codes)
        msg = next(d["message"] for d in r["diagnostics"]
                   if d["code"] == "BOUNDED_ITERATION_DIVERGES")
        self.assertIn("diverge_rule", msg)
        self.assertIn("收敛", msg)

    def test_fixed_point_converges(self):
        sit = dict(SIT_FAST, stats={"kills": 0, "combo": 8})
        pkg = build_closure(self.conn, "v1.2", ["combo_chain"], persist=False)
        out = evaluate(pkg, sit)
        self.assertEqual(out["score"], 19)
        combo = next(c for c in out["contributions"] if c["rule_id"] == "combo_chain")
        self.assertEqual(combo["detail"]["iterations"], 5)


class TestCycleClassification(BaseCase):
    def test_illegal_cycle_rejected(self):
        pkg = build_closure(self.conn, "v1.0", ["bad_loop"], persist=False)
        self.assertEqual(pkg["status"], "error")
        codes = [d["code"] for d in pkg["diagnostics"]]
        self.assertIn("ILLEGAL_RECURSION", codes)
        diag = next(d for d in pkg["diagnostics"] if d["code"] == "ILLEGAL_RECURSION")
        self.assertEqual(set(diag["rules"]), {"bad_loop", "bad_loop_b"})
        self.assertIsNone(evaluate(pkg, SIT_SLOW)["score"])

    def test_bounded_self_loop_legal(self):
        pkg = build_closure(self.conn, "v1.2", ["combo_chain"], persist=False)
        self.assertEqual(pkg["status"], "ok")
        self.assertEqual(pkg["stats"]["bounded_fixed_points"], 1)
        self.assertEqual(pkg["diagnostics"], [])


class TestFrozenClosureAndErrata(BaseCase):
    def test_frozen_package_entire_old_formulas(self):
        r = E.freeze_example(self.conn, "ex_ice_rush")
        self.assertEqual(r["explanation"]["score"], 390)
        pkg_id = r["explanation"]["package_id"]
        link = self.conn.execute(
            "SELECT frozen_package_id FROM examples WHERE id='ex_ice_rush'").fetchone()[0]
        self.assertEqual(link, pkg_id)
        rep = E.replay_frozen(self.conn, "ex_ice_rush")
        self.assertEqual(rep["score"], 390)
        frozen = get_package(self.conn, pkg_id)
        tb = next(v for v in frozen["frozen"].values() if v["rule_id"] == "time_bonus")
        self.assertEqual(tb["version_label"], "1.1.1")
        self.assertIn('"gt"', json.dumps(tb["expr"], ensure_ascii=False))

    def test_errata_notice_not_rewrite(self):
        E.freeze_example(self.conn, "ex_long_match_frozen")
        rep = E.replay_frozen(self.conn, "ex_long_match_frozen")
        self.assertEqual(rep["score"], 170)
        self.assertTrue(rep["errata_notices"])
        adv = E.corrected_replay_advisory(self.conn, "ex_long_match_frozen", "v1.4")
        self.assertEqual(adv["score"], 125)
        self.assertEqual(E.replay_frozen(self.conn, "ex_long_match_frozen")["score"], 170)

    def test_all_seed_examples_match_expected(self):
        for row in self.conn.execute("SELECT id, expected_score FROM examples"):
            r = E.freeze_example(self.conn, row["id"])
            if row["expected_score"] is None:
                self.assertIsNone(r["explanation"]["score"], row["id"])
            else:
                self.assertEqual(r["explanation"]["score"], row["expected_score"], row["id"])


class TestCompileStrategies(BaseCase):
    def test_precompiled_equals_query_identity(self):
        pre = precompile_patch(self.conn, "v1.2")
        q1 = build_closure(self.conn, "v1.2", pre["roots"], mode="query")
        q2 = build_closure(self.conn, "v1.2", pre["roots"], mode="query")
        self.assertEqual(q1["package_id"], pre["package_id"])
        self.assertTrue(q2["cache_hit"])
        q3 = build_closure(self.conn, "v1.2", ["kill_score"], mode="query")
        self.assertNotEqual(q3["package_id"], pre["package_id"])

    def test_precompile_all(self):
        pkgs = precompile_all(self.conn)
        self.assertEqual([p["anchor_patch"] for p in pkgs],
                         ["v1.0", "v1.1", "v1.2", "v1.3", "v1.4"])
        self.assertTrue(all(p["status"] == "ok" for p in pkgs))


class TestSearchAndIndex(BaseCase):
    def _build(self):
        searchindex.start_build(self.conn, resume=False)
        while searchindex.build_batch(self.conn, 50)["status"] == "building":
            pass

    def test_current_default_and_history_filter(self):
        self._build()
        cur = searchindex.search(self.conn, "限时")
        self.assertTrue(cur["current_only"])
        labels = [r["version_label"] for r in cur["results"]]
        self.assertIn("1.4.0", labels)
        self.assertNotIn("1.1.1", labels)
        hist = searchindex.search(self.conn, "限时", anchor_patch="v1.2",
                                  current_only=False)
        labels = [r["version_label"] for r in hist["results"]]
        self.assertIn("1.1.1", labels)
        self.assertNotIn("1.4.0", labels)

    def test_search_kind_filter_and_english_token(self):
        self._build()
        res = searchindex.search(self.conn, "ranger", kind="character")
        self.assertTrue(res["results"])
        self.assertTrue(all(r["kind"] == "character" for r in res["results"]))

    def test_interrupted_index_search_resilient_and_resumable(self):
        searchindex.start_build(self.conn, resume=False)
        searchindex.build_batch(self.conn, 2)
        st = searchindex.interrupt(self.conn)
        self.assertEqual(st["status"], "building")
        res = searchindex.search(self.conn, "击败", current_only=True)
        self.assertTrue(any(r["rule_id"] == "kill_score" for r in res["results"]))
        searchindex.start_build(self.conn, resume=True)
        last = searchindex.build_batch(self.conn, 5)
        while last["status"] == "building":
            last = searchindex.build_batch(self.conn, 5)
        self.assertEqual(last["status"], "ready")
        self.assertEqual(last["done"], last["total"])


class TestExplanationAndReport(BaseCase):
    def test_steps_and_report(self):
        r = E.explain_situation(self.conn, SIT_FAST, "v1.4", persist=True)
        self.assertEqual(r["score"], 455)  # 375 kill + 50 quick + 30 terrain
        texts = " ".join(s["text"] for s in r["step_explanation"])
        self.assertIn("kill_score", texts)
        self.assertIn("time_bonus", texts)
        self.assertTrue(any(s["action"] == "total" for s in r["step_explanation"]))
        report = E.export_report(self.conn, r["package_id"], SIT_FAST)
        self.assertEqual(report["score"], 455)
        self.assertEqual(report["situation_input"], SIT_FAST)
        self.assertEqual(report["package_summary"]["status"], "ok")
        self.assertTrue(report["frozen_rules"])
        # the exported report must also break down where the score came from
        self.assertTrue(report["score_breakdown"])
        self.assertTrue(any(s.get("action") == "total" for s in report["score_breakdown"]))
        self.assertEqual(report["evaluation"]["steps_used"], r["steps_used"])
        summary = E.export_report(self.conn, r["package_id"])
        self.assertGreaterEqual(summary["package_summary"]["rule_count"], 1)
        self.assertIsNone(summary["situation_input"])
        self.assertNotIn("score_breakdown", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGraphClassificationUnit(unittest.TestCase):
    def test_multi_rule_bounded_cycle_distinct_diagnosis(self):
        from app.graph import classify_cycles
        d = classify_cycles({"a": ["b"], "b": ["a"]},
                            bounded_edges={("a", "b"), ("b", "a")})
        self.assertEqual([x["code"] for x in d], ["BOUNDED_CYCLE_UNSUPPORTED"])

    def test_mixed_cycle_is_illegal(self):
        from app.graph import classify_cycles
        d = classify_cycles({"a": ["b"], "b": ["a"]}, bounded_edges={("a", "b")})
        self.assertEqual([x["code"] for x in d], ["ILLEGAL_RECURSION"])

    def test_bounded_self_loop_and_acyclic_ok(self):
        from app.graph import classify_cycles
        self.assertEqual(classify_cycles({"a": ["a"]}, bounded_edges={("a", "a")}), [])
        self.assertEqual(classify_cycles({"a": ["b"], "b": []}, bounded_edges=set()), [])
