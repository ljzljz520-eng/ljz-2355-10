"""Evaluate a FROZEN rule package against a concrete situation.

The evaluator is deliberately closure-local: it never reaches back into the
database for a "newer" rule version. Historical replay therefore uses exactly
the character numbers AND the scoring formula frozen together in one package.

Supported DSL nodes (see seed data for examples)::

  {"t":"lit","v":N}                         constant
  {"t":"const","name":"str"}                named parameter constant
  {"t":"field","path":"stats.kills"}        value from the situation
  {"t":"not","arg":...}, {"t":"neg","arg":...}
  {"t":"cmp","op":"gte|gt|lte|lt|eq|neq","l":...,"r":...}
  {"t":"arith","op":"add|sub|mul|div|floor","l":...,"r":...}
  {"t":"logic","op":"and|or","l":...,"r":...}
  {"t":"cond","if":...,"then":...,"else":...}
  {"t":"min"|"max"|"sum","args":[...]}
  {"t":"ref","rule":"kill_score"}           value of another rule in closure
  {"t":"fixed","recurrence_rule":"combo","start":...,"next":...,
              "until":{"cmp","op":"eq","l":{"t":"prev"},"r":{...}},
              "max_iter":N,"min_iter":0}
       inside next/until: {"t":"prev"} = previous iterate (bounded self ref)
"""
from __future__ import annotations

from typing import Any

DEFAULT_BUDGET = 5_000
MAX_BUDGET = 100_000
_MISSING = object()


class BudgetExceeded(Exception):
    def __init__(self, spent: int, budget: int, rule_id: str):
        super().__init__(f"求值超出预算：已执行 {spent} 步，超过上限 {budget}（规则 {rule_id}）。")
        self.spent = spent
        self.budget = budget
        self.rule_id = rule_id

    def to_diag(self) -> dict:
        return {
            "code": "EVALUATION_BUDGET_EXCEEDED",
            "severity": "error",
            "spent": self.spent,
            "budget": self.budget,
            "rule_id": self.rule_id,
            "message": str(self),
        }


class EvalError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def to_diag(self) -> dict:
        d = {"code": self.code, "severity": "error", "message": self.message}
        d.update(self.extra)
        return d


def _dig(situation: dict, path: str) -> Any:
    cur: Any = situation
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


