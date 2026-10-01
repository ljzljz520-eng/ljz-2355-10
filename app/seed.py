"""Idempotent seed: a small but complete game-rules corpus.

Timeline coordinates: patch seq * 10 + day_slot.
  v1.0 -> 10, v1.1 -> 20, v1.2 -> 30, v1.3 -> 40 (morning 41 / evening 42),
  v1.4 -> 50.
"""
from __future__ import annotations

import json

from .db import transaction

PATCHES = [
    ("v1.0", 1, "首发赛季", "2026-01-10"),
    ("v1.1", 2, "限时奖励上线（含勘误缺陷）", "2026-03-10"),
    ("v1.2", 3, "冰原地图与连击链", "2026-05-10"),
    ("v1.3", 4, "角色平衡日（同日两更）", "2026-07-10"),
    ("v1.4", 5, "勘误季：修正限时奖励", "2026-09-10"),
]

RULES = [
    ("kill_score", "scoring", "击败得分", "每次击败获得的基础分数"),
    ("time_bonus", "scoring", "限时奖励", "快速结束比赛获得的奖励分"),
    ("terrain_bonus", "scoring", "地形奖励", "利用地图地形获得的奖励分"),
    ("item_ice_charm", "scoring", "寒冰护符分数", "携带寒冰护符时的冰面计分（与地形奖励冲突）"),
    ("combo_chain", "scoring", "连击链", "连击数按定点迭代收敛后的奖励分"),
    ("bad_loop", "mechanic", "缺陷环规则", "两个规则互相引用的非法递归（应被拒绝编译）"),
    ("bad_loop_b", "mechanic", "缺陷环规则乙", "与缺陷环规则互引"),
    ("diverge_rule", "mechanic", "发散迭代规则", "有界迭代不收敛，必须诊断"),
    ("char_ranger", "character", "游侠", "远程角色，攻击随补丁调整"),
    ("char_guardian", "character", "守卫", "近战角色，防守型"),
    ("map_icefield", "map", "冰原", "冰面地图，地形判定字段 terrain=ice"),
    ("item_ice_charm_def", "item", "寒冰护符", "道具：在冰面改写地形计分"),
]

