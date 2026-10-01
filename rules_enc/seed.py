# -*- coding: utf-8 -*-
"""种子数据。

时间线 (UTC):
  2026-09-01  1.0.0  首发规则包
  2026-09-05          公开赛 (opener)，冻结 1.0.0 闭包
  2026-09-10 09:00   1.1.0  赛季更新（有界连环 combo、新地图/道具且条件冲突）
  2026-09-10 18:30   1.1.1  同日热修（击杀计分 24h 窗口、冲突裁决、Nova 微调）
  2026-09-11 18:30   热修击杀窗口到期
  2026-09-12          总决赛 (grand_final)，冻结当时闭包
  2026-09-20          勘误 E-1（澄清击杀只计排位击杀；不重写已发布示例）
  2026-09-20 12:00   1.1.2  勘误澄清落地（Nova 正式数值、ranked 击杀计分）
  2026-10-01          “当前”
"""
import json

from .compiler import resolve_closure, compile_closure, evaluate
from .indexing import rebuild_index


T100 = "2026-09-01T00:00:00Z"
T110 = "2026-09-10T09:00:00Z"
T111 = "2026-09-10T18:30:00Z"
T111_END = "2026-09-11T18:30:00Z"
T112 = "2026-09-20T12:00:00Z"
PLAY_OPEN = "2026-09-05T15:00:00Z"
PLAY_FINAL = "2026-09-11T12:00:00Z"
ERRATUM_AT = "2026-09-20T09:00:00Z"
NOW = "2026-10-01T00:00:00Z"

PACKAGES = [
    ("1.0.0", "release", T100, "首发规则包",
     "角色、地图、道具与击杀的基础计分规则。"),
    ("1.1.0", "release", T110, "赛季更新：霓虹庭院与磁铁",
     "新增霓虹庭院地图、磁铁道具（与地图条件存在冲突）、有界连击连环计分与赏金。"),
    ("1.1.1", "hotfix", T111, "同日热修：击杀限时加权与冲突裁决",
     "击杀分在 24 小时窗口内 2x；为磁铁/霓虹冲突配置 map_wins 裁决；Nova 临时下调。"),
    ("1.1.2", "release", T112, "勘误澄清：排位击杀",
     "落地 E-1：击杀分仅计排位击杀；Nova 正式数值 45。"),
    ("test-heavy", "test", "2026-09-15T00:00:00Z",
     "实验包：高成本迭代（预算演示，不参与正式比赛）",
     "含 iter_drag 大循环，用于演示“示例计算超预算”的明确诊断；test 类型。"),
]

# (rule_id, key, pkg, title, category, summary, source, slot, group, when, fp, prio, from, to)
def R(*args):
    return dict(zip(
        ("rule_id", "key", "package_id", "title", "category", "summary",
         "source", "slot", "conflict_group", "applicable_when", "fixpoint",
         "priority", "valid_from", "valid_to"), args))