class Evaluator:
    def __init__(self, package: dict, situation: dict, budget: int = DEFAULT_BUDGET):
        self.frozen = package["frozen"]
        self.situation = situation
        self.budget = min(max(1, budget), MAX_BUDGET)
        self.spent = 0
        self.trace: list[dict] = []
        self.diagnostics: list[dict] = []
        self._memo: dict[str, Any] = {}
        self._stack: list[str] = []
        self._fixed_iterations: dict[tuple[str, Any], int] = {}
        self._candidate: Any = _MISSING

    # -- core expression evaluation ----------------------------------------

    def eval_node(self, node: Any, prev_val: Any = None, prev_rule: str | None = None,
                  rule_id: str = "<expr>") -> Any:
        self.spent += 1
        if self.spent > self.budget:
            raise BudgetExceeded(self.spent, self.budget, rule_id)
        if not isinstance(node, dict):
            raise EvalError("BAD_NODE", f"非法表达式节点：{node!r}")
        t = node.get("t")

        if t == "lit":
            return node["v"]
        if t == "const":
            name = node["name"]
            owner = self._stack[-1] if self._stack else rule_id
            ver = self.frozen.get(owner)
            if ver and name in ver.get("params", {}).get("constants", {}):
                return ver["params"]["constants"][name]
            raise EvalError("UNDEFINED_CONST",
                            f"常量 {name} 未在规则 {owner} 的参数中定义", name=name)
        if t == "prev":
            if prev_rule is None:
                raise EvalError("PREV_OUTSIDE_FIXED",
                                "prev 节点只能出现在定点迭代的 next/until 中")
            return prev_val
        if t == "candidate":
            if self._candidate is _MISSING:
                raise EvalError("CANDIDATE_OUTSIDE_UNTIL",
                                "candidate 节点只能出现在定点迭代的 until 条件中")
            return self._candidate
        if t == "field":
            val = _dig(self.situation, node["path"])
            # missing numeric situation fields are treated as zero (no kills,
            # no combo...); booleans stay as False
            return 0 if val is None else val
        if t == "not":
            return not self.eval_node(node["arg"], prev_val, prev_rule, rule_id)
        if t == "neg":
            return -self._num(self.eval_node(node["arg"], prev_val, prev_rule, rule_id), rule_id)
        if t == "arith":
            a = self._num(self.eval_node(node["l"], prev_val, prev_rule, rule_id), rule_id)
            b = self._num(self.eval_node(node["r"], prev_val, prev_rule, rule_id), rule_id)
            op = node["op"]
            if op == "add":
                return a + b
            if op == "sub":
                return a - b
            if op == "mul":
                return a * b
            if op == "floor":
                # floor division accepting real operands -> integer iterate
                if b == 0:
                    raise EvalError("DIVISION_BY_ZERO", "计分表达式除以零", rule_id=rule_id)
                import math
                return int(math.floor(a / b))
            if op == "div":
                if b == 0:
                    raise EvalError("DIVISION_BY_ZERO", "计分表达式除以零", rule_id=rule_id)
                return a / b
            raise EvalError("BAD_OP", f"未知算术算符 {op}")
        if t == "cmp":
            a = self.eval_node(node["l"], prev_val, prev_rule, rule_id)
            b = self.eval_node(node["r"], prev_val, prev_rule, rule_id)
            op = node["op"]
            if op in ("eq", "neq"):
                # equality works across numbers, strings and booleans
                return (a == b) if op == "eq" else (a != b)
            try:
                a, b = self._num(a, rule_id), self._num(b, rule_id)
            except EvalError:
                a, b = str(a), str(b)
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if t == "logic":
            # evaluate both sides but keep PROPER boolean semantics (short-circuit
            # on real booleans only; a Python False from data must mean false)
            a = self.eval_node(node["l"], prev_val, prev_rule, rule_id)
            a = bool(a)
            if node["op"] == "and":
                if a is False:
                    return False
                return bool(self.eval_node(node["r"], prev_val, prev_rule, rule_id))
            if a is True:
                return True
            return bool(self.eval_node(node["r"], prev_val, prev_rule, rule_id))
        if t == "cond":
            if self.eval_node(node["if"], prev_val, prev_rule, rule_id):
                return self.eval_node(node["then"], prev_val, prev_rule, rule_id)
            return self.eval_node(node.get("else", {"t": "lit", "v": 0}),
                                  prev_val, prev_rule, rule_id)
        if t in ("min", "max", "sum"):
            vals = [self._num(self.eval_node(a, prev_val, prev_rule, rule_id), rule_id)
                    for a in node.get("args", [])]
            if not vals:
                return 0
            return min(vals) if t == "min" else max(vals) if t == "max" else sum(vals)
        if t == "ref":
            return self.eval_rule(node["rule"])
        if t == "fixed":
            return self._eval_fixed(node)
        raise EvalError("BAD_NODE", f"未知表达式类型 {t}")

    @staticmethod
    def _num(v: Any, rule_id: str) -> float | int:
        if isinstance(v, dict) and "value" in v:
            v = v["value"]  # unwrap fixed-point result in numeric context
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EvalError("TYPE_ERROR", f"规则 {rule_id} 需要数值，实际得到 {v!r}", rule_id=rule_id)
        return v

    def _eval_fixed(self, node: dict) -> Any:
        rule = node["recurrence_rule"]
        x = self.eval_node(node.get("start", {"t": "lit", "v": 0}),
                           None, None, rule_id=rule)
        max_iter = min(int(node.get("max_iter", 100)), self.budget)
        min_iter = int(node.get("min_iter", 0))
        iters = 0
        converged = False
        for it in range(1, max_iter + 1):
            self.spent += 1
            if self.spent > self.budget:
                raise BudgetExceeded(self.spent, self.budget, rule)
            cand = self.eval_node(node["next"], x, rule, rule_id=rule)
            # until compares the PREVIOUS iterate (prev) with the CANDIDATE
            self._candidate = cand
            done = bool(self.eval_node(node["until"], x, rule, rule_id=rule))
            self._candidate = _MISSING
            x = cand
            iters = it
            if done and it >= min_iter:
                converged = True
                break
        if not converged:
            raise EvalError(
                "BOUNDED_ITERATION_DIVERGES",
                f"规则 {rule} 的有界定点迭代在 {max_iter} 次内未收敛（最近一次值 {x!r}），"
                "拒绝使用未定值计分。",
                rule_id=rule, iterations=max_iter, last_value=x)
        result = {"value": x, "iterations": iters}
        self._fixed_iterations[(rule, x)] = iters
        return result

    # -- rule-level evaluation ---------------------------------------------

    def eval_rule(self, rule_id: str) -> Any:
        if rule_id in self._memo:
            return self._memo[rule_id]
        if rule_id in self._stack:
            raise EvalError("ILLEGAL_RECURSION",
                            f"规则 {rule_id} 在求值时直接/间接引用自身："
                            f"{' -> '.join(self._stack + [rule_id])}（缺少有界定点包裹）。",
                            cycle=self._stack + [rule_id])
        ver = self.frozen.get(rule_id)
        if ver is None:
            raise EvalError("RULE_NOT_IN_CLOSURE",
                            f"规则 {rule_id} 不在已冻结闭包中", rule_id=rule_id)
        expr = ver.get("expr")
        if expr is None:
            raise EvalError("RULE_NOT_EVALUABLE",
                            f"规则 {rule_id}（{ver['kind']}）没有可求值表达式，"
                            "只能作为事实/参数存在。", rule_id=rule_id)
        self._stack.append(rule_id)
        try:
            value = self.eval_node(expr, rule_id=rule_id)
        finally:
            self._stack.pop()
        # keep fixed-point metadata ({value, iterations}) in the memo so the
        # scoring run can surface convergence info; numeric contexts unwrap via
        # _num(). A rule that merely wraps a plain number is unwrapped here.
        if not (isinstance(value, dict) and "iterations" in value):
            value = self._num(value, rule_id)
        self._memo[rule_id] = value
        return value

    # -- full scoring run --------------------------------------------------

    def run(self, explicit_roots: list[str] | None = None) -> dict:
        scoring = [rid for rid, v in self.frozen.items() if v["kind"] == "scoring"]
        decorated = [(self.frozen[rid].get("params", {}).get("priority", 0), rid)
                     for rid in scoring]
        decorated.sort(key=lambda x: (-x[0], x[1]))

        contributions: list[dict] = []
        suppressed: list[dict] = []
        total = 0
        for _prio, rid in decorated:
            ver = self.frozen[rid]
            params = ver.get("params", {})
            gate = params.get("gate")
            fired = True
            gate_reason = "无条件触发"
            if gate:
                fired = bool(self.eval_node(gate, rule_id=rid))
                gate_reason = "条件成立" if fired else "条件不成立，跳过"
            if not fired:
                contributions.append({
                    "rule_id": rid, "version_label": ver["version_label"],
                    "fired": False, "value": 0, "reason": gate_reason,
                    "detail": None, "scored": True})
                continue
            value = self.eval_rule(rid)
            detail = None
            if isinstance(value, dict) and "iterations" in value:
                detail = {"iterations": value.get("iterations")}
                value = value["value"]
            else:
                iters = self._fixed_iterations.get((rid, self._num(value, rid)))
                if iters:
                    detail = {"iterations": iters}
            contributions.append({
                "rule_id": rid, "version_label": ver["version_label"],
                "fired": True, "value": self._num(value, rid), "reason": gate_reason,
                "group": params.get("conflict_group"),
                "priority": params.get("priority", 0), "detail": detail, "scored": True})

        # non-scoring explicit roots are evaluated for effect/diagnosis only
        # (e.g. bounded fixed-point probes such as the divergence fixture);
        # their values do NOT add to the score.
        extra_eval: list[dict] = []
        for rid in explicit_roots or []:
            if rid in self.frozen and self.frozen[rid]["kind"] != "scoring" \
                    and self.frozen[rid].get("expr") is not None:
                value = self.eval_rule(rid)
                detail = None
                if isinstance(value, dict) and "value" in value:
                    detail = {"iterations": value.get("iterations")}
                    value = value["value"]
                extra_eval.append({
                    "rule_id": rid, "version_label": self.frozen[rid]["version_label"],
                    "fired": True, "value": self._num(value, rid), "scored": False,
                    "reason": "非计分根规则：仅为诊断而显式求值，不计入总分",
                    "detail": detail})
        contributions.extend(extra_eval)

        # conflict arbitration within explicit groups: winner takes all
        groups: dict[str, list[dict]] = {}
        for c in contributions:
            if c["fired"] and c.get("group"):
                groups.setdefault(c["group"], []).append(c)
        for group, members in groups.items():
            if len(members) <= 1:
                continue
            winner = max(members, key=lambda c: (c["priority"], c["rule_id"]))
            for c in members:
                if c is not winner:
                    self.diagnostics.append({
                        "code": "CONDITION_CONFLICT_SUPPRESSED",
                        "severity": "warning",
                        "group": group,
                        "rule_id": c["rule_id"],
                        "winner": winner["rule_id"],
                        "message": f"道具与地图条件冲突：冲突组 {group} 中规则 {c['rule_id']} "
                                   f"与 {winner['rule_id']} 同时成立，按优先级仅保留 "
                                   f"{winner['rule_id']}（撤回 {c['value']} 分）。",
                    })
                    c["fired"] = False
                    c["reason"] = f"冲突组 {group} 被 {winner['rule_id']} 仲裁压制"
                    c["suppressed_by"] = winner["rule_id"]
                    suppressed.append({"rule_id": c["rule_id"], "value": c["value"],
                                       "by": winner["rule_id"]})
                    c["value"] = 0
        total = sum(c["value"] for c in contributions
                    if c["fired"] and c.get("scored", True))

        return {
            "score": total,
            "contributions": contributions,
            "suppressed": suppressed,
            "steps_used": self.spent,
            "budget": self.budget,
            "trace": self.trace,
            "diagnostics": self.diagnostics,
        }


def evaluate(package: dict, situation: dict, budget: int = DEFAULT_BUDGET) -> dict:
    if package.get("status") == "error":
        return {
            "score": None,
            "contributions": [],
            "steps_used": 0,
            "budget": budget,
            "diagnostics": [{
                "code": "PACKAGE_INVALID",
                "severity": "error",
                "message": "规则包未通过依赖校验，禁止求值；请先修复其诊断信息。",
            }] + package.get("diagnostics", []),
        }
    ev = Evaluator(package, situation, budget=budget)
    extra_roots = list(situation.get("_extra_roots") or []) + list(package.get("roots", []))
    try:
        return ev.run(explicit_roots=extra_roots)
    except (BudgetExceeded, EvalError) as exc:
        return {
            "score": None,
            "contributions": ev.trace,
            "steps_used": ev.spent,
            "budget": ev.budget,
            "diagnostics": ev.diagnostics + [exc.to_diag()],
        }
