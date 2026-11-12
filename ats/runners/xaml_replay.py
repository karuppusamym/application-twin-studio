"""Replays the supported UiPath XAML subset against a twin environment.

This is a SIMULATED replay of the original automation's logic. It is not the
UiPath runtime: timing, selector engines, activity defaults and package
versions differ. It exists so a migration team can exercise the original
workflow's steps before (or without) a licensed robot. A certificate records
which runner produced the evidence, and replay evidence never counts as
original-runner acceptance.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from .. import vbexpr
from ..importers.uipath import (SUPPORTED, X_NS, body_activities, children, local, parse_selector, prop,
                                resolve_inside, safe_parse, selector_of)
from ..runtime.tn3270 import Tn3270Client
from .htmldriver import Blocked, ElementNotFound, HtmlDriver

CONTROL_KEYS = {"TRANSMIT": "ENTER", "ENTER": "ENTER", "CLEAR": "CLEAR", "PA1": "PA1", "PA2": "PA2",
                **{f"F{n}": f"PF{n}" for n in range(1, 25)}, **{f"PF{n}": f"PF{n}" for n in range(1, 25)}}


class WorkflowFault(Exception):
    """An activity threw (what UiPath would surface as a faulted job)."""


class UnsupportedActivity(Exception):
    pass


class XamlReplay:
    def __init__(self, project_dir: Path, allowed_prefixes: list[str], allowed_hosts: list[tuple[str, int]],
                 max_steps: int = 2000):
        self.dir = Path(project_dir)
        self.allowed = [p.rstrip("/") for p in allowed_prefixes]
        self.allowed_hosts = {(h, int(p)) for h, p in allowed_hosts}
        self.browser: HtmlDriver | None = None
        self.terminal: Tn3270Client | None = None
        self.http = httpx.Client(timeout=15.0)
        self.log: list[dict] = []
        self.steps = 0
        self.max_steps = max_steps

    # -- entry ------------------------------------------------------------
    def run(self, entry: str, args: dict) -> dict:
        try:
            return self._run_file(entry, dict(args))
        finally:
            if self.browser:
                self.browser.close()
            if self.terminal:
                self.terminal.close()
            self.http.close()

    def _run_file(self, rel: str, scope: dict) -> dict:
        root = safe_parse(resolve_inside(self.dir, rel))
        for el in root.iter(f"{{{X_NS}}}Property"):
            scope.setdefault(el.get("Name"), None)
        for act in children(root):
            self._exec(act, scope, rel)
        return scope

    # -- helpers ----------------------------------------------------------
    def _val(self, raw: str | None, scope: dict) -> Any:
        src, is_expr = vbexpr.unwrap(raw)
        return vbexpr.evaluate(src, scope) if is_expr else src

    def _out(self, raw: str | None, scope: dict, value: Any) -> None:
        if raw:
            name, _ = vbexpr.unwrap(raw)
            scope[name.strip()] = value

    def _arg_text(self, el: ET.Element, name: str) -> str | None:
        """Argument given either as attribute or as <X.Name><InArgument>[..]</InArgument></X.Name>."""
        if el.get(name) is not None:
            return el.get(name)
        p = prop(el, name)
        if p is not None:
            for d in p.iter():
                if local(d.tag) in ("InArgument", "OutArgument", "InOutArgument") and (d.text or "").strip():
                    return d.text.strip()
        return None

    def _note(self, rel: str, el: ET.Element, info: str) -> None:
        self.log.append({"n": len(self.log) + 1, "file": rel, "activity": local(el.tag),
                         "display_name": el.get("DisplayName") or local(el.tag), "info": info})

    def _declare(self, el: ET.Element, scope: dict) -> None:
        p = prop(el, "Variables")
        for v in (p if p is not None else []):
            if local(v.tag) == "Variable":
                default = v.get("Default")
                scope.setdefault(v.get("Name"), self._val(default, scope) if default is not None else None)

    # -- interpreter ------------------------------------------------------
    def _exec(self, el: ET.Element, scope: dict, rel: str) -> None:
        name = local(el.tag)
        self.steps += 1
        if self.steps > self.max_steps:
            raise WorkflowFault("step budget exceeded (possible loop)")
        if name not in SUPPORTED:
            raise UnsupportedActivity(f"{name} ({el.get('DisplayName') or name}) in {rel}")
        getattr(self, f"_a_{name}")(el, scope, rel)

    def _a_Sequence(self, el, scope, rel):
        self._declare(el, scope)
        for c in children(el):
            self._exec(c, scope, rel)

    def _a_Comment(self, el, scope, rel):
        pass

    def _a_LogMessage(self, el, scope, rel):
        self._note(rel, el, str(self._val(self._arg_text(el, "Message"), scope)))

    _a_WriteLine = _a_LogMessage

    def _a_Assign(self, el, scope, rel):
        value = self._val(self._arg_text(el, "Value"), scope)
        self._out(self._arg_text(el, "To"), scope, value)
        self._note(rel, el, f"{vbexpr.unwrap(self._arg_text(el, 'To'))[0]} = {value!r}")

    def _a_If(self, el, scope, rel):
        cond = bool(self._val(el.get("Condition"), scope))
        self._note(rel, el, f"condition is {cond}")
        for c in body_activities(el, "Then" if cond else "Else"):
            self._exec(c, scope, rel)

    def _a_Throw(self, el, scope, rel):
        raise WorkflowFault(el.get("DisplayName") or "Throw")

    def _a_RetryScope(self, el, scope, rel):
        attempts = int(el.get("NumberOfRetries", "3"))
        for attempt in range(1, attempts + 1):
            try:
                for c in body_activities(el, "ActivityBody"):
                    self._exec(c, scope, rel)
                return
            except (WorkflowFault, httpx.HTTPError) as exc:
                self._note(rel, el, f"attempt {attempt} failed: {exc}")
                if attempt == attempts:
                    raise WorkflowFault(f"retry scope exhausted after {attempts} attempts") from exc

    def _a_InvokeWorkflowFile(self, el, scope, rel):
        target = el.get("WorkflowFileName", "").replace("\\", "/")
        inner: dict = {}
        outs: list[tuple[str, str]] = []
        p = prop(el, "Arguments")
        for a in (p if p is not None else []):
            key = a.get(f"{{{X_NS}}}Key")
            kind = local(a.tag)
            if kind in ("InArgument", "InOutArgument"):
                inner[key] = self._val((a.text or "").strip(), scope)
            if kind in ("OutArgument", "InOutArgument"):
                outs.append((key, (a.text or "").strip()))
        self._note(rel, el, f"invoke {target}")
        result = self._run_file(target, inner)
        for key, dest in outs:
            self._out(dest, scope, result.get(key))

    # -- web --------------------------------------------------------------
    def _browser(self) -> HtmlDriver:
        if self.browser is None:
            raise WorkflowFault("UI activity used outside an Open Browser scope")
        return self.browser

    def _a_OpenBrowser(self, el, scope, rel):
        url = str(self._val(el.get("Url"), scope))
        self.browser = HtmlDriver(self.allowed)
        self.browser.goto(url)
        self._note(rel, el, f"open {url} -> {self.browser.status}")
        for c in body_activities(el, "Body"):
            self._exec(c, scope, rel)

    def _a_NavigateTo(self, el, scope, rel):
        url = str(self._val(el.get("Url"), scope))
        self._browser().goto(url)
        self._note(rel, el, f"navigate {url} -> {self._browser().status}")

    def _a_CloseTab(self, el, scope, rel):
        pass

    def _sel(self, el) -> dict:
        return parse_selector(selector_of(el))

    def _a_TypeInto(self, el, scope, rel):
        sel = self._sel(el)
        try:
            self._browser().type_into(sel, str(self._val(el.get("Text"), scope)))
        except ElementNotFound as exc:
            raise WorkflowFault(str(exc)) from exc
        self._note(rel, el, f"type into #{sel.get('id', sel)}")

    def _a_Click(self, el, scope, rel):
        sel = self._sel(el)
        try:
            self._browser().click(sel)
        except ElementNotFound as exc:
            raise WorkflowFault(str(exc)) from exc
        self._note(rel, el, f"click #{sel.get('id', sel)} -> {self._browser().url}")

    def _a_GetText(self, el, scope, rel):
        sel = self._sel(el)
        try:
            value = self._browser().get_text(sel)
        except ElementNotFound as exc:
            raise WorkflowFault(str(exc)) from exc
        self._out(self._arg_text(el, "Value"), scope, value)
        self._note(rel, el, f"#{sel.get('id', sel)} = {value!r}")

    def _a_UiElementExists(self, el, scope, rel):
        sel = self._sel(el)
        found = self._browser().exists(sel)
        self._out(self._arg_text(el, "Exists"), scope, found)
        self._note(rel, el, f"#{sel.get('id', sel)} exists = {found}")

    # -- http -------------------------------------------------------------
    def _a_HttpClient(self, el, scope, rel):
        url = str(self._val(el.get("EndPoint"), scope))
        if not any(url == p or url.startswith(p + "/") for p in self.allowed):
            raise Blocked(f"HTTP request to {url} is outside the bound test environment")
        headers = {}
        p = prop(el, "Headers")
        for a in (p if p is not None else []):
            headers[a.get(f"{{{X_NS}}}Key")] = str(self._val((a.text or "").strip(), scope))
        body = self._val(el.get("Body"), scope) if el.get("Body") is not None else None
        if body is not None:
            headers.setdefault("Content-Type", el.get("BodyFormat", "application/json"))
        method = el.get("Method", "GET").upper()
        resp = self.http.request(method, url, headers=headers, content=None if body is None else str(body).encode())
        self._out(self._arg_text(el, "Result"), scope, resp.text)
        self._out(self._arg_text(el, "StatusCode"), scope, resp.status_code)
        self._note(rel, el, f"{method} {url} -> {resp.status_code}")

    # -- terminal ---------------------------------------------------------
    def _term(self) -> Tn3270Client:
        if self.terminal is None:
            raise WorkflowFault("terminal activity used outside a Terminal Session scope")
        return self.terminal

    def _a_TerminalSession(self, el, scope, rel):
        conn = str(self._val(el.get("ConnectionString"), scope))
        host, _, port = conn.rpartition(":")
        if (host, int(port or 0)) not in self.allowed_hosts:
            raise Blocked(f"terminal connection to {conn} is outside the bound test environment")
        self.terminal = Tn3270Client(host, int(port))
        self._note(rel, el, f"connect tn3270 {conn}")
        try:
            for c in body_activities(el, "Body"):
                self._exec(c, scope, rel)
        finally:
            self.terminal.close()
            self.terminal = None

    def _a_TerminalSetFieldAtPosition(self, el, scope, rel):
        row, col = int(self._val(el.get("Row"), scope)), int(self._val(el.get("Column"), scope))
        try:
            self._term().set_field(row, col, str(self._val(el.get("Text"), scope)))
        except ValueError as exc:
            raise WorkflowFault(str(exc)) from exc
        self._note(rel, el, f"set field at {row},{col}")

    def _a_TerminalSendControlKey(self, el, scope, rel):
        key = CONTROL_KEYS.get((el.get("Key") or "Transmit").upper())
        if key is None:
            raise WorkflowFault(f"unknown control key {el.get('Key')}")
        self._term().send(key)
        self._note(rel, el, f"send {key}")

    def _a_TerminalGetTextAtPosition(self, el, scope, rel):
        row, col = int(self._val(el.get("Row"), scope)), int(self._val(el.get("Column"), scope))
        value = self._term().get(row, col, int(self._val(el.get("Length"), scope)))
        self._out(self._arg_text(el, "Text"), scope, value)
        self._note(rel, el, f"text at {row},{col} = {value!r}")

    def _a_TerminalWaitScreenText(self, el, scope, rel):
        text = str(self._val(el.get("Text"), scope))
        # The twin host answers every AID synchronously, so the screen is already final.
        if text not in self._term().text():
            raise WorkflowFault(f"screen text {text!r} did not appear")
        self._note(rel, el, f"screen shows {text!r}")
