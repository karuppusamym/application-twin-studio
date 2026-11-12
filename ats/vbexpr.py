"""A deliberately small evaluator for the VB.NET expressions found in UiPath XAML.

Supported: string / integer literals, True / False / Nothing, variables,
``+`` and ``&`` concatenation, integer ``+ -``, comparisons, And / Or / Not
(AndAlso / OrElse), parentheses, and the members .ToString .Trim .ToUpper
.ToLower .Length .Contains(x) .StartsWith(x) .Equals(x) plus
String.IsNullOrEmpty(x). Anything else raises ``Unsupported`` - the importer
turns that into a gap instead of guessing. Uploaded code is never executed.
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"]|"")*")|(?P<num>\d+)|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<op><>|<=|>=|[=<>+&().,\-]))')
_MEMBERS = {"tostring", "trim", "toupper", "tolower", "length", "contains", "startswith", "equals"}


class Unsupported(ValueError):
    pass


def tokenize(src: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise Unsupported(f"cannot parse expression near: {src[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


def unwrap(text: str | None) -> tuple[str, bool]:
    """'[expr]' -> ('expr', True); a plain literal attribute -> (text, False)."""
    if text is None:
        return "", False
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        return t[1:-1], True
    return text, False


class _Parser:
    def __init__(self, tokens: list, variables: dict | None, collect: set | None):
        self.t, self.i, self.vars, self.collect = tokens, 0, variables, collect

    def peek(self) -> tuple[str, str]:
        return self.t[self.i] if self.i < len(self.t) else ("end", "")

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def word(self, *names: str) -> bool:
        k, v = self.peek()
        if k == "id" and v.lower() in names:
            self.i += 1
            return True
        return False

    def op(self, *ops: str) -> str | None:
        k, v = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        return None

    def expr(self) -> Any:
        left = self.and_()
        while self.word("or", "orelse"):
            right = self.and_()
            left = bool(left) or bool(right)
        return left

    def and_(self) -> Any:
        left = self.not_()
        while self.word("and", "andalso"):
            right = self.not_()
            left = bool(left) and bool(right)
        return left

    def not_(self) -> Any:
        if self.word("not"):
            return not bool(self.not_())
        return self.compare()

    def compare(self) -> Any:
        left = self.concat()
        op = self.op("=", "<>", "<", ">", "<=", ">=")
        if op is None:
            return left
        right = self.concat()
        if self.vars is None:
            return True
        if op in ("=", "<>"):
            same = (left == right) if type(left) is type(right) else (str(left) == str(right))
            return same if op == "=" else not same
        a, b = int(left), int(right)
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    def concat(self) -> Any:
        left = self.primary()
        while True:
            op = self.op("+", "&", "-")
            if op is None:
                return left
            right = self.primary()
            if self.vars is None:
                continue
            if op == "-":
                left = int(left) - int(right)
            elif op == "+" and isinstance(left, int) and isinstance(right, int) and not isinstance(left, bool):
                left = left + right
            else:
                left = _s(left) + _s(right)

    def primary(self) -> Any:
        kind, val = self.take()
        if kind == "str":
            value: Any = val[1:-1].replace('""', '"')
        elif kind == "num":
            value = int(val)
        elif kind == "op" and val == "(":
            value = self.expr()
            if not self.op(")"):
                raise Unsupported("missing )")
        elif kind == "id":
            low = val.lower()
            if low == "true":
                value = True
            elif low == "false":
                value = False
            elif low == "nothing":
                value = None
            elif low == "string" and self.op("."):
                _, fn = self.take()
                if fn.lower() != "isnullorempty" or not self.op("("):
                    raise Unsupported(f"String.{fn} is not supported")
                arg = self.expr()
                self.op(")")
                value = arg in (None, "")
            else:
                if self.collect is not None:
                    self.collect.add(val)
                if self.vars is None:
                    value = ""
                elif val in self.vars:
                    value = self.vars[val]
                else:
                    raise Unsupported(f"unknown variable {val}")
        else:
            raise Unsupported(f"unexpected token {val!r}")
        while self.op("."):
            _, member = self.take()
            m = member.lower()
            if m not in _MEMBERS:
                raise Unsupported(f"member .{member} is not supported")
            args = []
            if self.op("("):
                while not self.op(")"):
                    args.append(self.expr())
                    self.op(",")
            if self.vars is None:
                continue
            s = _s(value)
            value = {"tostring": lambda: s, "trim": lambda: s.strip(), "toupper": lambda: s.upper(),
                     "tolower": lambda: s.lower(), "length": lambda: len(s),
                     "contains": lambda: _s(args[0]) in s, "startswith": lambda: s.startswith(_s(args[0])),
                     "equals": lambda: s == _s(args[0])}[m]()
        return value


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def evaluate(src: str, variables: dict) -> Any:
    p = _Parser(tokenize(src), variables, None)
    value = p.expr()
    if p.peek()[0] != "end":
        raise Unsupported(f"unexpected trailing input in {src!r}")
    return value


def analyse(src: str) -> set[str]:
    """Static check: returns the variables used, raises Unsupported if not parseable."""
    names: set[str] = set()
    p = _Parser(tokenize(src), None, names)
    p.expr()
    if p.peek()[0] != "end":
        raise Unsupported(f"unexpected trailing input in {src!r}")
    return names


def concat_parts(src: str) -> list[tuple[str, str]] | None:
    """For URL mapping: 'a + "/x/" + b' -> [('var','a'), ('lit','/x/'), ('var','b')]; None if not a plain concat."""
    try:
        toks = tokenize(src)
    except Unsupported:
        return None
    parts, expect_value = [], True
    for kind, val in toks:
        if expect_value and kind == "str":
            parts.append(("lit", val[1:-1].replace('""', '"')))
        elif expect_value and kind == "id":
            parts.append(("var", val))
        elif not expect_value and kind == "op" and val in ("+", "&"):
            pass
        else:
            return None
        expect_value = not expect_value
    return parts