RULES = [
    # ---------- 1.0.0 基础规则 ----------
    R("score_nova_v1", "score_nova", "1.0.0", "角色分·新星", "character",
      "选择新星(Nova)获得 50 分。",
      "50", "character", None,
      "input('character', '') == 'nova'", None, 0, T100, T111),
    R("score_axel_v1", "score_axel", "1.0.0", "角色分·阿克塞尔", "character",
      "选择阿克塞尔(Axel)获得 30 分。",
      "30", "character", None,
      "input('character', '') == 'axel'", None, 0, T100, None),
    R("map_yard_v1", "map_yard", "1.0.0", "地图分·废弃庭院", "map",
      "在废弃庭院比赛获得 60 分。",
      "60", "map", None,
      "input('map', '') == 'yard'", None, 0, T100, None),
    R("map_base_v1", "map_base", "1.0.0", "地图分·基础场地", "map",
      "基础场地 20 分。",
      "20", "map", None,
      "input('map', '') == 'base'", None, 0, T100, None),
    R("item_shield_v1", "item_shield", "1.0.0", "道具分·护盾", "item",
      "携带护盾获得 15 分。",
      "15", "item", None,
      "input('item', '') == 'shield'", None, 0, T100, None),
    R("kill_score_v1", "kill_score", "1.0.0", "击杀计分（基础）", "scoring",
      "每次击杀 100 分。",
      "input('kills', 0) * 100", "kill", None, None, None, 0, T100, None),
    R("total_score_v1", "total_score", "1.0.0", "总分（首版）", "scoring",
      "总分 = 100 基础分 + 角色 + 地图 + 道具 + 击杀。",
      "100 + ref('character') + ref('map') + ref('item') + ref('kill')",
      None, None, None, None, 0, T100, T110),

    # ---------- 1.1.0 赛季更新 ----------
    R("map_neon_v1", "map_neon", "1.1.0", "地图分·霓虹庭院", "map",
      "在霓虹庭院比赛获得 100 分。",
      "100", "map", "item_map_neon",
      "input('map', '') == 'neon'", None, 0, T110, None),
    R("item_magnet_v1", "item_magnet", "1.1.0", "道具分·磁铁（霓虹）", "item",
      "霓虹庭院携带磁铁获得 120 分；与霓虹地图分同时成立时构成冲突。",
      "120", "item", "item_map_neon",
      "input('map', '') == 'neon' and input('item', '') == 'magnet'",
      None, 0, T110, None),
    R("combo_ping_v1", "combo_ping", "1.1.0", "连击环·平击", "scoring",
      "有界连环：ping = 30 + 0.4*pong，初始 0，同步迭代收敛。",
      "30 + 0.4 * ref('combo_pong')",
      "combo", None, None,
      json.dumps({"max_iter": 50, "tol": 1e-6, "init": 0}),
      0, T110, None),
    R("combo_pong_v1", "combo_pong", "1.1.0", "连击环·还击", "scoring",
      "有界连环：pong = 0.5*ping，初始 0，同步迭代收敛。",
      "0.5 * ref('combo_ping')",
      "combo", None, None,
      json.dumps({"max_iter": 50, "tol": 1e-6, "init": 0}),
      0, T110, None),
    R("bounty_v1", "bounty", "1.1.0", "赏金", "scoring",
      "每次击杀额外 25 赏金。",
      "input('kills', 0) * 25", "bounty", None, None, None, 0, T110, None),
    R("total_score_v2", "total_score", "1.1.0", "总分（赛季版）", "scoring",
      "总分 = 100 + 角色 + 地图 + 道具 + 击杀 + 连击 + 赏金。",
      ("100 + ref('character') + ref('map') + ref('item') + ref('kill')"
       " + ref('combo') + ref('bounty')"),
      None, None, None, None, 0, T110, None),

    # ---------- 1.1.1 同日热修（限时 24h 的击杀加权） ----------
    R("kill_score_hotfix", "kill_score", "1.1.1",
      "击杀计分（热修限时 2x）", "scoring",
      "热修窗口内每次击杀 200 分；窗口 2026-09-10T18:30Z 至 2026-09-11T18:30Z。",
      "input('kills', 0) * 200", "kill", None, None, None, 10,
      T111, T111_END),
    R("score_nova_v2_hot", "score_nova", "1.1.1",
      "角色分·新星（热修临时下调）", "character",
      "热修临时将新星调整为 40 分。",
      "40", "character", None,
      "input('character', '') == 'nova'", None, 5, T111, T112),

    # ---------- 1.1.2 勘误澄清落地 ----------
    R("kill_score_ranked", "kill_score", "1.1.2",
      "击杀计分（仅排位击杀）", "scoring",
      "勘误 E-1：只有排位击杀计入，每次 100 分。",
      "input('ranked_kills', input('kills', 0)) * 100",
      "kill", None, None, None, 10, T112, None),
    R("score_nova_v2", "score_nova", "1.1.2",
      "角色分·新星（正式 45）", "character",
      "新星正式数值 45 分（勘误后定稿）。",
      "45", "character", None,
      "input('character', '') == 'nova'", None, 6, T112, None),

    # ---------- test-heavy 实验包：表达式级预算演示 ----------
    R("heavy_drag_v1", "heavy_drag", "test-heavy",
      "重型拖拽迭代（实验）", "scoring",
      "iter_drag(n) 逐项累加 n 次；n 很大时超过局面节点预算 => BUDGET_EXCEEDED。",
      "iter_drag(input('n', 0), 0.99, 1)", None, None, None, None, 0,
      "2026-09-15T00:00:00Z", None),
]

POLICIES = [
    ("pol_neon_111", "item_map_neon", "1.1.1", "winner",
     {"winner": "map_neon_v1", "rationale": "地图条件优先于道具条件（map_wins）"},
     5, T111, None),
]

ERRATA = [
    ("E-1", ERRATUM_AT, "kill_score", "ex_grand_final", "clarification",
     "勘误：击杀计分只计排位击杀",
     "1.1.0/1.1.1 文本中“击杀”被部分战队理解为全部击杀。正式澄清：只有"
     "排位击杀(ranked_kills)计入击杀分。已发布的 2026-09-12 总决赛示例结果"
     "保持原样（按当时闭包 frozen），本勘误另以“修正关系”标注并提供重算值，"
     "不重写历史。", "corrects"),
]

