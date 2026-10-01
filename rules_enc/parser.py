# -*- coding: utf-8 -*-
"""规则 DSL 递归下降解析器。"""
from . import ast_nodes as ast
from .tokenizer import tokenize, RuleSyntaxError, Token


class Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0

    # ---- 基础工具 ----
    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def accept(self, kind, value=None):
        t = self.peek()
        if t.kind == kind and (value is None or t.value == value):
            return self.next()
        return None

    def expect(self, kind, value=None):
        t = self.peek()
        if t.kind != kind or (value is not None and t.value != value):
            want = value if value is not None else kind
            raise RuleSyntaxError(f"位置 {t.pos}: 期望 {want}，实际为 {t.value!r}")
        return self.next()

    # ---- 语法分层 ----
    def parse(self):
        node = self.ternary()
        if self.peek().kind != "EOF":
            t = self.peek()
            raise RuleSyntaxError(f"位置 {t.pos}: 多余的 token {t.value!r}")
        return node

    def ternary(self):
        cond = self.logical()
        if self.accept("OP", "?"):
            then = self.ternary()
            self.expect("OP", ":")
            els = self.ternary()
            return ast.Call(token="?:", pos=cond.pos, name="if", args=[cond, then, els])
        return cond

    def logical(self):
        left = self.comparison()
        while True:
            t = self.peek()
            if t.kind == "KEYWORD" and t.value in ("and", "or"):
                self.next()
                right = self.comparison()
                left = ast.BinOp(token=t.value, pos=t.pos, op=t.value, left=left, right=right)
            else:
                return left

    def comparison(self):
        left = self.addsub()
        while True:
            t = self.peek()
            if t.kind == "OP" and t.value in ("==", "!=", "<", "<=", ">", ">="):
                self.next()
                right = self.addsub()
                left = ast.BinOp(token=t.value, pos=t.pos, op=t.value, left=left, right=right)
            else:
                return left

    def addsub(self):
        left = self.muldiv()
        while True:
            t = self.peek()
            if t.kind == "OP" and t.value in ("+", "-"):
                self.next()
                right = self.muldiv()
                left = ast.BinOp(token=t.value, pos=t.pos, op=t.value, left=left, right=right)
            else:
                return left

    def muldiv(self):
        left = self.unary()
        while True:
            t = self.peek()
            if t.kind == "OP" and t.value in ("*", "/", "%"):
                self.next()
                right = self.unary()
                left = ast.BinOp(token=t.value, pos=t.pos, op=t.value, left=left, right=right)
            else:
                return left

    def unary(self):
        t = self.peek()
        if (t.kind == "OP" and t.value == "-") or (t.kind == "KEYWORD" and t.value == "not") \
                or (t.kind == "OP" and t.value == "!"):
            self.next()
            return ast.Unary(token=t.value, pos=t.pos, op=t.value, operand=self.unary())
        return self.postfix()

    def postfix(self):
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "NUMBER":
            self.next()
            return ast.Number(token=t.value, pos=t.pos, value=float(t.value))
        if t.kind == "KEYWORD" and t.value in ("true", "false"):
            self.next()
            return ast.Bool(token=t.value, pos=t.pos, value=(t.value == "true"))
        if t.kind == "KEYWORD" and t.value == "input":
            self.next()
            self.expect("OP", "(")
            key = self.primary()
            if not isinstance(key, ast.String):
                raise RuleSyntaxError(f"位置 {key.pos}: input 的键必须是字符串字面量")
            default = None
            if self.accept("OP", ","):
                default = self.ternary()
            self.expect("OP", ")")
            return ast.Input(token="input", pos=t.pos, key=key, default=default)
        if t.kind == "KEYWORD" and t.value == "ref":
            self.next()
            self.expect("OP", "(")
            name = self.primary()
            if not isinstance(name, ast.String):
                raise RuleSyntaxError(f"位置 {name.pos}: ref 的规则名必须是字符串字面量")
            self.expect("OP", ")")
            return ast.Ref(token="ref", pos=t.pos, name=name)
        if t.kind == "ID":
            name = self.next().value
            self.expect("OP", "(")
            args = []
            if not self.accept("OP", ")"):
                args.append(self.ternary())
                while self.accept("OP", ","):
                    args.append(self.ternary())
                self.expect("OP", ")")
            return ast.Call(token=name, pos=t.pos, name=name, args=args)
        if t.kind == "STRING":
            self.next()
            return ast.String(token=t.value, pos=t.pos, value=t.value)
        if t.kind == "OP" and t.value == "":
            self.next()
            return self.primary()
        if self.accept("OP", "("):
            node = self.ternary()
            self.expect("OP", ")")
            return node
        raise RuleSyntaxError(f"位置 {t.pos}: 意外的 token {t.value!r}")


def parse(src: str) -> ast.Node:
    return Parser(src).parse()


def collect_refs(node, out=None):
    """静态收集表达式中的规则引用（用于依赖校验）。"""
    if out is None:
        out = []
    if isinstance(node, ast.Ref):
        out.append(node.name.value)
    for child in getattr(node, "__dict__", {}).values():
        if isinstance(child, ast.Node):
            collect_refs(child, out)
        elif isinstance(child, list):
            for x in child:
                if isinstance(x, ast.Node):
                    collect_refs(x, out)
    return out


def collect_inputs(node, out=None):
    if out is None:
        out = []
    if isinstance(node, ast.Input):
        out.append(node.key.value)
    for child in getattr(node, "__dict__", {}).values():
        if isinstance(child, ast.Node):
            collect_inputs(child, out)
        elif isinstance(child, list):
            for x in child:
                if isinstance(x, ast.Node):
                    collect_inputs(x, out)
    return out