# (rule_id, label, patch, status, body, params, expr,
#  start_patch, end_patch, start_seq, end_seq, deps)
V = [
    ("kill_score", "1.0.0", "v1.0", "superseded",
     "每次击败得 100 分（首发基线）。",
     {"priority": 100, "gate": {"t": "cmp", "op": "gt",
        "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 0}}},
     {"t": "arith", "op": "mul",
      "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 100}},
     "v1.0", "v1.1", 10, 20, []),

    ("kill_score", "1.2.0", "v1.2", "active",
     "冰原赛季击败得分上调为 120。",
     {"priority": 100, "gate": {"t": "cmp", "op": "gt",
        "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 0}}},
     {"t": "arith", "op": "mul",
      "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 120}},
     "v1.2", "v1.3", 30, 41, []),

    ("kill_score", "1.3.0", "v1.3", "superseded",
     "平衡日早更：击败得分削弱为 110；同日晚些被进一步调整（同日先后，seq 坐标 41）。",
     {"priority": 100, "day_slot": "morning", "gate": {"t": "cmp", "op": "gt",
        "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 0}}},
     {"t": "arith", "op": "mul",
      "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 110}},
     "v1.3", "v1.3", 41, 42, []),

    ("kill_score", "1.3.1", "v1.3", "active",
     "平衡日晚更（seq 坐标 42）：击败得分定为 125；与 1.2.0 的区间在同日内完成切换。",
     {"priority": 100, "day_slot": "evening", "gate": {"t": "cmp", "op": "gt",
        "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 0}}},
     {"t": "arith", "op": "mul",
      "l": {"t": "field", "path": "stats.kills"}, "r": {"t": "lit", "v": 125}},
     "v1.3", None, 42, None, []),

    ("time_bonus", "1.1.0", "v1.1", "superseded",
     "120 秒内结束比赛奖励 50 分（首发实现，正确）。",
     {"priority": 90, "gate": {"t": "cmp", "op": "lte",
        "l": {"t": "field", "path": "match.duration_sec"}, "r": {"t": "lit", "v": 120}}},
     {"t": "cond",
      "if": {"t": "cmp", "op": "lte",
             "l": {"t": "field", "path": "match.duration_sec"},
             "r": {"t": "lit", "v": 120}},
      "then": {"t": "lit", "v": 50}, "else": {"t": "lit", "v": 0}},
     "v1.1", "v1.1", 20, 29, []),

    ("time_bonus", "1.1.1", "v1.1", "corrected",
     "【有缺陷】条件方向写反：>120 秒才给 50 分，与规则文字相反。已被 v1.4 勘误修正。",
     {"priority": 90, "gate": {"t": "cmp", "op": "gt",
        "l": {"t": "field", "path": "match.duration_sec"}, "r": {"t": "lit", "v": 120}}},
     {"t": "cond",
      "if": {"t": "cmp", "op": "gt",
             "l": {"t": "field", "path": "match.duration_sec"},
             "r": {"t": "lit", "v": 120}},
      "then": {"t": "lit", "v": 50}, "else": {"t": "lit", "v": 0}},
     "v1.2", "v1.3", 30, 49, []),

    ("terrain_bonus", "1.2.0", "v1.2", "active",
     "在冰原利用地形（terrain_used=true）得 30 分。",
     {"priority": 50, "conflict_group": "ice_scoring",
      "gate": {"t": "logic", "op": "and",
               "l": {"t": "cmp", "op": "eq",
                     "l": {"t": "field", "path": "map.terrain"},
                     "r": {"t": "lit", "v": "ice"}},
               "r": {"t": "field", "path": "events.terrain_used"}}},
     {"t": "cond",
      "if": {"t": "logic", "op": "and",
             "l": {"t": "cmp", "op": "eq",
                   "l": {"t": "field", "path": "map.terrain"},
                   "r": {"t": "lit", "v": "ice"}},
             "r": {"t": "field", "path": "events.terrain_used"}},
      "then": {"t": "lit", "v": 30}, "else": {"t": "lit", "v": 0}},
     "v1.2", None, 30, None, []),

    ("item_ice_charm", "1.2.0", "v1.2", "active",
     "携带寒冰护符且在冰面得 40 分；与地形奖励同属 ice_scoring 冲突组，"
     "按优先级 60>50 只保留护符分数。",
     {"priority": 60, "conflict_group": "ice_scoring",
      "requires": ["item_ice_charm_def"],
      "gate": {"t": "logic", "op": "and",
               "l": {"t": "cmp", "op": "eq",
                     "l": {"t": "field", "path": "map.terrain"},
                     "r": {"t": "lit", "v": "ice"}},
               "r": {"t": "field", "path": "items.ice_charm"}}},
     {"t": "cond",
      "if": {"t": "logic", "op": "and",
             "l": {"t": "cmp", "op": "eq",
                   "l": {"t": "field", "path": "map.terrain"},
                   "r": {"t": "lit", "v": "ice"}},
             "r": {"t": "field", "path": "items.ice_charm"}},
      "then": {"t": "lit", "v": 40}, "else": {"t": "lit", "v": 0}},
     "v1.2", None, 30, None, [("item_ice_charm_def", "requires")]),

    ("combo_chain", "1.2.0", "v1.2", "active",
     "连击链：从连击数起，每轮按 floor(10 + 0.5×上一轮) 递推，连续两轮相等时收敛，"
     "上限 12 轮；最终分数取收敛值与 60 上限的较小值。有界定点语义。",
     {"priority": 40, "constants": {"cap_bonus": 60},
      "gate": {"t": "cmp", "op": "gt",
               "l": {"t": "field", "path": "stats.combo"}, "r": {"t": "lit", "v": 0}}},
     {"t": "min", "args": [
        {"t": "const", "name": "cap_bonus"},
        {"t": "fixed", "recurrence_rule": "combo_chain",
         "start": {"t": "field", "path": "stats.combo"},
         "next": {"t": "arith", "op": "floor",
                  "l": {"t": "arith", "op": "add",
                        "l": {"t": "lit", "v": 10},
                        "r": {"t": "arith", "op": "mul",
                              "l": {"t": "lit", "v": 0.5},
                              "r": {"t": "prev"}}},
                  "r": {"t": "lit", "v": 1}},
         "until": {"t": "cmp", "op": "eq",
                   "l": {"t": "prev"},
                   "r": {"t": "candidate"}},
         "max_iter": 12, "min_iter": 1},
     ]},
     "v1.2", None, 30, None, []),

    ("bad_loop", "1.0.0", "v1.0", "active",
     "【非法】引用 bad_loop_b，而对方又引用回来——无界互递归，静态拒绝。",
     {"priority": 1},
     {"t": "ref", "rule": "bad_loop_b"},
     "v1.0", None, 10, None, [("bad_loop_b", "refs")]),

    ("bad_loop_b", "1.0.0", "v1.0", "active",
     "【非法】反向引用 bad_loop，构成环。",
     {"priority": 1},
     {"t": "ref", "rule": "bad_loop"},
     "v1.0", None, 10, None, [("bad_loop", "refs")]),

    ("diverge_rule", "1.0.0", "v1.0", "active",
     "【运行期错误】定点迭代 x+1 直到 >=10000，100 轮内不可能收敛，必须报告发散/超预算。",
     {"priority": 1},
     {"t": "fixed", "recurrence_rule": "diverge_rule",
      "start": {"t": "lit", "v": 0},
      "next": {"t": "arith", "op": "add",
               "l": {"t": "prev"}, "r": {"t": "lit", "v": 1}},
      "until": {"t": "cmp", "op": "gte",
                "l": {"t": "prev"}, "r": {"t": "lit", "v": 10_000}},
      "max_iter": 100},
     "v1.0", None, 10, None, []),

    ("char_ranger", "1.0.0", "v1.0", "superseded",
     "游侠：首发赛季攻击力 10。",
     {"attack": 10}, None, "v1.0", "v1.1", 10, 30, []),
    ("char_ranger", "1.2.0", "v1.2", "active",
     "游侠：冰原赛季攻击 11。",
     {"attack": 11}, None, "v1.2", None, 30, None, []),
    ("char_guardian", "1.0.0", "v1.0", "active",
     "守卫：攻击 8，护甲 5。",
     {"attack": 8, "armor": 5}, None, "v1.0", None, 10, None, []),
    ("map_icefield", "1.2.0", "v1.2", "active",
     "冰原：冰面地形，滑倒概率 0.2。",
     {"terrain": "ice", "slip_chance": 0.2}, None, "v1.2", None, 30, None, []),
    ("item_ice_charm_def", "1.2.0", "v1.2", "active",
     "寒冰护符：装备后改写冰面计分。",
     {"slot": "charm"}, None, "v1.2", None, 30, None, []),
]

ERRATUM_VERSION = ("time_bonus", "1.4.0", "v1.4", "active",
     "【勘误修正】恢复规则本意：120 秒内结束奖励 50 分。"
     "旧版示例局面不重写，仅以修正关系关联。",
     {"priority": 90, "gate": {"t": "cmp", "op": "lte",
        "l": {"t": "field", "path": "match.duration_sec"}, "r": {"t": "lit", "v": 120}}},
     {"t": "cond",
      "if": {"t": "cmp", "op": "lte",
             "l": {"t": "field", "path": "match.duration_sec"},
             "r": {"t": "lit", "v": 120}},
      "then": {"t": "lit", "v": 50}, "else": {"t": "lit", "v": 0}},
     "v1.4", None, 50, None, [])

EXAMPLES = [
    ("ex_launch_duel", "首发对局：2 击败", "v1.0", "v1.0",
     {"character": "char_ranger", "stats": {"kills": 2, "combo": 0},
      "match": {"duration_sec": 150}, "map": {"terrain": "dust"},
      "events": {"terrain_used": False}, "items": {"ice_charm": False}},
     200),
    ("ex_ice_rush", "冰原速攻：快局反无奖励（冻结缺陷版 v1.2，含地形奖励）", "v1.2", "v1.2",
     {"character": "char_ranger", "stats": {"kills": 3, "combo": 0},
      "match": {"duration_sec": 95}, "map": {"terrain": "ice"},
      "events": {"terrain_used": True}, "items": {"ice_charm": False}},
     390),
    ("ex_long_match_frozen", "慢局展示缺陷：>120 秒仍得 50（冻结缺陷，勘误只另标关系）",
     "v1.2", "v1.3",
     {"character": "char_guardian", "stats": {"kills": 1, "combo": 0},
      "match": {"duration_sec": 200}, "map": {"terrain": "dust"},
      "events": {"terrain_used": False}, "items": {"ice_charm": False}},
     170),
    ("ex_budget_breaker", "超预算示例：发散迭代规则", "v1.0", "v1.0",
     {"character": "char_guardian", "stats": {"kills": 0, "combo": 0},
      "match": {"duration_sec": 999}, "map": {"terrain": "dust"},
      "events": {"terrain_used": False}, "items": {"ice_charm": False},
      "_extra_roots": ["diverge_rule"]},
     None),
    ("ex_cycle", "非法递归示例：互引规则闭包", "v1.0", "v1.0",
     {"character": "char_guardian", "stats": {"kills": 0, "combo": 0},
      "match": {"duration_sec": 999}, "map": {"terrain": "dust"},
      "events": {"terrain_used": False}, "items": {"ice_charm": False},
      "_extra_roots": ["bad_loop"]},
     None),
]


def seed(conn) -> bool:
    with transaction(conn):
        if conn.execute("SELECT COUNT(*) c FROM patches").fetchone()["c"]:
            return False
        conn.executemany(
            "INSERT INTO patches(id, seq, title, released_on) VALUES (?,?,?,?)", PATCHES)
        conn.executemany(
            "INSERT INTO rules(id, kind, name, summary) VALUES (?,?,?,?)", RULES)

        version_ids: dict[tuple[str, str], int] = {}
        for v in V + [ERRATUM_VERSION]:
            (rule_id, label, patch_id, status, body, params, expr,
             start_patch, end_patch, start_seq, end_seq, deps) = v
            cur = conn.execute(
                "INSERT INTO rule_versions"
                "(rule_id, version_label, patch_id, status, body, params_json, expr_json,"
                " effective_start_patch, effective_end_patch,"
                " effective_start_seq, effective_end_seq) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rule_id, label, patch_id, status, body,
                 json.dumps(params, ensure_ascii=False),
                 json.dumps(expr, ensure_ascii=False) if expr is not None else None,
                 start_patch, end_patch, start_seq, end_seq))
            vid = cur.lastrowid
            version_ids[(rule_id, label)] = vid
            for target, kind in deps:
                conn.execute(
                    "INSERT INTO rule_dependencies(source_version, target_rule_id, kind)"
                    " VALUES (?,?,?)", (vid, target, kind))

        conn.execute(
            "INSERT INTO errata(patch_id, bad_version, fixed_version, note) "
            "VALUES (?,?,?,?)",
            ("v1.4", version_ids[("time_bonus", "1.1.1")],
             version_ids[("time_bonus", "1.4.0")],
             "限时奖励比较算符由 gt 误写，导致慢局反而加分；v1.4 改回 lte。"
             "已发布示例局面保持冻结，不重写其分数。"))

        for (eid, title, anchor, pub, situation, expected) in EXAMPLES:
            conn.execute(
                "INSERT INTO examples(id, title, anchor_patch, published_patch,"
                " situation_json, expected_score) VALUES (?,?,?,?,?,?)",
                (eid, title, anchor, pub,
                 json.dumps(situation, ensure_ascii=False), expected))
    return True
