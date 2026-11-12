"""Onboarding analysis: automation -> required twin operations -> gaps -> readiness.

Registration is universal; executable support is conditional. Every node that
cannot be mapped to a modelled operation, every unsupported activity and every
unresolved binding becomes a gap record. Critical gaps block readiness; they
are never turned into a successful response.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import vbexpr
from .importers import uipath
from .spec import PROTOCOLS

_TPL = re.compile(r"\{\{\s*([a-z0-9_-]+)\.([a-z0-9_]+)")


def _gap(automation_id: str, kind: str, severity: str, summary: str, node: dict | None = None,
         action: str = "", app: str | None = None) -> dict:
    key = f"{automation_id}|{kind}|{summary}|{(node or {}).get('id')}"
    return {"id": "G-" + hashlib.sha1(key.encode()).hexdigest()[:7].upper(), "automation_id": automation_id,
            "kind": kind, "severity": severity, "summary": summary, "application_id": app,
            "node": (node or {}).get("id"), "node_name": (node or {}).get("display_name"),
            "requested_action": action, "status": "open", "blocks": "readiness" if severity == "critical" else "qualification"}


def ui_index(spec: dict) -> dict[str, dict]:
    """Element id -> {page, operation} for everything the web adapter renders."""
    idx: dict[str, dict] = {eid: {"page": "*", "operation": None} for eid in ("error-message", "message", "page-title", "current-user", "nav-logout")}
    for page in (spec.get("ui") or {}).get("pages") or []:
        pid, op = page["id"], page.get("operation")
        if page["type"] == "form":
            for f in page.get("fields") or []:
                idx[f.get("id", f["name"])] = {"page": pid, "operation": None}
            idx[(page.get("submit") or {}).get("id", "btn-submit")] = {"page": pid, "operation": op}
        elif page["type"] == "table":
            idx[f"table-{pid}"] = {"page": pid, "operation": op}
            idx["btn-filter"] = {"page": pid, "operation": op}
            for f in page.get("filters") or []:
                idx[f.get("id", "filter-" + f["name"])] = {"page": pid, "operation": None}
        else:
            for f in page.get("fields") or []:
                idx[f"field-{f['field']}"] = {"page": pid, "operation": op}
            for a in page.get("actions") or []:
                idx[a["id"]] = {"page": pid, "operation": a["operation"]}
                for f in a.get("fields") or []:
                    idx[f.get("id", f"{a['id']}-{f['name']}")] = {"page": pid, "operation": None}
            for rel in page.get("related") or []:
                idx[f"table-{rel['id']}"] = {"page": pid, "operation": rel["operation"]}
    for n in (spec.get("ui") or {}).get("nav") or []:
        idx[f"nav-{n['id']}"] = {"page": "*", "operation": None}
    return idx


def _route_match(spec: dict, method: str, path: str, interface_protocol: str = "http") -> str | None:
    for op in spec["operations"]:
        if not op.get("route"):
            continue
        m, p = op["route"].split(" ", 1)
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+", p.rstrip("/")) + "/?$"
        if m == method.upper() and re.match(rx, path):
            return op["id"]
    return None


class _TerminalWalker:
    """Follows literal terminal input through the screen state machine, statically."""

    def __init__(self, spec: dict):
        self.term = spec.get("terminal") or {}
        self.screen: str | None = self.term.get("start")
        self.fields: dict[str, str | None] = {}

    def input_at(self, row: int, col: int) -> tuple[str | None, bool]:
        """(input name on current screen, exists on any screen)"""
        anywhere = False
        for sid, scr in (self.term.get("screens") or {}).items():
            for i in scr.get("inputs") or []:
                if i["row"] == row and i["col"] == col:
                    anywhere = True
                    if sid == self.screen:
                        return i["name"], True
        return None, anywhere

    def key(self, aid: str) -> tuple[str | None, bool]:
        """Returns (operation invoked, key known). Advances the tracked screen when it can."""
        if self.screen is None:
            known = any(aid in (s.get("keys") or {}) for s in (self.term.get("screens") or {}).values())
            return None, known
        action = ((self.term["screens"][self.screen]).get("keys") or {}).get(aid)
        fields, self.fields = self.fields, {}
        if action is None:
            return None, False
        op = None
        if "switch" in action:
            src = action["switch"]
            name = (src.get("$upper") if isinstance(src, dict) else src) or ""
            value = fields.get(str(name).replace("$field.", ""))
            if value is None:
                self.screen = None  # value only known at run time
                return None, True
            action = (action.get("cases") or {}).get(str(value).upper(), action.get("default") or {})
        if "operation" in action:
            op = action["operation"]
            action = action.get("on_success") or {}
        if action.get("goto"):
            self.screen = action["goto"]
        return op, True


def analyse(automation: dict, specs: dict[str, dict]) -> dict:
    aid = automation["id"]
    gaps: list[dict] = []
    required: dict[tuple, dict] = {}
    nodes: list[dict] = []
    package_hash, importer_version, summary = None, "generic-manifest-0.1", {}

    def need(app: str, op: str, via: str) -> None:
        o = next(o for o in specs[app]["operations"] if o["id"] == op)
        r = required.setdefault((app, op), {"application_id": app, "operation": op, "interface": o["interface"],
                                            "verification": o.get("verification", "hypothesis"), "via": []})
        if via not in r["via"]:
            r["via"].append(via)

    # which workflow argument points at which application surface
    arg_app: dict[str, tuple[str, str]] = {}
    bindings = []
    for name, tpl in (automation.get("arguments") or {}).items():
        refs = _TPL.findall(str(tpl))
        ok = True
        for app, surface in refs:
            if app == "inputs":
                continue
            if app not in specs:
                ok = False
                gaps.append(_gap(aid, "missing_application", "critical", f"argument {name} needs application '{app}', which has no twin",
                                 action=f"Onboard evidence for '{app}' and author its ATS-Spec, or bind an approved sandbox.", app=app))
            else:
                arg_app[name] = (app, surface)
        bindings.append({"argument": name, "template": tpl, "resolved": ok})

    if automation.get("tool") == "uipath":
        try:
            imp = uipath.import_project(Path(automation["_dir"]), automation.get("entry"))
        except uipath.ImportError_ as exc:
            return {"automation_id": aid, "error": str(exc), "nodes": [], "gaps": [
                _gap(aid, "import_rejected", "critical", str(exc), action="Supply a well-formed package.")],
                "readiness": {"state": "BLOCKED", "confidence": "none", "blockers": [str(exc)]}}
        package_hash, importer_version, summary, nodes = imp["package_hash"], imp["importer_version"], imp["summary"], imp["nodes"]
        ui_app: str | None = None
        term: _TerminalWalker | None = None
        term_app: str | None = None
        for n in nodes:
            d, act = n["detail"], n["activity"]
            n["mapping"] = None
            if not n["supported"]:
                gaps.append(_gap(aid, "unsupported_activity", "critical", n["reason"] or f"{act} is not supported", n,
                                 "Provide an adapter with a conformance test, or replace the step and record the diff."))
                continue
            if act in ("OpenBrowser", "NavigateTo", "HttpClient", "SoapClient", "TerminalSession"):
                attr = {"HttpClient": "EndPoint", "SoapClient": "EndPoint", "TerminalSession": "ConnectionString"}.get(act, "Url")
                src, is_expr = vbexpr.unwrap(d.get(attr))
                parts = vbexpr.concat_parts(src) if is_expr else [("lit", src)]
                head = parts[0] if parts else None
                target = arg_app.get(head[1]) if head and head[0] == "var" else None
                if target is None:
                    gaps.append(_gap(aid, "unresolved_destination", "critical",
                                     f"{act} destination '{d.get(attr)}' is not bound to a twin interface", n,
                                     "Bind the destination through a workflow argument, or record a reviewed test-only patch."))
                    continue
                app, surface = target
                spec = specs[app]
                path = "".join(p[1] if p[0] == "lit" else "X" for p in parts[1:])
                if act == "TerminalSession":
                    term, term_app = _TerminalWalker(spec), app
                    n["mapping"] = {"application_id": app, "interface": "tn3270"}
                    if not spec.get("terminal"):
                        gaps.append(_gap(aid, "missing_interface", "critical", f"{app} has no terminal interface", n, app=app))
                elif act == "SoapClient":
                    op = next((o["id"] for o in spec["operations"] if o.get("soap_operation") == d.get("Method")), None)
                    if op:
                        need(app, op, n["id"])
                        n["mapping"] = {"application_id": app, "operation": op}
                    else:
                        gaps.append(_gap(aid, "unmodeled_operation", "critical",
                                         f"SOAP operation {d.get('Method')} is not modelled by twin '{app}'", n,
                                         "Add the operation to the ATS-Spec with evidence, then re-run onboarding.", app))
                elif act == "HttpClient":
                    op = _route_match(spec, d.get("Method", "GET"), path)
                    if op:
                        need(app, op, n["id"])
                        n["mapping"] = {"application_id": app, "operation": op}
                    else:
                        gaps.append(_gap(aid, "unmodeled_operation", "critical",
                                         f"{d.get('Method', 'GET')} {path} is not modelled by twin '{app}'", n,
                                         "Add the operation to the ATS-Spec with evidence, then re-run onboarding.", app))
                else:
                    ui_app = app
                    page = next((p for p in (spec.get("ui") or {}).get("pages") or []
                                 if re.match("^" + re.sub(r"\{[^}]+\}", "[^/]+", p["path"]) + "$", "/ui" + path)), None)
                    n["mapping"] = {"application_id": app, "page": page["id"] if page else None}
                    if page is None:
                        gaps.append(_gap(aid, "unmodeled_page", "critical", f"page {path or '/'} is not modelled by twin '{app}'", n, app=app))
                    elif page.get("operation") and page["type"] != "form":
                        need(app, page["operation"], n["id"])
            elif n["kind"] == "ui":
                sel = d.get("selector") or {}
                hit = ui_index(specs[ui_app]).get(sel.get("id", "")) if ui_app else None
                if hit is None:
                    gaps.append(_gap(aid, "unknown_selector", "critical",
                                     f"selector {sel} does not match any element of twin '{ui_app}'", n,
                                     "Capture the page and extend the twin UI, preserving the original selector.", ui_app))
                else:
                    n["mapping"] = {"application_id": ui_app, **hit}
                    if hit["operation"] and act == "Click":
                        need(ui_app, hit["operation"], n["id"])
            elif n["kind"] == "terminal" and term is not None:
                if act == "TerminalSetFieldAtPosition":
                    row, col = int(d.get("Row", 0)), int(d.get("Column", 0))
                    name, anywhere = term.input_at(row, col)
                    text, is_expr = vbexpr.unwrap(d.get("Text"))
                    if name:
                        term.fields[name] = None if is_expr else text
                    if not anywhere:
                        gaps.append(_gap(aid, "unknown_field", "critical", f"no input field at row {row} column {col} on any '{term_app}' screen", n, app=term_app))
                    n["mapping"] = {"application_id": term_app, "screen": term.screen, "field": name}
                elif act == "TerminalSendControlKey":
                    from .runners.xaml_replay import CONTROL_KEYS
                    key = CONTROL_KEYS.get((d.get("Key") or "Transmit").upper(), d.get("Key"))
                    before = term.screen
                    op, known = term.key(key)
                    n["mapping"] = {"application_id": term_app, "screen": before, "key": key, "operation": op}
                    if not known:
                        gaps.append(_gap(aid, "unknown_key", "major", f"key {key} is not handled on screen {before}", n, app=term_app))
                    if op:
                        need(term_app, op, n["id"])
                else:
                    n["mapping"] = {"application_id": term_app, "screen": term.screen}
    else:
        for i, req in enumerate(automation.get("requires") or []):
            node = {"id": f"m{i + 1:03d}", "file": "automation.yaml", "activity": "manifest-requirement",
                    "display_name": f"{req.get('app')}.{req.get('operation') or req.get('entity')}", "kind": "manifest",
                    "surface": None, "supported": True, "reason": None, "detail": req, "mapping": None}
            nodes.append(node)
            app = req.get("app")
            if app not in specs:
                node["supported"] = False
                gaps.append(_gap(aid, "missing_application", "critical", f"application '{app}' has no twin", node, app=app))
                continue
            spec = specs[app]
            itf = next((x for x in spec["interfaces"] if x["id"] == req.get("interface")), None)
            if itf is None or itf["protocol"] not in PROTOCOLS:
                node["supported"] = False
                gaps.append(_gap(aid, "missing_interface", "critical", f"{app} has no executable interface '{req.get('interface')}'", node, app=app))
                continue
            if req.get("operation"):
                if req["operation"] not in {o["id"] for o in spec["operations"]}:
                    node["supported"] = False
                    gaps.append(_gap(aid, "unmodeled_operation", "critical", f"{app}.{req['operation']} is not modelled", node, app=app))
                    continue
                need(app, req["operation"], node["id"])
            elif req.get("entity") and req["entity"] not in spec["entities"]:
                node["supported"] = False
                gaps.append(_gap(aid, "unmodeled_entity", "critical", f"{app} has no entity {req['entity']}", node, app=app))
                continue
            node["mapping"] = {"application_id": app, "operation": req.get("operation"), "entity": req.get("entity")}
        summary = {"nodes": len(nodes), "supported": sum(n["supported"] for n in nodes),
                   "opaque": sum(not n["supported"] for n in nodes)}

    for r in required.values():
        if r["verification"] in ("hypothesis", "demonstration"):
            gaps.append(_gap(aid, "evidence", "evidence",
                             f"{r['application_id']}.{r['operation']} is modelled from {r['verification']} rules, not owner-approved evidence",
                             action="Application owner reviews the rule and supplies version-matched examples.", app=r["application_id"]))

    critical = [g for g in gaps if g["severity"] == "critical"]
    evidence = [g for g in gaps if g["severity"] == "evidence"]
    summary["mapped"] = sum(1 for n in nodes if n.get("mapping"))
    return {
        "automation_id": aid, "tool": automation.get("tool"), "runner": automation.get("runner"),
        "package_hash": package_hash, "importer_version": importer_version, "summary": summary,
        "nodes": nodes, "bindings": bindings,
        "applications": sorted({r["application_id"] for r in required.values()}),
        "required_operations": sorted(required.values(), key=lambda r: (r["application_id"], r["operation"])),
        "gaps": gaps,
        "readiness": {
            "state": "BLOCKED" if critical else "READY",
            "confidence": "none" if critical else "exploratory" if evidence else "model_validated",
            "blockers": [g["summary"] for g in critical],
            "note": ("Runnable for exploratory tests only. Evidence gaps keep it out of migration qualification."
                     if evidence and not critical else ""),
        },
    }
