"""Static analysis of the rule DSL.

Walks expression trees, extracts rule-reference edges, and classifies cycles:

* a rule referencing *itself* inside a ``fixed`` (bounded fixed-point) construct
  is legal: bounded iteration semantics with an ``until`` convergence test and
  a hard iteration cap;
* every other cycle is illegal recursion (no well-defined finite evaluation),
  with one exception reported separately: a multi-rule cycle whose edges are all
  bounded references. The evaluator only implements single-rule fixed points,
  so that gets a distinct ``BOUNDED_CYCLE_UNSUPPORTED`` diagnosis.
"""
from __future__ import annotations

from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Expression tree traversal
# ---------------------------------------------------------------------------

_CHILD_KEYS = {
    "lit": (), "field": (), "const": (),
    "prev": (), "candidate": (),
    "not": ("arg",),
    "neg": ("arg",),
    "ref": (),
    "cmp": ("l", "r"),
    "arith": ("l", "r"),
    "logic": ("l", "r"),
    "cond": ("if", "then", "else"),
    "min": ("args",), "max": ("args",), "sum": ("args",),
    "fixed": ("start", "next"),
}


def walk(node: Any) -> Iterator[dict]:
    """Yield every expression node (dicts with a ``t`` tag)."""
    if isinstance(node, dict) and "t" in node:
        yield node
        if node["t"] in ("min", "max", "sum"):
            for a in node.get("args", []):
                yield from walk(a)
        elif node["t"] == "fixed":
            yield from walk(node.get("start"))
            # next is walked separately with fixed context by callers; walk it
            # plainly here too so generic traversal sees every node
            yield from walk(node.get("next"))
        else:
            for key in _CHILD_KEYS.get(node["t"], ()):
                child = node.get(key)
                if isinstance(child, list):
                    for a in child:
                        yield from walk(a)
                else:
                    yield from walk(child)
    elif isinstance(node, list):
        for a in node:
            yield from walk(a)


def extract_edges(expr: Any) -> list[dict]:
    """Return reference edges inside one expression.

    Edge: {"target": rule_id, "bounded": bool, "inside_fixed": bool}
    """
    edges: list[dict] = []

    def rec(n: Any, fixed_rule: str | None) -> None:
        if not isinstance(n, dict) or "t" not in n:
            if isinstance(n, list):
                for a in n:
                    rec(a, fixed_rule)
            return
        if n["t"] == "prev":
            # bounded self reference of the enclosing fixed-point rule
            if fixed_rule is not None:
                edges.append({"target": fixed_rule, "bounded": True, "inside_fixed": True})
            return
        if n["t"] == "ref":
            target = n["rule"]
            edges.append({"target": target, "bounded": False,
                          "inside_fixed": fixed_rule is not None})
            return
        if n["t"] == "fixed":
            # next/until run in the fixed rule's bounded-iteration scope;
            # start does not
            rec(n.get("start"), fixed_rule)
            rec(n.get("next"), n.get("recurrence_rule"))
            rec(n.get("until"), n.get("recurrence_rule"))
            return
        for key in _CHILD_KEYS.get(n["t"], ()):
            child = n.get(key)
            if isinstance(child, list):
                for a in child:
                    rec(a, fixed_rule)
            else:
                rec(child, fixed_rule)

    rec(expr, None)
    return edges


def direct_ref_targets(expr: Any) -> list[str]:
    """Rule ids referenced (excluding bounded prev references)."""
    return [e["target"] for e in extract_edges(expr) if not e["bounded"]]


# ---------------------------------------------------------------------------
# Tarjan strongly connected components
# ---------------------------------------------------------------------------

def tarjan_scc(nodes: set[str], adj: dict[str, list[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    result: list[list[str]] = []

    def strong(v: str) -> None:
        nonlocal counter
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    for v in sorted(nodes):
        if v not in index:
            strong(v)
    return result


def classify_cycles(adj: dict[str, list[str]], bounded_edges: set[tuple[str, str]]) -> list[dict]:
    """Inspect every cyclic SCC. Returns diagnostics (empty == acyclic-ok)."""
    nodes = set(adj) | {w for ws in adj.values() for w in ws}
    diagnostics: list[dict] = []
    for comp in tarjan_scc(nodes, adj):
        comp_set = set(comp)
        cycle_edges = [
            (u, v) for u in comp for v in adj.get(u, [])
            if v in comp_set and (u == v or True)
        ]
        # keep only edges that actually participate in a cycle (self loops do)
        internal = [(u, v) for (u, v) in cycle_edges if u == v or len(comp) > 1]
        if not internal:
            continue
        if len(comp) == 1 and all(e in bounded_edges for e in internal):
            continue  # legal bounded self fixed-point
        if all(e in bounded_edges for e in internal):
            diagnostics.append({
                "code": "BOUNDED_CYCLE_UNSUPPORTED",
                "severity": "error",
                "rules": sorted(comp),
                "cycle": [list(e) for e in internal],
                "message": "有界循环由多条规则组成，当前求值器仅支持单规则定点迭代；无法确定求值顺序。",
            })
        else:
            unbounded = [list(e) for e in internal if e not in bounded_edges]
            diagnostics.append({
                "code": "ILLEGAL_RECURSION",
                "severity": "error",
                "rules": sorted(comp),
                "cycle": [list(e) for e in internal],
                "unbounded_edges": unbounded,
                "message": f"规则间存在无界递归引用 {' -> '.join(sorted(comp) + [sorted(comp)[0]])}："
                           "引用没有包裹在带收敛条件与迭代上限的定点构造中，拒绝编译。",
            })
    return diagnostics
