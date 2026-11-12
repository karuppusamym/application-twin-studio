"""Safe static importer for UiPath XAML projects.

Parses workflows without executing anything: no code, no build hooks, no
expression evaluation beyond the tiny analyser in ``vbexpr``. Activities
outside the supported subset are kept as opaque nodes and become gaps - they
are never silently skipped.

Honest scope note: the supported subset was developed against hand-authored
XAML that follows the Studio layout. A real Studio export should be run
through this importer before its coverage numbers are trusted.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .. import vbexpr

MAX_FILE_BYTES = 5 * 1024 * 1024
X_NS = "http://schemas.microsoft.com/winfx/2006/xaml"

# local activity name -> (kind, surface)
SUPPORTED = {
    "Sequence": ("control", None), "If": ("control", None), "Assign": ("data", None),
    "InvokeWorkflowFile": ("control", None), "RetryScope": ("control", None), "Throw": ("control", None),
    "LogMessage": ("data", None), "WriteLine": ("data", None), "Comment": ("data", None),
    "OpenBrowser": ("ui", "web"), "NavigateTo": ("ui", "web"), "TypeInto": ("ui", "web"), "Click": ("ui", "web"),
    "GetText": ("ui", "web"), "UiElementExists": ("ui", "web"), "CloseTab": ("ui", "web"),
    "HttpClient": ("http", "http"),
    "TerminalSession": ("terminal", "tn3270"), "TerminalSetFieldAtPosition": ("terminal", "tn3270"),
    "TerminalSendControlKey": ("terminal", "tn3270"), "TerminalGetTextAtPosition": ("terminal", "tn3270"),
    "TerminalWaitScreenText": ("terminal", "tn3270"),
}
# Non-activity elements that carry structure or metadata.
STRUCTURAL = {"Activity", "ActivityAction", "DelegateInArgument", "Variable", "InArgument", "OutArgument",
              "InOutArgument", "Target", "Members", "Property", "TextExpression", "Collection", "AssemblyReference",
              "String", "Dictionary", "ViewStateManager", "ViewStateData", "WorkflowViewStateService"}
EXPRESSION_ATTRS = ("Url", "Text", "Condition", "EndPoint", "Body", "Message", "ConnectionString", "Value", "Exists",
                    "Result", "StatusCode", "Row", "Column", "Length")


class ImportError_(Exception):
    pass


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_property(el: ET.Element) -> bool:
    return "." in local(el.tag)


def children(el: ET.Element) -> list[ET.Element]:
    """Activity children of a container (skips property elements and metadata)."""
    return [c for c in el if not is_property(c) and local(c.tag) not in STRUCTURAL]


def prop(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if local(c.tag) == f"{local(el.tag)}.{name}":
            return c
    return None


def body_activities(el: ET.Element, name: str) -> list[ET.Element]:
    """Activities inside a property such as OpenBrowser.Body / If.Then (unwraps ActivityAction)."""
    p = prop(el, name)
    if p is None:
        return []
    out = []
    for c in p:
        if local(c.tag) == "ActivityAction":
            out += children(c)
        elif not is_property(c) and local(c.tag) not in STRUCTURAL:
            out.append(c)
    return out


def parse_selector(selector: str | None) -> dict:
    """UiPath selector string -> attributes of its last tag."""
    tags = re.findall(r"<(\w+)\s+([^>]*?)/?>", selector or "")
    if not tags:
        return {}
    attrs = dict(re.findall(r"(\w+)\s*=\s*'([^']*)'", tags[-1][1]))
    attrs["_tag"] = tags[-1][0]
    return attrs


def selector_of(el: ET.Element) -> str | None:
    if el.get("Selector"):
        return el.get("Selector")
    for d in el.iter():
        if local(d.tag) == "Target" and d.get("Selector"):
            return d.get("Selector")
    return None


def safe_parse(path: Path) -> ET.Element:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ImportError_(f"{path.name}: larger than {MAX_FILE_BYTES} bytes")
    head = raw[:4096].decode("utf-8", "ignore") + raw.decode("utf-8", "ignore")
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ImportError_(f"{path.name}: DTD / entity declarations are rejected")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ImportError_(f"{path.name}: not well-formed XML ({exc})") from exc


def arguments_of(root: ET.Element) -> list[dict]:
    out = []
    for el in root.iter(f"{{{X_NS}}}Property"):
        t = el.get("Type", "")
        out.append({"name": el.get("Name"), "direction": "out" if t.startswith("OutArgument") else
                    "inout" if t.startswith("InOutArgument") else "in", "type": t})
    return out


def resolve_inside(base: Path, name: str) -> Path:
    """Reject path traversal out of the package directory."""
    target = (base / name.replace("\\", "/")).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise ImportError_(f"workflow reference escapes the package: {name}")
    return target


def import_project(project_dir: Path, entry: str | None = None) -> dict:
    project_dir = Path(project_dir)
    meta: dict = {}
    pj = project_dir / "project.json"
    if pj.exists():
        meta = json.loads(pj.read_text(encoding="utf-8-sig"))
    entry = entry or meta.get("main") or "Main.xaml"
    nodes: list[dict] = []
    files: dict[str, str] = {}
    arguments: dict[str, list] = {}

    def walk_file(rel: str, depth: int) -> None:
        if rel in files or depth > 20:
            return
        path = resolve_inside(project_dir, rel)
        if not path.exists():
            nodes.append(_node(len(nodes), rel, "InvokeWorkflowFile", f"missing workflow {rel}", "opaque", None,
                               False, f"referenced workflow file {rel} is not in the package"))
            return
        files[rel] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        root = safe_parse(path)
        arguments[rel] = arguments_of(root)
        for act in children(root):
            walk(act, rel, depth)

    def walk(el: ET.Element, rel: str, depth: int) -> None:
        name = local(el.tag)
        kind, surface = SUPPORTED.get(name, ("opaque", None))
        supported, reason, detail = name in SUPPORTED, None, {}
        if not supported:
            reason = f"activity {name} is outside the supported subset"
        for attr in EXPRESSION_ATTRS:
            raw = el.get(attr)
            if raw is None:
                continue
            src, is_expr = vbexpr.unwrap(raw)
            detail[attr] = raw
            if is_expr and supported:
                try:
                    vbexpr.analyse(src)
                except vbexpr.Unsupported as exc:
                    supported, reason = False, f"expression in {attr} not supported: {exc}"
        for p in el:  # arguments written as <X.Name><InArgument>[expr]</InArgument></X.Name>
            if not is_property(p) or local(p.tag).split(".", 1)[1] in ("Body", "Then", "Else", "ActivityBody", "Variables", "Target"):
                continue
            for d in p.iter():
                text = (d.text or "").strip()
                if supported and text and local(d.tag) in ("InArgument", "OutArgument", "InOutArgument"):
                    src, is_expr = vbexpr.unwrap(text)
                    if is_expr:
                        try:
                            vbexpr.analyse(src)
                        except vbexpr.Unsupported as exc:
                            supported, reason = False, f"expression not supported: {exc}"
        sel = selector_of(el) if kind == "ui" else None
        if sel:
            detail["selector"] = parse_selector(sel)
        for attr in ("Method", "Key", "WorkflowFileName", "NumberOfRetries", "BrowserType"):
            if el.get(attr) is not None:
                detail[attr] = el.get(attr)
        if name == "OpenBrowser" and (el.get("BrowserType") or "").upper() == "IE":
            supported, reason = False, "Internet Explorer automation needs a legacy-browser adapter (not available)"
        nodes.append(_node(len(nodes), rel, name, el.get("DisplayName") or name, kind if supported else "opaque",
                           surface, supported, reason, detail))
        if name == "InvokeWorkflowFile" and el.get("WorkflowFileName"):
            target, is_expr = vbexpr.unwrap(el.get("WorkflowFileName"))
            if is_expr:
                nodes[-1].update(supported=False, kind="opaque", reason="dynamic workflow file name cannot be resolved statically")
            else:
                walk_file(target.replace("\\", "/"), depth + 1)
        if not supported:
            return  # do not descend into something we cannot execute
        for sub in ("Then", "Else", "Body", "ActivityBody"):
            for c in body_activities(el, sub):
                walk(c, rel, depth)
        for c in children(el):
            walk(c, rel, depth)

    walk_file(entry, 0)
    total = len(nodes)
    sup = sum(1 for n in nodes if n["supported"])
    return {
        "tool": "uipath",
        "project": {"name": meta.get("name"), "version": meta.get("projectVersion"), "main": entry,
                    "studio_version": meta.get("studioVersion"), "dependencies": meta.get("dependencies") or {}},
        "importer_version": "uipath-static-0.1",
        "files": files,
        "package_hash": "sha256:" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "arguments": arguments.get(entry, []),
        "nodes": nodes,
        "summary": {"nodes": total, "supported": sup, "opaque": total - sup},
    }


def _node(n: int, file: str, activity: str, display: str, kind: str, surface: str | None, supported: bool,
          reason: str | None, detail: dict | None = None) -> dict:
    return {"id": f"n{n + 1:03d}", "file": file, "activity": activity, "display_name": display, "kind": kind,
            "surface": surface, "supported": supported, "reason": reason, "detail": detail or {}}
