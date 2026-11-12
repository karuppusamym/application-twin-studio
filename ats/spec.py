"""ATS-Spec loading and semantic validation.

ATS-Spec is the generator-independent description of a twin's observable
behaviour: interfaces, entities and state transitions, operations built from
allowlisted steps, optional web pages and terminal screens. A spec that fails
validation never becomes a runnable twin.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import expr

SCHEMA_VERSION = "1.0"
PROTOCOLS = {"http", "web", "tn3270", "sql", "soap"}
# Protocols that can be registered and mapped but have no executable adapter yet.
UNSUPPORTED_PROTOCOLS = {"mq", "kafka", "file-batch", "ie-legacy", "windows-desktop", "tn5250"}
STEP_TYPES = {"load", "query", "require", "call", "decide", "insert", "update", "delete", "emit", "respond", "if", "set"}
VERIFICATION = {"hypothesis", "demonstration", "owner_approved", "reference_verified"}
BUILTIN_ERRORS = {
    "VALIDATION_ERROR": {"status": 422, "message": "Request failed validation"},
    "UNAUTHORIZED": {"status": 401, "message": "Missing or invalid test credential"},
    "NOT_FOUND": {"status": 404, "message": "Resource not found"},
    "STATE_CONFLICT": {"status": 409, "message": "Resource state does not allow this operation"},
    "REVISION_CONFLICT": {"status": 412, "message": "Resource was modified by another request"},
    "IDEMPOTENCY_KEY_REQUIRED": {"status": 400, "message": "Idempotency-Key header is required"},
    "IDEMPOTENCY_KEY_REUSED": {"status": 409, "message": "Idempotency-Key was reused with a different request"},
    "DEPENDENCY_UNAVAILABLE": {"status": 503, "message": "A required dependency did not respond", "retryable": True},
    "DEPENDENCY_TIMEOUT": {"status": 504, "message": "Injected timeout", "retryable": True},
    "INJECTED_FAULT": {"status": 500, "message": "Injected fault", "retryable": True},
    "UNMODELED_OPERATION": {"status": 501, "message": "This twin does not model the requested operation"},
}
_ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")


class SpecError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def load_file(path: Path) -> dict:
    spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return prepare(spec)


def prepare(spec: dict) -> dict:
    errors = validate(spec)
    if errors:
        raise SpecError(errors)
    spec = dict(spec)
    spec["_digest"] = digest({k: v for k, v in spec.items() if not k.startswith("_")})
    return spec


def _walk_steps(steps: list, fn) -> None:
    for i, step in enumerate(steps or []):
        fn(i, step)
        if isinstance(step, dict) and "if" in step:
            body = step["if"] or {}
            _walk_steps(body.get("then", []), fn)
            _walk_steps(body.get("else", []), fn)


def validate(spec: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    for key in ("schema_version", "application_id", "spec_version", "interfaces", "entities", "operations"):
        if key not in spec:
            errors.append(f"missing required key '{key}'")
    if errors:
        return errors
    if str(spec["schema_version"]) != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {spec['schema_version']}")
    if not re.match(r"^[a-z][a-z0-9_-]*$", str(spec["application_id"])):
        errors.append("application_id must be lowercase alphanumeric")

    interfaces: dict[str, dict] = {}
    for itf in spec["interfaces"]:
        if itf.get("id") in interfaces:
            errors.append(f"duplicate interface id {itf.get('id')}")
        interfaces[itf.get("id")] = itf
        proto = itf.get("protocol")
        if proto not in PROTOCOLS | UNSUPPORTED_PROTOCOLS:
            errors.append(f"interface {itf.get('id')}: unknown protocol {proto}")

    entities = spec["entities"] or {}
    for name, ent in entities.items():
        fields = ent.get("fields") or {}
        if ent.get("key") not in fields:
            errors.append(f"entity {name}: key '{ent.get('key')}' is not a declared field")
        sf = ent.get("state_field")
        if sf:
            enum = (fields.get(sf) or {}).get("enum") if isinstance(fields.get(sf), dict) else None
            if not enum:
                errors.append(f"entity {name}: state_field '{sf}' needs an enum")
            for src, dsts in (ent.get("transitions") or {}).items():
                for s in [src, *dsts]:
                    if enum and s not in enum:
                        errors.append(f"entity {name}: transition uses undeclared state {s}")

    deps = {d["application_id"] for d in spec.get("dependencies") or []}
    declared_errors = set(BUILTIN_ERRORS) | set(spec.get("errors") or {})
    ops: dict[str, dict] = {}
    for op in spec["operations"]:
        oid = op.get("id")
        where = f"operation {oid}"
        if oid in ops:
            errors.append(f"duplicate operation id {oid}")
        ops[oid] = op
        if op.get("interface") not in interfaces:
            errors.append(f"{where}: unknown interface {op.get('interface')}")
        if op.get("route") and not _ROUTE.match(op["route"]):
            errors.append(f"{where}: route must look like 'GET /path/{{param}}'")
        if op.get("verification", "hypothesis") not in VERIFICATION:
            errors.append(f"{where}: unknown verification '{op.get('verification')}'")
        if not op.get("steps"):
            errors.append(f"{where}: needs at least one step")

        def check_step(i: int, step: Any, where=where) -> None:
            sw = f"{where} step {i + 1}"
            if not isinstance(step, dict) or len(step) != 1:
                errors.append(f"{sw}: a step is a mapping with exactly one step type")
                return
            (kind, body), = step.items()
            if kind not in STEP_TYPES:
                errors.append(f"{sw}: step type '{kind}' is not on the allowlist")
                return
            body = body or {}
            expr.check(body, sw, errors)
            ent = body.get("entity") if isinstance(body, dict) else None
            if kind in ("load", "query", "insert", "update", "delete") and ent not in entities:
                errors.append(f"{sw}: unknown entity {ent}")
            if kind == "call" and body.get("dependency") not in deps:
                errors.append(f"{sw}: dependency '{body.get('dependency')}' is not declared")
            if kind == "require":
                expr.check_cond(body.get("that"), sw, errors)
            if kind == "if":
                expr.check_cond(body.get("when"), sw, errors)
            if kind == "decide":
                for rule in body.get("rules") or []:
                    expr.check_cond(rule.get("when"), sw, errors)
            for code_key in ("error", "not_found"):
                code = body.get(code_key) if isinstance(body, dict) else None
                if code and code not in declared_errors:
                    errors.append(f"{sw}: error code {code} is not declared")

        _walk_steps(op.get("steps") or [], check_step)

    for page in (spec.get("ui") or {}).get("pages") or []:
        pw = f"ui page {page.get('id')}"
        if page.get("type") not in ("form", "table", "detail"):
            errors.append(f"{pw}: unknown page type {page.get('type')}")
        refs = [page.get("operation")] + [a.get("operation") for a in page.get("actions") or []]
        refs += [r.get("operation") for r in page.get("related") or []]
        for ref in refs:
            if ref and ref not in ops:
                errors.append(f"{pw}: unknown operation {ref}")

    term = spec.get("terminal")
    if term:
        screens = term.get("screens") or {}
        if term.get("start") not in screens:
            errors.append("terminal: start screen is not defined")

        def check_action(act: Any, where: str) -> None:
            if not isinstance(act, dict):
                return
            if act.get("operation") and act["operation"] not in ops:
                errors.append(f"{where}: unknown operation {act['operation']}")
            if act.get("goto") and act["goto"] not in screens:
                errors.append(f"{where}: unknown screen {act['goto']}")
            for sub in ("on_success", "on_error", "default"):
                check_action(act.get(sub), where)
            for sub in (act.get("cases") or {}).values():
                check_action(sub, where)

        for sid, scr in screens.items():
            for key, act in (scr.get("keys") or {}).items():
                check_action(act, f"terminal screen {sid} key {key}")
    return errors


def capability_manifest(spec: dict) -> dict:
    """What this twin can and cannot do, per interface."""
    out = []
    for itf in spec["interfaces"]:
        proto = itf["protocol"]
        out.append(
            {
                "interface_id": itf["id"],
                "protocol": proto,
                "executable": proto in PROTOCOLS,
                "unsupported_reason": None if proto in PROTOCOLS else f"no runtime adapter for {proto} yet",
                "operations": [o["id"] for o in spec["operations"] if o["interface"] == itf["id"]],
            }
        )
    return {"application_id": spec["application_id"], "spec_version": spec["spec_version"], "interfaces": out}
