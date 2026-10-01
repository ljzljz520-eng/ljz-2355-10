# -*- coding: utf-8 -*-
"""规则求值器。

职责:
1. 按求值计划（拓扑组 / 有界不动点组）求值；
2. 有界环采用同步迭代: 组内引用始终读取“上一轮”的值，
   收敛判据为最大绝对变化 <= tol，超过 max_iter => 超预算诊断；
3. 统计 AST 节点求值次数，超过局面预算 => 超预算诊断；
4. 槽位汇总(slot_sum)与冲突组(conflict_group)裁决；
5. 记录逐步追踪，供前端解释分数来源。
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ast_nodes as ast


class RuleEngineError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class MissingInput(RuleEngineError):
    def __init__(self, key):
        super().__init__("MISSING_INPUT", f"局面输入缺少键: {key}", {"key": key})


class BudgetExceeded(RuleEngineError):
    def __init__(self, kind, message, detail=None):
        super().__init__("BUDGET_EXCEEDED", message, detail or {})
        self.detail["kind"] = kind


class RuleConflict(RuleEngineError):
    def __init__(self, group, candidates, policy=None):
        msg = f"冲突组 {group} 内多条规则条件同时生效: {', '.join(candidates)}"
        if policy:
            msg += f"（已按策略 {policy} 裁决）"
        else:
            msg += "，且该补丁闭包中没有配置裁决策略 —— 无法唯一求解"
        super().__init__("RULE_CONFLICT", msg, {
            "conflict_group": group, "candidates": candidates,
            "resolved": bool(policy), "policy": policy,
        })


@dataclass
class Step:
    rule_id: str
    title: str
    kind: str                       # rule / aggregator / fixpoint
    formula: str
    value: Any = None
    applicable: bool = True
    inputs: Dict[str, Any] = field(default_factory=dict)
    refs: Dict[str, Any] = field(default_factory=dict)
    iterations: Optional[int] = None
    converged: Optional[bool] = None
    rounds: Optional[List[Dict[str, float]]] = None
    note: str = ""


def _num(x) -> float:
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    return float(x)


class Evaluator:
    def __init__(self, compiled, inputs: Dict[str, Any], node_budget: int = 200_000):
        self.compiled = compiled
        self.inputs = inputs
        self.node_budget = node_budget
        self.node_count = 0
        self.env: Dict[str, Any] = {}
        self.steps: List[Step] = []
        self._used_inputs: Dict[str, Any] = {}
        self._used_refs: Dict[str, List[str]] = {}   # 单次表达式求值期间
        self._fix_members: Optional[set] = None
        self._prev: Dict[str, Any] = {}
        self._current_rule: Optional[str] = None

    # ---------- 表达式求值 ----------
    def eval_node(self, node: ast.Node):
        self.node_count += 1
        if self.node_count > self.node_budget:
            raise BudgetExceeded(
                "node_budget",
                f"局面计算超过节点预算 {self.node_budget}（已求值 {self.node_count} 个 AST 节点），"
                "判定为示例计算超预算并中止。",
                {"limit": self.node_budget, "used": self.node_count})

        if isinstance(node, ast.Number):
            return node.value
        if isinstance(node, ast.String):
            return node.value
        if isinstance(node, ast.Bool):
            return node.value
        if isinstance(node, ast.Input):
            key = node.key.value
            if key in self.inputs:
                val = self.inputs[key]
            elif node.default is not None:
                val = self.eval_node(node.default)
            else:
                raise MissingInput(key)
            self._used_inputs[key] = val
            return val
        if isinstance(node, ast.Ref):
            logical = node.name.value
            resolve = (self.compiled["rules"]
                       .get(self._current_rule, {}).get("ref_resolve") or {})
            name = resolve.get(logical, logical)
            cur_stack = self._used_refs.setdefault("_", [])
            cur_stack.append(logical)
            if self._fix_members is not None and name in self._fix_members:
                # 有界迭代: 组内引用读上一轮快照
                return self._prev[name]
            if name not in self.env:
                raise RuleEngineError(
                    "REF_UNRESOLVED",
                    f"引用 {logical}（解析为 {name}）在当前求值点尚无值（求值计划非法）",
                    {"ref": logical, "resolved": name})
            return self.env[name]
        if isinstance(node, ast.Unary):
            v = self.eval_node(node.operand)
            if node.op == "-":
                return -_num(v)
            return not bool(v)
        if isinstance(node, ast.BinOp):
            op = node.op
            if op == "and":
                a = self.eval_node(node.left)
                return self.eval_node(node.right) if a else a
            if op == "or":
                a = self.eval_node(node.left)
                return a if a else self.eval_node(node.right)
            a = self.eval_node(node.left)
            b = self.eval_node(node.right)
            if op == "+":
                return _num(a) + _num(b)
            if op == "-":
                return _num(a) - _num(b)
            if op == "*":
                return _num(a) * _num(b)
            if op == "/":
                return _num(a) / _num(b)
            if op == "%":
                return _num(a) % _num(b)
            if op == "==":
                return a == b
            if op == "!=":
                return a != b
            if op == "<":
                return _num(a) < _num(b)
            if op == "<=":
                return _num(a) <= _num(b)
            if op == ">":
                return _num(a) > _num(b)
            if op == ">=":
                return _num(a) >= _num(b)
        if isinstance(node, ast.Call):
            return self.eval_call(node)
        raise RuleEngineError("EVAL_ERROR", f"未知 AST 节点 {type(node).__name__}")

    def eval_call(self, node: ast.Call):
        name = node.name
        a = node.args
        if name == "if":
            cond = self.eval_node(a[0])
            return self.eval_node(a[1]) if cond else self.eval_node(a[2])
        if name == "sum":
            return sum(_num(self.eval_node(x)) for x in a)
        if name == "max":
            return max(_num(self.eval_node(x)) for x in a)
        if name == "min":
            return min(_num(self.eval_node(x)) for x in a)
        if name == "abs":
            return abs(_num(self.eval_node(a[0])))
        if name == "floor":
            return float(math.floor(_num(self.eval_node(a[0]))))
        if name == "ceil":
            return float(math.ceil(_num(self.eval_node(a[0]))))
        if name == "round":
            return float(round(_num(self.eval_node(a[0]))))
        if name == "clamp":
            x, lo, hi = (_num(self.eval_node(v)) for v in a)
            return min(max(x, lo), hi)
        if name == "iter_drag":
            # 逐项累加的拖延效应: sum_{i=0}^{n-1} base*factor^i
            # 用于演示表达式级预算（n 受局面预算约束）。
            n = int(_num(self.eval_node(a[0])))
            factor = _num(self.eval_node(a[1])) if len(a) > 1 else 0.5
            base = _num(self.eval_node(a[2])) if len(a) > 2 else 1.0
            total, term = 0.0, base
            for _ in range(n):
                # 每次迭代等价于一次加法与一次乘法（约 3 个 AST 节点成本）
                self.node_count += 3
                total += term
                term *= factor
                self.node_count += 1
                if self.node_count > self.node_budget:
                    raise BudgetExceeded(
                        "node_budget",
                        f"iter_drag 循环 {n} 次超过节点预算 {self.node_budget}",
                        {"limit": self.node_budget, "used": self.node_count})
            return total
        raise RuleEngineError("UNKNOWN_FUNCTION", f"未知函数 {name}", {"fn": name})

    # ---------- 主流程 ----------
    def run(self):
        c = self.compiled
        # 1) 预先求各贡献规则的生效条件（静态保证仅依赖 input）
        applicability = {}
        for rid, rdef in c["rules"].items():
            cond = rdef.get("applicable_ast")
            applicability[rid] = (self.eval_node(cond) if cond is not None else True)

        # 2) 冲突组裁决
        losers = self._resolve_conflicts(applicability)

        # 3) 按计划组求值
        for group in c["plan"]:
            agg = next((a for a in c["aggregators"] if a["id"] in group.rule_ids), None)
            if agg is not None:
                self._eval_aggregator(agg, applicability, losers)
                continue
            if group.bounded:
                self._eval_fixpoint(group)
            else:
                rid = group.rule_ids[0]
                self._eval_rule(rid, applicability, losers)

        outputs = c["outputs"]
        result = {oid: self.env.get(oid) for oid in outputs}
        return result, self.steps

    # ---------- 冲突 ----------
    def _resolve_conflicts(self, applicability):
        losers = set()
        groups: Dict[str, List[str]] = {}
        for rid, rdef in self.compiled["rules"].items():
            g = rdef.get("conflict_group")
            if g and applicability.get(rid):
                groups.setdefault(g, []).append(rid)
        for group, members in groups.items():
            if len(members) <= 1:
                continue
            policy = self.compiled["policies"].get(group)
            if policy is None:
                raise RuleConflict(group, sorted(members))
            if policy["mode"] == "winner":
                win = policy["winner"]
                if win not in members:
                    raise RuleConflict(group, sorted(members))
                losers.update(m for m in members if m != win)
            elif policy["mode"] == "priority":
                order = policy["order"]
                winner = next((r for r in order if r in members), None)
                if winner is None:
                    raise RuleConflict(group, sorted(members))
                losers.update(m for m in members if m != winner)
            else:
                raise RuleConflict(group, sorted(members))
        return losers

    # ---------- 单规则 ----------
    def _eval_rule(self, rid, applicability, losers):
        rdef = self.compiled["rules"][rid]
        self._current_rule = rid
        applies = bool(applicability.get(rid, True))
        if rid in losers:
            applies = False
        self._used_inputs = {}
        self._used_refs = {"_": []}
        if applies:
            value = self.eval_node(rdef["ast"])
            refvals = {}
            for logical in self._used_refs["_"]:
                target = (rdef.get("ref_resolve") or {}).get(logical, logical)
                refvals[logical] = self.env.get(target)
            step = Step(
                rule_id=rid, title=rdef.get("title", rid), kind="rule",
                formula=rdef.get("source", ""), value=value, applicable=True,
                inputs=dict(self._used_inputs), refs=refvals,
            )
            self.env[rid] = value
        else:
            value = 0
            reason = "被冲突策略压制" if rid in losers else "生效条件不满足"
            step = Step(
                rule_id=rid, title=rdef.get("title", rid), kind="rule",
                formula=rdef.get("source", ""), value=0, applicable=False, note=reason)
            self.env[rid] = 0
        self.steps.append(step)

    # ---------- 有界不动点 ----------
    def _eval_fixpoint(self, group):
        members = set(group.rule_ids)
        self._fix_members = members
        prev = dict(group.inits)
        rounds = []
        converged = False
        iterations = 0
        try:
            for it in range(1, group.max_iter + 1):
                iterations = it
                curr = {}
                for rid in group.rule_ids:
                    self._current_rule = rid
                    self._used_inputs = {}
                    self._used_refs = {"_": []}
                    self._prev = prev
                    curr[rid] = self.eval_node(self.compiled["rules"][rid]["ast"])
                rounds.append(dict(curr))
                delta = max(abs(_num(curr[r]) - _num(prev[r])) for r in group.rule_ids)
                prev = curr
                if delta <= group.tol:
                    converged = True
                    break
            if not converged:
                raise BudgetExceeded(
                    "iteration_cap",
                    f"有界规则环 {sorted(members)} 在 {group.max_iter} 轮内未收敛"
                    f"（最后变化量 {delta:.6g} > tol={group.tol:g}），"
                    "属于非收敛/超预算局面，拒绝给出分数。",
                    {"cycle": sorted(members), "max_iter": group.max_iter,
                     "last_delta": delta, "last_round": rounds[-1] if rounds else {}})
        finally:
            self._fix_members = None
        self.env.update(prev)
        for rid in group.rule_ids:
            rdef = self.compiled["rules"][rid]
            self.steps.append(Step(
                rule_id=rid, title=rdef.get("title", rid), kind="fixpoint",
                formula=rdef.get("source", ""), value=prev[rid],
                iterations=iterations, converged=True, rounds=rounds,
                note=f"有界同步迭代，{iterations} 轮收敛"))

    # ---------- 槽位汇总 ----------
    def _eval_aggregator(self, agg, applicability, losers):
        total = 0.0
        pieces = {}
        for rid in agg["contributors"]:
            if applicability.get(rid, True) and rid not in losers:
                pieces[rid] = self.env[rid]
                total += _num(self.env[rid])
        self.env[agg["id"]] = total
        self.steps.append(Step(
            rule_id=agg["id"], title=agg["title"], kind="aggregator",
            formula=" + ".join(agg["contributors"]) or "0",
            value=total, refs=dict(pieces),
            note=f"槽位 {agg['id']} 汇总（{agg['agg']}）"))