EXAMPLES = [
    ("ex_opener", "2026 赛季公开赛", PLAY_OPEN,
     {"character": "axel", "map": "yard", "item": "shield", "kills": 1},
     1.0),
    ("ex_grand_final", "2026 赛季总决赛", PLAY_FINAL,
     {"character": "nova", "map": "neon", "item": "shield", "kills": 2,
      "ranked_kills": 1},
     1.1),
]


def seed(repo, verbose=True):
    c = repo.conn
    c.execute("DELETE FROM meta")
    for p in PACKAGES:
        c.execute("INSERT INTO packages(package_id,kind,released_at,title,description)"
                  " VALUES(?,?,?,?,?)", p)
    for r in RULES:
        c.execute(
            "INSERT INTO rule_versions(rule_id,rule_key,package_id,title,category,"
            "summary,source,slot,conflict_group,applicable_when,fixpoint,priority,"
            "valid_from,valid_to,ord) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (r["rule_id"], r["key"], r["package_id"], r["title"], r["category"],
             r["summary"], r["source"], r["slot"], r["conflict_group"],
             r["applicable_when"], r["fixpoint"], r["priority"],
             r["valid_from"], r["valid_to"]))
        # 关系库显式存依赖
        from .parser import parse, collect_refs
        for tgt in collect_refs(parse(r["source"])):
            c.execute("INSERT OR IGNORE INTO rule_deps(from_rule_id,to_target)"
                      " VALUES(?,?)", (r["rule_id"], tgt))
    for pol in POLICIES:
        c.execute("INSERT INTO conflict_policies(policy_id,conflict_group,package_id,"
                  "mode,payload,priority,valid_from,valid_to) VALUES(?,?,?,?,?,?,?,?)",
                  (pol[0], pol[1], pol[2], pol[3], json.dumps(pol[4], ensure_ascii=False),
                   pol[5], pol[6], pol[7]))
    for e in ERRATA:
        c.execute("INSERT INTO errata(errata_id,published_at,target_key,target_example,"
                  "severity,title,body,relation) VALUES(?,?,?,?,?,?,?,?)", e)

    # ---- 为每个补丁包预编译规则包（冻结闭包） ----
    compiled_cache = {}
    for pkg_id, _, released_at, _, _ in PACKAGES:
        rules, policies, blob, active = resolve_closure(repo, released_at)
        compiled = compile_closure(rules, policies, blob)
        repo.save_snapshot(compiled["hash"], "package", pkg_id,
                           released_at, rules, policies)
        compiled_cache[pkg_id] = (released_at, compiled)
        if verbose:
            print(f"[预编译] 补丁包 {pkg_id} @ {released_at} 闭包身份 {compiled['hash']}"
                  f"（{len(rules)} 条规则，{len(policies)} 条策略）")

    # ---- 比赛示例：冻结整套闭包并计算原始已发布结果 ----
    for ex_id, title, played_at, inputs, pkg_hint in EXAMPLES:
        # 历史比赛冻结到比赛时刻生效闭包（示例直接绑定具体包，杜绝“旧数值+新公式”）
        pkg_id = "1.0.0" if pkg_hint == 1.0 else "1.1.1"
        released_at, compiled = compiled_cache[pkg_id]
        trace = evaluate(compiled, inputs)
        expected = trace["outputs"]
        c.execute("INSERT INTO examples(example_id,title,played_at,snapshot_hash,"
                  "inputs,expected,note) VALUES(?,?,?,?,?,?,?)",
                  (ex_id, title, played_at, compiled["hash"],
                   json.dumps(inputs, ensure_ascii=False),
                   json.dumps(expected, ensure_ascii=False),
                   f"冻结补丁包 {pkg_id} @ {released_at}"))

    # ---- 勘误修正关系：同一局面用修正版闭包重算，不改写原始示例 ----
    row = c.execute("SELECT inputs FROM examples WHERE example_id='ex_grand_final'").fetchone()
    gf_inputs = json.loads(row["inputs"])
    rules112, pol112, blob112, _ = resolve_closure(repo, T112)
    comp112 = compile_closure(rules112, pol112, blob112)
    repo.save_snapshot(comp112["hash"], "query", f"E-1:ex_grand_final", T112,
                       rules112, pol112)
    corr = evaluate(comp112, gf_inputs)
    c.execute("INSERT INTO example_corrections(example_id,errata_id,corrected_hash,"
              "recomputed,note) VALUES(?,?,?,?,?)",
            ("ex_grand_final", "E-1", comp112["hash"],
             json.dumps(corr["outputs"], ensure_ascii=False),
             "应用 E-1 澄清后的重算（ranked_kills=1），与原始结果并列展示"))

    repo.set_meta("current_package", "1.1.2")
    repo.set_meta("now", NOW)
    repo.conn.commit()

    # ---- 检索索引 ----
    rebuild_index(repo)
    return repo
