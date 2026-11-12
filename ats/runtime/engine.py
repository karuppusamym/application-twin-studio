"""Deterministic behaviour engine: one state model behind every interface.

HTTP routes, web pages and terminal screens all end up in ``TwinApp.invoke``.
An operation is a list of allowlisted steps executed inside one transaction
together with its idempotency receipt and outbox events.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .. import expr
from ..spec import BUILTIN_ERRORS, digest
from .store import Store, StoreError


@dataclass
class Result:
    status: int
    body: Any = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str | None:
        return self.body.get("code") if not self.ok and isinstance(self.body, dict) else None


class TwinError(Exception):
    def __init__(self, code: str, message: str | None = None, field_errors: list | None = None):
        super().__init__(code)
        self.code, self.message, self.field_errors = code, message, field_errors or []


class _Respond(Exception):
    def __init__(self, result: Result):
        self.result = result


def _compile_route(route: str) -> tuple[str, re.Pattern]:
    method, path = route.split(" ", 1)
    pattern = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", r"(?P<\1>[^/]+)", path.rstrip("/") or "/")
    return method, re.compile(f"^{pattern}/?$")


class TwinApp:
    def __init__(self, spec: dict, store: Store, env: "Any"):
        self.spec, self.store, self.env = spec, store, env
        self.app_id = spec["application_id"]
        self.ops = {o["id"]: o for o in spec["operations"]}
        self.interfaces = {i["id"]: i for i in spec["interfaces"]}
        self.errors = {**BUILTIN_ERRORS, **(spec.get("errors") or {})}
        self.routes = [(*_compile_route(o["route"]), o) for o in spec["operations"] if o.get("route")]

    # -- routing ----------------------------------------------------------
    def match(self, method: str, path: str) -> tuple[dict, dict] | None:
        for m, rx, op in self.routes:
            if m == method.upper():
                hit = rx.match(path)
                if hit:
                    return op, hit.groupdict()
        return None

    def error_result(self, code: str, message: str | None = None, field_errors: list | None = None) -> Result:
        meta = self.errors.get(code, {"status": 500, "message": code})
        return Result(
            meta.get("status", 500),
            {
                "code": code,
                "message": message or meta.get("message", code),
                "trace_id": self.env.next_trace(),
                "retryable": bool(meta.get("retryable", False)),
                "field_errors": field_errors or [],
            },
        )

    # -- invocation -------------------------------------------------------
    def invoke(self, op_id: str, path: dict | None = None, query: dict | None = None, body: Any = None,
               headers: dict | None = None, caller: str = "anonymous") -> Result:
        op = self.ops.get(op_id)
        if op is None:
            return self.error_result("UNMODELED_OPERATION", f"{self.app_id} has no operation {op_id}")
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        fault = self.env.faults.take(self.app_id, op_id)
        self.env.record_call(self.app_id, op_id, caller, fault)
        if fault and fault["type"] in ("timeout", "error", "unavailable"):
            code = {"timeout": "DEPENDENCY_TIMEOUT", "unavailable": "DEPENDENCY_UNAVAILABLE"}.get(fault["type"], "INJECTED_FAULT")
            return self.error_result(code, f"Injected {fault['type']} (scenario fault profile)")

        ctx = {"path": path or {}, "query": query or {}, "body": body if body is not None else {},
               "header": headers, "now": self.env.clock, "caller": caller}
        key = None
        try:
            schema = op.get("input_schema")
            if schema:
                problems = sorted(jsonschema.Draft202012Validator(schema).iter_errors(ctx["body"]), key=str)
                if problems:
                    raise TwinError("VALIDATION_ERROR", field_errors=[
                        {"field": ".".join(map(str, p.path)) or "(body)", "message": p.message} for p in problems])
            idem = op.get("idempotency")
            request_hash = digest({"path": ctx["path"], "body": ctx["body"]})
            if idem:
                key = headers.get("idempotency-key")
                if not key and idem.get("required"):
                    raise TwinError("IDEMPOTENCY_KEY_REQUIRED")
                if key:
                    prior = self.store.receipt(op_id, caller, key)
                    if prior:
                        if prior["request_hash"] != request_hash:
                            raise TwinError("IDEMPOTENCY_KEY_REUSED")
                        import json
                        return Result(prior["status"], json.loads(prior["body"]), {"Idempotent-Replay": "true"})
            self.store.begin()
            try:
                result = self._run(op.get("steps") or [], ctx)
                if key:
                    self.store.save_receipt(op_id, caller, key, request_hash, result.status, result.body)
                self.store.commit()
            except BaseException:
                self.store.rollback()
                raise
        except TwinError as exc:
            return self.error_result(exc.code, exc.message, exc.field_errors)
        except (StoreError, expr.ExprError) as exc:
            return self.error_result("VALIDATION_ERROR", str(exc))
        if fault and fault["type"] == "timeout_after_commit":
            # The effect is committed but the caller never sees the answer.
            return self.error_result("DEPENDENCY_TIMEOUT", "Injected timeout after commit")
        return result

    def _run(self, steps: list, ctx: dict) -> Result:
        try:
            self._steps(steps, ctx)
        except _Respond as r:
            return r.result
        return Result(204, None)

    def _steps(self, steps: list, ctx: dict) -> None:
        for step in steps:
            (kind, body), = step.items()
            getattr(self, f"_step_{kind}")(body or {}, ctx)

    # -- step implementations (the allowlist) -----------------------------
    def _step_load(self, s: dict, ctx: dict) -> None:
        row = self.store.get(s["entity"], expr.resolve(s["key"], ctx))
        if row is None and s.get("not_found"):
            raise TwinError(s["not_found"])
        ctx[s.get("as", s["entity"])] = row

    def _step_query(self, s: dict, ctx: dict) -> None:
        where = {f: expr.resolve(v, ctx) for f, v in (s.get("where") or {}).items()}
        ctx[s.get("as", s["entity"])] = self.store.query(s["entity"], where, s.get("order_by"))

    def _step_require(self, s: dict, ctx: dict) -> None:
        if not expr.test(s.get("that"), ctx):
            raise TwinError(s.get("error", "STATE_CONFLICT"), expr.resolve(s.get("message"), ctx))

    def _step_set(self, s: dict, ctx: dict) -> None:
        ctx.update(expr.resolve(s, ctx))

    def _step_if(self, s: dict, ctx: dict) -> None:
        self._steps(s.get("then" if expr.test(s.get("when"), ctx) else "else") or [], ctx)

    def _step_decide(self, s: dict, ctx: dict) -> None:
        for rule in s.get("rules") or []:
            if expr.test(rule.get("when"), ctx):
                ctx.update(expr.resolve(rule.get("set") or {}, ctx))
                ctx["_rule"] = rule.get("id")
                return
        ctx.update(expr.resolve(s.get("default") or {}, ctx))
        ctx["_rule"] = "default"

    def _step_call(self, s: dict, ctx: dict) -> None:
        tolerate = set(s.get("tolerate") or [])
        res = None
        for _attempt in range(1 + int(s.get("retry", 0))):
            res = self.env.call(
                self.app_id, s["dependency"], s["operation"],
                path=expr.resolve(s.get("path") or {}, ctx), query=expr.resolve(s.get("query") or {}, ctx),
                body=expr.resolve(s.get("body"), ctx),
            )
            if res.ok or res.status < 500:
                break
        name = s.get("as", s["operation"])
        if res.ok:
            ctx[name] = res.body
        elif res.status in tolerate:
            ctx[name] = {"_error": res.code, "_status": res.status}
        else:
            raise TwinError(s.get("error", "DEPENDENCY_UNAVAILABLE"),
                            f"{s['dependency']}.{s['operation']} failed with {res.status} {res.code}")

    def _step_insert(self, s: dict, ctx: dict) -> None:
        entity = s["entity"]
        values = expr.resolve(s.get("values") or {}, ctx)
        gen = s.get("id")
        if gen:
            n = self.store.next_seq(gen.get("sequence", entity), int(gen.get("start", 1)))
            values[self.spec["entities"][entity]["key"]] = f"{gen.get('prefix', '')}{n:0{int(gen.get('pad', 1))}d}"
        try:
            row = self.store.insert(entity, values)
        except sqlite3.IntegrityError as exc:
            raise TwinError(s.get("conflict", "STATE_CONFLICT"), f"{entity} already exists") from exc
        ctx[s.get("as", entity)] = row

    def _step_update(self, s: dict, ctx: dict) -> None:
        entity = s["entity"]
        ent = self.spec["entities"][entity]
        key = expr.resolve(s["key"], ctx)
        row = self.store.get(entity, key)
        if row is None:
            raise TwinError(s.get("not_found", "NOT_FOUND"))
        expected = expr.resolve(s.get("expect_rev"), ctx)
        if expected not in (None, "") and str(expected) != str(row["_rev"]):
            raise TwinError("REVISION_CONFLICT")
        values = expr.resolve(s.get("set") or {}, ctx)
        sf = ent.get("state_field")
        if sf and sf in values and values[sf] != row[sf]:
            if values[sf] not in (ent.get("transitions") or {}).get(row[sf], []):
                raise TwinError("STATE_CONFLICT", f"{entity} cannot move from {row[sf]} to {values[sf]}")
        ctx[s.get("as", entity)] = self.store.update(entity, key, values)

    def _step_delete(self, s: dict, ctx: dict) -> None:
        self.store.delete(s["entity"], expr.resolve(s["key"], ctx))

    def _step_emit(self, s: dict, ctx: dict) -> None:
        self.store.emit(s["event"], expr.resolve(s.get("payload") or {}, ctx), ctx["now"])

    def _step_respond(self, s: dict, ctx: dict) -> None:
        raise _Respond(Result(int(s.get("status", 200)), expr.resolve(s.get("body"), ctx)))
