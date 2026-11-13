"""Schema-rendered web UI for a twin.

Pages are server-rendered HTML forms with stable element ids, so UiPath,
Selenium, Playwright or the bundled HTML driver can all drive them. Every
button posts to the same operation handlers the API uses; the UI holds no
business rule of its own.

Stable id convention (what selectors should target):
  field-<name>            value on a detail page
  <input id>              form inputs (id from spec, defaults to the field name)
  table-<page>, row-<key>, cell-<key>-<field>
  message / error-message flash and error banners
"""
from __future__ import annotations

import re
import secrets
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

from .. import expr

_CSS = """
body{font:14px/1.45 Segoe UI,Arial,sans-serif;margin:0;background:#f3f5f8;color:#1c2733}
header{background:#12355b;color:#fff;padding:10px 20px;display:flex;gap:18px;align-items:center}
header b{font-size:16px}header a{color:#cfe1f5;text-decoration:none}header span{margin-left:auto;font-size:12px;opacity:.85}
main{max-width:980px;margin:22px auto;background:#fff;border:1px solid #d7dde5;border-radius:6px;padding:20px 24px}
h1{font-size:19px;margin:0 0 14px}h2{font-size:15px;margin:22px 0 8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #e3e8ee}
th{background:#f0f3f7;font-weight:600}dl{display:grid;grid-template-columns:190px 1fr;gap:6px 14px;margin:0}
dt{color:#5b6875}dd{margin:0;font-weight:600}label{display:block;margin:10px 0 3px;color:#42505e}
input,select{padding:6px 8px;border:1px solid #b9c3cf;border-radius:4px;min-width:240px;font:inherit}
button{padding:7px 14px;border:0;border-radius:4px;background:#1d5fa8;color:#fff;font:inherit;cursor:pointer;margin:12px 8px 0 0}
#error-message{background:#fdecea;border:1px solid #f1b0a8;color:#8a1c12;padding:8px 12px;border-radius:4px;margin-bottom:12px}
#message{background:#e8f5ec;border:1px solid #a9d6b6;color:#17602e;padding:8px 12px;border-radius:4px;margin-bottom:12px}
.twin{font-size:11px;color:#7a8794;margin-top:18px}form.inline{display:inline-block;margin-right:10px;vertical-align:top}
"""


def _rx(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", r"(?P<\1>[^/]+)", path.rstrip("/")) + "/?$")


def _fill(template: str, values: dict) -> str:
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: quote(str(values.get(m.group(1), "")), safe=""), template)


