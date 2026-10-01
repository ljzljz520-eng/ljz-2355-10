# -*- coding: utf-8 -*-
"""规则 DSL 的 AST 节点定义。

表达式语法（后续在 parser 中落实优先级）:
    expr   := ternary
    ternary:= logical ('?' expr ':' expr)?
    logical := comparison (('and'|'or') comparison)*
    comparison := addsub (('=='|'!='|'<'|'<='|'>'|'>=') addsub)*
    addsub := muldiv (('+'|'-') muldiv)*
    muldiv := unary (('*'|'/'|'%') unary)*
    unary  := ('-'|'not'|'!') unary | postfix
    postfix:= primary ('(' arglist ')')?
    primary:= number | string | 'true' | 'false'
            | 'input' '(' string (',' expr)? ')'
            | 'ref' '(' string ')'
            | '(' expr ')'
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Node:
    token: str = ""          # 源片段，便于报告展示
    pos: int = 0             # 源码位置


@dataclass
class Number(Node):
    value: float = 0.0


@dataclass
class String(Node):
    value: str = ""


@dataclass
class Bool(Node):
    value: bool = False


@dataclass
class Input(Node):
    """input('key') 或 input('key', default) —— 局面输入。"""
    key: Node = None
    default: Optional[Node] = None


@dataclass
class Ref(Node):
    """ref('rule_id') —— 规则间引用；依赖图据此建立。"""
    name: Node = None


@dataclass
class Call(Node):
    """内置函数调用，如 if/and/or/sum/max/min/floor/abs/iter_drag。"""
    name: str = ""
    args: List[Node] = field(default_factory=list)


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None


@dataclass
class BinOp(Node):
    op: str = ""
    left: Node = None
    right: Node = None
