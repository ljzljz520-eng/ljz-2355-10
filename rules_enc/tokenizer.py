# -*- coding: utf-8 -*-
"""规则 DSL 词法分析器。"""
from dataclasses import dataclass


class RuleSyntaxError(Exception):
    pass


@dataclass
class Token:
    kind: str   # NUMBER / STRING / ID / OP / KEYWORD
    value: str
    pos: int


_SINGLE = set("+-*/%(),?:!<>=")
_KEYWORDS = {"and", "or", "not", "true", "false", "input", "ref"}


def tokenize(src: str):
    toks = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        # 注释：# 到行尾
        if c == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (src[j].isdigit() or src[j] == "."):
                if src[j] == ".":
                    if seen_dot:
                        raise RuleSyntaxError(f"位置 {j}: 数字中出现多个小数点")
                    seen_dot = True
                j += 1
            toks.append(Token("NUMBER", src[i:j], i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            kind = "KEYWORD" if word in _KEYWORDS else "ID"
            toks.append(Token(kind, word, i))
            i = j
            continue
        if c in ("'", '"'):
            quote = c
            j = i + 1
            buf = []
            while j < n and src[j] != quote:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            if j >= n:
                raise RuleSyntaxError(f"位置 {i}: 字符串未闭合")
            toks.append(Token("STRING", "".join(buf), i))
            i = j + 1
            continue
        if src.startswith(("==", "!=", "<=", ">="), i):
            toks.append(Token("OP", src[i:i + 2], i))
            i += 2
            continue
        if c in _SINGLE:
            toks.append(Token("OP", c, i))
            i += 1
            continue
        raise RuleSyntaxError(f"位置 {i}: 无法识别的字符 {c!r}")
    toks.append(Token("EOF", "", n))
    return toks