class WebUI:
    def __init__(self, app: Any, prefix: str):
        self.app, self.prefix = app, prefix
        self.ui = app.spec.get("ui") or {}
        self.pages = [(_rx(p["path"]), p) for p in self.ui.get("pages") or []]
        self.session_cfg = self.ui.get("session")
        self.sessions: dict[str, dict] = {}

    # Response is a plain dict so the HTTP layer stays thin.
    def _redirect(self, path: str, cookie: tuple | None = None, **flash: str) -> dict:
        qs = urlencode({k: v for k, v in flash.items() if v})
        return {"status": 303, "location": self.prefix + path + (("&" if "?" in path else "?") + qs if qs else ""), "cookie": cookie}

    def handle(self, method: str, path: str, query: dict, form: dict, cookies: dict) -> dict:
        path = path.rstrip("/") or "/"
        if path in ("/ui", "/"):
            return self._redirect(self.ui.get("home", "/ui"))
        cookie_name = (self.session_cfg or {}).get("cookie", "ATSSESSION")
        session = self.sessions.get(cookies.get(cookie_name, ""))
        if path == "/ui/logout":
            self.sessions.pop(cookies.get(cookie_name, ""), None)
            return self._redirect((self.session_cfg or {}).get("login_page", "/ui"), _msg="Signed out")

        action_id = None
        m = re.match(r"^(.*)/_action/([A-Za-z0-9_-]+)$", path)
        if m:
            path, action_id = m.group(1), m.group(2)
        for rx, page in self.pages:
            hit = rx.match(path)
            if not hit:
                continue
            params = hit.groupdict()
            if self.session_cfg and not page.get("public") and session is None:
                return self._redirect(self.session_cfg["login_page"], _err="Please sign in")
            caller = f"ui:{session['user']}" if session else "ui"
            if method == "POST":
                return self._post(page, params, form, action_id, caller, cookie_name)
            return {"status": 200, "html": self._render(page, params, query, session, caller)}
        return {"status": 404, "html": self._shell("Not found", "<p id='error-message'>This twin has no such page.</p>", None)}

    # -- POST -------------------------------------------------------------
    def _post(self, page: dict, params: dict, form: dict, action_id: str | None, caller: str, cookie_name: str) -> dict:
        here = _fill(page["path"], params)
        if page["type"] == "form" and action_id is None:
            res = self.app.invoke(page["operation"], path=params, body=dict(form), caller=caller)
            if not res.ok:
                return self._redirect(here, _err=res.body.get("message", "Request failed"))
            cookie = None
            if page.get("login"):
                sid = secrets.token_hex(16)
                self.sessions[sid] = {"user": form.get((self.session_cfg or {}).get("user_field", "username"), "user")}
                cookie = (cookie_name, sid)
            target = _fill(page.get("on_success", self.ui.get("home", "/ui")), {**params, **(res.body or {})})
            return self._redirect(target, cookie=cookie, _msg=page.get("success_message", ""))
        action = next((a for a in page.get("actions") or [] if a["id"] == action_id), None)
        if action is None:
            return {"status": 404, "html": self._shell("Not found", "<p id='error-message'>Unknown action.</p>", None)}
        body = {k: v for k, v in form.items()}
        body.update(expr.resolve(action.get("body") or {}, {"path": params, "form": form}))
        res = self.app.invoke(action["operation"], path=params, body=body, caller=caller)
        if not res.ok:
            # error_style "message": show only the host message line, as screen-to-web tools (HATS etc.) do
            text = res.body.get("message") if self.ui.get("error_style") == "message" else f"{res.body.get('code')}: {res.body.get('message')}"
            return self._redirect(here, _err=text)
        return self._redirect(_fill(action.get("on_success", page["path"]), {**params, **(res.body if isinstance(res.body, dict) else {})}),
                              _msg=action.get("success_message", f"{action['label']} completed"))

    # -- GET --------------------------------------------------------------
    def _render(self, page: dict, params: dict, query: dict, session: dict | None, caller: str) -> str:
        e = escape
        parts = []
        if query.get("_err"):
            parts.append(f"<div id='error-message' role='alert'>{e(query['_err'])}</div>")
        if query.get("_msg"):
            parts.append(f"<div id='message' role='status'>{e(query['_msg'])}</div>")
        here = self.prefix + _fill(page["path"], params)

        if page["type"] == "form":
            parts.append(f"<form id='form-{e(page['id'])}' method='post' action='{e(here)}'>")
            parts += [self._input(f, f.get("id", f["name"])) for f in page.get("fields") or []]
            sub = page.get("submit") or {}
            parts.append(f"<button type='submit' id='{e(sub.get('id', 'btn-submit'))}'>{e(sub.get('label', 'Submit'))}</button></form>")
            return self._shell(page.get("title", ""), "".join(parts), session)

        q = {k: v for k, v in query.items() if not k.startswith("_") and v != ""}
        res = self.app.invoke(page["operation"], path=params, query=q, caller=caller)
        if not res.ok:
            parts.append(f"<div id='error-message' role='alert'>{e(str(res.body.get('code')))}: {e(str(res.body.get('message')))}</div>")
            return self._shell(page.get("title", ""), "".join(parts), session)
        ctx = {"result": res.body, "path": params}

        if page["type"] == "table":
            if page.get("filters"):
                parts.append(f"<form id='filter-{e(page['id'])}' method='get' action='{e(here)}'>")
                for f in page["filters"]:
                    parts.append(self._input(f, f.get("id", "filter-" + f["name"]), q.get(f["name"], "")))
                parts.append("<button type='submit' id='btn-filter'>Filter</button></form>")
            parts.append(self._table(page["id"], page, expr.resolve(page.get("rows", "$result.items"), ctx) or []))
        else:  # detail
            parts.append("<dl>")
            for f in page.get("fields") or []:
                value = expr.lookup(res.body, f["field"]) if isinstance(res.body, dict) else None
                parts.append(f"<dt>{e(f.get('label', f['field']))}</dt><dd id='field-{e(f['field'])}' data-field='{e(f['field'])}'>"
                             f"{e('' if value is None else str(value))}</dd>")
            parts.append("</dl>")
            for a in page.get("actions") or []:
                parts.append(f"<form class='inline' id='form-{e(a['id'])}' method='post' action='{e(here)}/_action/{e(a['id'])}'>")
                parts += [self._input(f, f.get("id", f"{a['id']}-{f['name']}")) for f in a.get("fields") or []]
                parts.append(f"<button type='submit' id='{e(a['id'])}'>{e(a['label'])}</button></form>")
            for rel in page.get("related") or []:
                rres = self.app.invoke(rel["operation"], path=params, caller=caller)
                rows = expr.resolve(rel.get("rows", "$result.items"), {"result": rres.body}) if rres.ok else []
                parts.append(f"<h2>{e(rel.get('title', rel['id']))}</h2>" + self._table(rel["id"], rel, rows or []))
        return self._shell(page.get("title", ""), "".join(parts), session)

    def _input(self, f: dict, element_id: str, value: str = "") -> str:
        e = escape
        label = f"<label for='{e(element_id)}'>{e(f.get('label', f['name']))}</label>"
        if f.get("options"):
            opts = "".join(f"<option value='{e(str(o))}'{' selected' if str(o) == value else ''}>{e(str(o))}</option>" for o in f["options"])
            return f"{label}<select id='{e(element_id)}' name='{e(f['name'])}'>{opts}</select>"
        return (f"{label}<input id='{e(element_id)}' name='{e(f['name'])}' type='{e(f.get('type', 'text'))}' "
                f"value='{e(value)}' aria-label='{e(f.get('label', f['name']))}'>")

    def _table(self, table_id: str, cfg: dict, rows: list) -> str:
        e = escape
        cols = cfg.get("columns") or []
        key = cfg.get("key")
        head = "".join(f"<th>{e(c.get('label', c['field']))}</th>" for c in cols)
        body = []
        for row in rows:
            k = str(row.get(key, "")) if key else ""
            cells = []
            for c in cols:
                text = e("" if row.get(c["field"]) is None else str(row.get(c["field"])))
                if c.get("link"):
                    text = f"<a id='link-{e(k)}' href='{e(self.prefix + _fill(c['link'], row))}'>{text}</a>"
                cells.append(f"<td id='cell-{e(k)}-{e(c['field'])}'>{text}</td>")
            body.append(f"<tr id='row-{e(k)}'>{''.join(cells)}</tr>")
        return (f"<table id='table-{e(table_id)}'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
                f"<p id='count-{e(table_id)}' class='twin'>{len(rows)} row(s)</p>")

    def _shell(self, title: str, content: str, session: dict | None) -> str:
        e = escape
        nav = "".join(f"<a id='nav-{e(n['id'])}' href='{e(self.prefix + n['path'])}'>{e(n['label'])}</a>" for n in self.ui.get("nav") or [])
        who = (f"<span id='current-user'>{e(session['user'])} · <a id='nav-logout' href='{e(self.prefix)}/ui/logout'>Sign out</a></span>"
               if session else "<span></span>")
        app_title = self.ui.get("title", self.app.app_id)
        return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)} - {e(app_title)}</title>"
                f"<style>{_CSS}</style></head><body><header><b>{e(app_title)}</b>{nav}{who}</header>"
                f"<main><h1 id='page-title'>{e(title)}</h1>{content}"
                f"<p class='twin'>Application twin · {e(self.app.app_id)} {e(str(self.app.spec['spec_version']))} · "
                f"environment {e(self.app.env.id)} · synthetic data</p></main></body></html>")
