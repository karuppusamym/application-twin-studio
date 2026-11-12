"""Independent assertions, original-vs-replacement comparison and certificates.

Three claims are kept apart:
  model verification     - does the twin follow its reviewed spec (unit tests)
  reference validation   - does the twin match the real application (needs
                           version-matched observations; none exist for the
                           samples, so it is never claimed here)
  migration validation   - do original and replacement both satisfy the same
                           independent expected outcomes

Assertions read the environment snapshot and the runner output. They are
written by hand in the scenario file, not derived from handler code, so two
runners agreeing with each other against a wrong twin still fail.
"""
from __future__ import annotations

from typing import Any

from .expr import lookup

# Declared normalisation: implementation bookkeeping only. Money, identity,
# status and decision fields are never ignored.
DEFAULT_IGNORE = {"_rev"}


def _rows(snapshot: dict, a: dict) -> list[dict]:
    rows = snapshot["state"].get(a["app"], {}).get(a["entity"], [])
    where = a.get("where") or {}
    return [r for r in rows if all(str(r.get(k)) == str(v) for k, v in where.items())]


def evaluate(assertions: list[dict], snapshot: dict, runner: dict, keys: dict) -> list[dict]:
    out = [{
        "id": "runner-completed", "type": "runner", "critical": True,
        "passed": runner["status"] == "COMPLETED", "expected": "COMPLETED", "actual": runner["status"],
        "detail": runner.get("error") or "",
    }]
    for a in assertions:
        t, res = a["type"], {"id": a.get("id", a["type"]), "type": a["type"], "critical": bool(a.get("critical")), "detail": ""}
        if t == "state":
            key_field = keys[a["app"]][a["entity"]]
            row = next((r for r in snapshot["state"].get(a["app"], {}).get(a["entity"], [])
                        if str(r.get(key_field)) == str(a["key"])), None)
            res["expected"] = a["expect"]
            res["actual"] = None if row is None else {k: row.get(k) for k in a["expect"]}
            res["passed"] = row is not None and all(str(row.get(k)) == str(v) for k, v in a["expect"].items())
            res["detail"] = f"{a['app']}.{a['entity']} {a['key']}"
        elif t == "count":
            n = len(_rows(snapshot, a))
            res.update(expected=a["equals"], actual=n, passed=n == a["equals"],
                       detail=f"{a['app']}.{a['entity']} where {a.get('where') or {}}")
        elif t == "rows":
            rows = _rows(snapshot, a)
            bad = [r for r in rows if any(str(r.get(k)) != str(v) for k, v in a["each"].items())]
            res.update(expected=a["each"], actual=[{k: r.get(k) for k in a["each"]} for r in rows],
                       passed=bool(rows) and not bad, detail=f"{a['app']}.{a['entity']} where {a.get('where') or {}}")
        elif t == "event":
            evs = [e for e in snapshot["events"].get(a["app"], []) if e["event_type"] == a["event"]
                   and all(str(e["payload"].get(k)) == str(v) for k, v in (a.get("where") or {}).items())]
            res.update(expected=a["count"], actual=len(evs), passed=len(evs) == a["count"], detail=f"{a['app']} event {a['event']}")
        elif t == "output":
            actual = lookup(runner.get("output") or {}, a["path"])
            res.update(expected=a["equals"], actual=actual, passed=str(actual) == str(a["equals"]) and actual is not None,
                       detail=f"runner output {a['path']}")
        else:
            res.update(expected=None, actual=None, passed=False, detail=f"unknown assertion type {t}")
        out.append(res)
    return out


def verdict(results: list[dict]) -> str:
    if any(not r["passed"] and r["critical"] for r in results):
        return "FAILED"
    return "PASSED" if all(r["passed"] for r in results) else "PASSED_WITH_WARNINGS"


def business_view(snapshot: dict, output: dict, ignore: set[str] | None = None) -> dict:
    ignore = DEFAULT_IGNORE | (ignore or set())
    state = {}
    for app, entities in snapshot["state"].items():
        for entity, rows in entities.items():
            state[f"{app}.{entity}"] = [
                {k: v for k, v in r.items() if k not in ignore and f"{app}.{entity}.{k}" not in ignore} for r in rows]
    events = {app: [{"event_type": e["event_type"], "payload": e["payload"]} for e in evs]
              for app, evs in snapshot["events"].items()}
    return {"state": state, "events": events, "output": {k: v for k, v in (output or {}).items() if k != "trace"}}


def diff(a: Any, b: Any, path: str = "") -> list[dict]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
        return out
    if isinstance(a, list) and isinstance(b, list):
        out = []
        for i in range(max(len(a), len(b))):
            out += diff(a[i] if i < len(a) else None, b[i] if i < len(b) else None, f"{path}[{i}]")
        return out
    return [] if a == b else [{"path": path, "original": a, "replacement": b}]


def certificate(original: dict, replacement: dict, differences: list[dict], verifications: set[str], open_gaps: int) -> dict:
    """Scoped confidence statement for one comparison."""
    both_pass = original["verdict"] != "FAILED" and replacement["verdict"] != "FAILED"
    equivalent = not differences
    if not both_pass or not equivalent:
        result = "NOT_EQUIVALENT" if both_pass else "FAILED"
    else:
        result = "EQUIVALENT_IN_SCOPE"
    if verifications & {"hypothesis", "demonstration"}:
        confidence = "exploratory"
    elif verifications <= {"reference_verified"}:
        confidence = "reference_validated"
    else:
        confidence = "model_validated"
    simulated = "simulated-replay" in (original["runner"]["evidence_class"], replacement["runner"]["evidence_class"])
    complete = result == "EQUIVALENT_IN_SCOPE" and confidence == "reference_validated" and not simulated and open_gaps == 0
    limits = []
    if confidence == "exploratory":
        limits.append("Twin rules are demonstration/hypothesis level: this proves consistency with the model, not with the real application.")
    if simulated:
        limits.append("The original automation ran through the XAML replay simulator, not a licensed UiPath robot.")
    if open_gaps:
        limits.append(f"{open_gaps} gap(s) remain open for these automations.")
    return {"result": result, "confidence": confidence,
            "category": "migration_evidence_complete" if complete else confidence if result == "EQUIVALENT_IN_SCOPE" else "none",
            "may_qualify_migration": complete, "limits": limits}
