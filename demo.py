# -*- coding: utf-8 -*-
"""命令行验收演示: python demo.py"""
import json
from rules_enc.repository import Repo
from rules_enc.seed import seed
from rules_enc.service import RuleService
from rules_enc.evaluator import RuleEngineError


def line(t=""): print(t)


def main():
    repo = Repo(":memory:")
    seed(repo, verbose=False)
    svc = RuleService(repo)

    line("=" * 64)
    line("场景 1｜同日补丁先后（1.1.0 09:00 → 1.1.1 18:30）")
    line("=" * 64)
    nova = lambda t: svc.explain_custom(
        {"character": "nova", "map": "base", "item": "shield",
         "kills": 0, "ranked_kills": 0}, as_of=t)["trace"]["outputs"]["__slot_character"]
    for t in ["2026-09-10T08:00:00Z", "2026-09-10T10:00:00Z",
              "2026-09-10T19:00:00Z", "2026-09-21T00:00:00Z"]:
        line(f"  {t}  Nova 角色分 = {nova(t)}")

    line("\n" + "=" * 64)
    line("场景 2｜生效范围重叠：热修 2x 击杀（priority 高者胜）")
    line("=" * 64)
    kill = lambda t: svc.explain_custom(
        {"kills": 2}, as_of=t)["trace"]["outputs"]["__slot_kill"]
    for t in ["2026-09-10T10:00:00Z", "2026-09-11T12:00:00Z", "2026-09-11T20:00:00Z"]:
        line(f"  {t}  kills=2 → kill 槽 = {kill(t)}")

    line("\n" + "=" * 64)
    line("场景 3｜道具×地图条件冲突")
    line("=" * 64)
    mag = {"character": "nova", "map": "neon", "item": "magnet", "kills": 1}
    for pkg in ["1.1.0", "1.1.1"]:
        try:
            out = svc.explain_custom(mag, package_id=pkg)
            line(f"  {pkg}: 可解，total = {out['trace']['outputs']['total_score_v2']:.2f}")
        except RuleEngineError as e:
            line(f"  {pkg}: 诊断 {e.code} → {e}")

    line("\n" + "=" * 64)
    line("场景 4｜历史回放冻结整套闭包 + 勘误不重写")
    line("=" * 64)
    for ex in ["ex_opener", "ex_grand_final"]:
        r = svc.explain_example(ex)
        tk = [k for k in r["expected_published"] if k.startswith("total")].pop()
        line(f"  {ex}: hash={r['frozen_hash'][:30]}…  "
             f"已发布={r['expected_published'][tk]:.2f} 重算={r['recomputed'][tk]:.2f} "
             f"一致={r['matches_published']}")
        for c in r["corrections"]:
            line(f"    └ 勘误 {c['errata_id']}（另标修正关系，不改写）: "
                 f"{c['recomputed'][tk]:.2f}")

    line("\n" + "=" * 64)
    line("场景 5｜示例计算超预算（test-heavy 补丁）")
    line("=" * 64)
    for n in (5, 100000):
        try:
            out = svc.explain_custom({"n": n}, package_id="test-heavy")
            line(f"  n={n}: heavy_drag = {out['trace']['outputs']['heavy_drag_v1']:.3f}")
        except RuleEngineError as e:
            line(f"  n={n}: 诊断 {e.code}/{e.detail['kind']} → {str(e)[:48]}…")

    line("\n" + "=" * 64)
    line("场景 6｜环语义：有界 vs 非法递归")
    line("=" * 64)
    from rules_enc.graph import build_plan
    try:
        build_plan({"a": {"refs": ["b"], "meta": {}},
                    "b": {"refs": ["a"], "meta": {
                        "fixpoint": {"max_iter": 5, "init": 0}}}})
    except Exception as e:
        line("  非法递归 → " + str(e))
    out = svc.explain_custom(
        {"character": "axel", "map": "base", "item": "shield", "kills": 0},
        package_id="1.1.0")
    line(f"  有界环 combo = {out['trace']['outputs']['__slot_combo']:.4f}（收敛）")

    line("\n" + "=" * 64)
    line("场景 7｜预编译包 vs 按查询组合：身份校验 + 缓存")
    line("=" * 64)
    for v in svc.verify_precompiled():
        line(f"  {v['package_id']:10s} 规则={v['rule_count']:2d} "
             f"身份一致={v['identity_ok']}  {v['snapshot_closure_hash'][:34]}…")
    a = svc.compile_for_package("1.1.1")
    b, _ = svc.compile_as_of("2026-09-10T18:30:00Z")
    line(f"  缓存复用同一对象: {a is b}（同一时间点组合与预编译身份相同）")

    line("\n" + "=" * 64)
    line("场景 8｜索引更新中断恢复")
    line("=" * 64)
    from rules_enc.indexing import IndexRebuildInterrupted
    before = svc.index_status()["live_docs"]
    try:
        svc.reindex(fail_after=3)
    except IndexRebuildInterrupted as e:
        line(f"  中断: {e}")
    line(f"  线上索引仍为 {before} 条，搜索可用结果数="
         f"{len(svc.search('击杀'))}；重试重建={svc.reindex()} 条")


if __name__ == "__main__":
    main()
