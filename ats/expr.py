"""Allowlisted expression and condition language used by ATS-Spec.

Nothing here evaluates code. A value is either a literal, a ``$ref.path``
reference into the operation context, a ``"text ${ref} text"`` template, or a
small set of ``{$fn: args}`` functions. Conditions are nested dicts built from
a fixed operator set. Unknown operators are rejected at spec validation time.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

FUNCTIONS = {"$concat", "$add", "$sub", "$mul", "$coalesce", "$count", "$upper", "$trim", "$str"}
CONDITIONS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "exists", "empty", "not", "all", "any"}
_TEMPLATE = re.compile(r"\$\{([^}]+)\}")


class ExprError(ValueError):
    pass


def lookup(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None


def _text(v: Any) -> str:
    return "" if v is None else str(v)


def resolve(value: Any, ctx: dict) -> Any:
    """Resolve a spec value against the context."""
    if isinstance(value, str):
        if value.startswith("$") and not value.startswith("${") and " " not in value:
            return lookup(ctx, value[1:])
        if "${" in value:
            return _TEMPLATE.sub(lambda m: _text(lookup(ctx, m.group(1).strip())), value)
        return value
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        if len(value) == 1:
            (key, arg), = value.items()
            if isinstance(key, str) and key.startswith("$"):
                return _call(key, arg, ctx)
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


def _call(fn: str, arg: Any, ctx: dict) -> Any:
    if fn not in FUNCTIONS:
        raise ExprError(f"unknown function {fn}")
    args = resolve(arg if isinstance(arg, list) else [arg], ctx)
    if fn == "$concat":
        return "".join(_text(a) for a in args)
    if fn == "$coalesce":
        return next((a for a in args if a not in (None, "")), None)
    if fn == "$count":
        return len(args[0]) if isinstance(args[0], (list, dict, str)) else 0
    if fn == "$upper":
        return _text(args[0]).upper()
    if fn == "$trim":
        return _text(args[0]).strip()
    if fn == "$str":
        return _text(args[0])
    nums = [_dec(a) for a in args]
    if any(n is None for n in nums):
        raise ExprError(f"{fn} needs numeric arguments, got {args!r}")
    out = nums[0]
    for n in nums[1:]:
        out = out + n if fn == "$add" else out - n if fn == "$sub" else out * n
    return str(out)  # decimals travel as strings: no float rounding of money


def _same(a: Any, b: Any) -> bool:
    # Numeric comparison only when one side is a real number; two strings
    # compare as strings so identifiers like "0012" never equal "12".
    if isinstance(a, (int, float, Decimal)) or isinstance(b, (int, float, Decimal)):
        da, db = _dec(a), _dec(b)
        if da is not None and db is not None:
            return da == db
    return a == b


def test(cond: Any, ctx: dict) -> bool:
    """Evaluate a condition dict. ``None``/empty means true."""
    if cond in (None, {}, True):
        return True
    if cond is False:
        return False
    if not isinstance(cond, dict) or len(cond) != 1:
        raise ExprError(f"condition must be a single-operator mapping: {cond!r}")
    (op, arg), = cond.items()
    if op not in CONDITIONS:
        raise ExprError(f"unknown condition operator {op}")
    if op == "all":
        return all(test(c, ctx) for c in arg)
    if op == "any":
        return any(test(c, ctx) for c in arg)
    if op == "not":
        return not test(arg, ctx)
    if op == "exists":
        return resolve(arg, ctx) is not None
    if op == "empty":
        return resolve(arg, ctx) in (None, "", [], {})
    a, b = resolve(arg[0], ctx), resolve(arg[1], ctx)
    if op == "eq":
        return _same(a, b)
    if op == "ne":
        return not _same(a, b)
    if op == "in":
        return any(_same(a, x) for x in (b or []))
    da, db = _dec(a), _dec(b)
    if da is None or db is None:
        return False
    return {"gt": da > db, "gte": da >= db, "lt": da < db, "lte": da <= db}[op]


def check(value: Any, where: str, errors: list[str]) -> None:
    """Static validation: every function used is on the allowlist."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("$") and k not in FUNCTIONS:
                errors.append(f"{where}: unknown function {k}")
            check(v, where, errors)
    elif isinstance(value, list):
        for v in value:
            check(v, where, errors)


def check_cond(cond: Any, where: str, errors: list[str]) -> None:
    if cond in (None, {}, True, False):
        return
    if not isinstance(cond, dict) or len(cond) != 1:
        errors.append(f"{where}: condition must have exactly one operator")
        return
    (op, arg), = cond.items()
    if op not in CONDITIONS:
        errors.append(f"{where}: unknown condition operator '{op}'")
    elif op in ("all", "any"):
        for c in arg or []:
            check_cond(c, where, errors)
    elif op == "not":
        check_cond(arg, where, errors)
    elif op not in ("exists", "empty") and (not isinstance(arg, list) or len(arg) != 2):
        errors.append(f"{where}: '{op}' needs two operands")
