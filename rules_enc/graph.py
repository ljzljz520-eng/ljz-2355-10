# -*- coding: utf-8 -*-
"""依赖校验与强连通分量(SCC)分析。

环语义区分:
* 无环引用 —— 按拓扑序一次性求值。
* 有界求值(bounded fixpoint) —— SCC 内每条规则都显式标注
  fixpoint{max_iter, tol, init}，求值器做同步迭代，收敛即返回；
  超过 max_iter 仍不收敛 => BudgetExceeded（示例计算超预算，可诊断）。
* 非法递归 —— 环中存在未标注有界语义的规则 => 编译期 IllegalRecursion，
  报告具体环路径，该补丁包不可发布/不可用。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Set


class DependencyError(Exception):
    """依赖校验失败（引用缺失等）。"""


class IllegalRecursion(Exception):
    def __init__(self, cycle, unbounded):
        self.cycle = cycle
        self.unbounded = unbounded
        path = " -> ".join(cycle + [cycle[0]])
        who = ", ".join(sorted(unbounded))
        super().__init__(
            f"非法递归: 规则环 [{path}] 中 {who} 未声明有界求值语义"
            "（fixpoint: max_iter/tol/init）；有界环才允许同步迭代求解。"
        )


@dataclass
class EvalGroup:
    """求值计划中的一个组：单条无环规则，或一个有界不动点 SCC。"""
    rule_ids: List[str]
    bounded: bool = False
    max_iter: int = 0
    tol: float = 0.0
    inits: Dict[str, float] = field(default_factory=dict)


def tarjan(nodes: Set[str], edges: Dict[str, List[str]]):
    """返回 SCC 列表（按拓扑序：被依赖者在前）。"""
    index = {}
    low = {}
    onstack = set()
    stack = []
    counter = [0]
    sccs = []

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        onstack.add(v)
        for w in edges.get(v, []):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in onstack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                onstack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    import sys
    sys.setrecursionlimit(10000)
    for v in sorted(nodes):
        if v not in index:
            strong(v)

    # Tarjan 的完成顺序：无出边（被依赖）的 SCC 先出列 => 被依赖者在前
    ordered = list(sccs)

    # 组内按 id 排序，保证跨进程求值顺序确定（缓存身份相关）
    result = []
    for comp in ordered:
        members = sorted(comp)
        result.append(members)
    return result


def build_plan(rules: Dict[str, dict]):
    """rules: rule_id -> {'refs': [...], 'meta': {...}}。

    返回 List[EvalGroup]，依赖在前；并做引用校验与环合法性校验。
    """
    ids = set(rules)
    edges = {}
    for rid, r in rules.items():
        missing = [x for x in r["refs"] if x not in ids]
        if missing:
            raise DependencyError(f"规则 {rid} 引用了不存在的规则: {', '.join(missing)}")
        edges[rid] = list(r["refs"])

    components = tarjan(ids, edges)
    plan: List[EvalGroup] = []
    for comp in components:
        if len(comp) == 1:
            only = comp[0]
            if only not in edges.get(only, []):
                plan.append(EvalGroup(rule_ids=[only]))
                continue
        # 这是一个真正的环（含自环）：必须全体声明一致的有界语义
        bounded_metas = []
        unbounded = []
        for rid in comp:
            fp = rules[rid].get("meta", {}).get("fixpoint")
            if fp and "max_iter" in fp and "init" in fp:
                bounded_metas.append((rid, fp))
            else:
                unbounded.append(rid)
        if unbounded:
            raise IllegalRecursion(comp, unbounded)

        max_iters = {fp["max_iter"] for _, fp in bounded_metas}
        if len(max_iters) != 1:
            raise IllegalRecursion(
                comp,
                [f"{rid}(max_iter 不一致)"])
        max_iter = max_iters.pop()
        tol = bounded_metas[0][1].get("tol", 1e-9)
        inits = {rid: float(fp["init"]) for rid, fp in bounded_metas}
        plan.append(EvalGroup(
            rule_ids=sorted(comp), bounded=True,
            max_iter=max_iter, tol=tol, inits=inits))
    return plan
