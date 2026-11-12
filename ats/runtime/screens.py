"""Protocol-independent terminal screen state machine.

The TN3270 server and the portal's web terminal both drive this class, and
every key press that changes data goes through ``TwinApp.invoke`` — the same
handlers the HTTP API uses.
"""
from __future__ import annotations

from typing import Any

from .. import expr


class ScreenSession:
    def __init__(self, app: Any):
        self.app = app
        self.term = app.spec["terminal"]
        self.rows = int(self.term.get("rows", 24))
        self.cols = int(self.term.get("cols", 80))
        self.screen = self.term["start"]
        self.vars: dict = {}
        self.message = ""
        self.closed = False

    # -- rendering --------------------------------------------------------
    def _ctx(self, fields: dict, **extra: Any) -> dict:
        return {"field": fields, "s": self.vars, "message": self.message, "now": self.app.env.clock, **extra}

    def layout(self) -> dict:
        scr = self.term["screens"][self.screen]
        ctx = self._ctx({})
        fields = []
        for row, col, text in scr.get("text") or []:
            fields.append({"row": row, "col": col, "len": len(text), "text": text, "protected": True})
        for o in scr.get("outputs") or []:
            value = expr.resolve(o.get("value"), ctx)
            text = ("" if value is None else str(value))[: o["len"]].ljust(o["len"])
            fields.append({"row": o["row"], "col": o["col"], "len": o["len"], "text": text, "protected": True,
                           "name": o.get("name"), "intense": bool(o.get("intense"))})
        inputs = scr.get("inputs") or []
        for i in inputs:
            fields.append({"row": i["row"], "col": i["col"], "len": i["len"], "text": "", "protected": False,
                           "name": i["name"], "hidden": bool(i.get("hidden"))})
        cursor = [inputs[0]["row"], inputs[0]["col"]] if inputs else [1, 1]
        return {"screen": self.screen, "rows": self.rows, "cols": self.cols, "fields": fields, "cursor": cursor,
                "keys": sorted((scr.get("keys") or {}).keys())}

    def text_rows(self) -> list[str]:
        grid = [[" "] * self.cols for _ in range(self.rows)]
        for f in self.layout()["fields"]:
            if f.get("hidden"):
                continue
            for n, ch in enumerate(f["text"]):
                c = f["col"] - 1 + n
                if c < self.cols:
                    grid[f["row"] - 1][c] = ch
        return ["".join(r) for r in grid]

    # -- input ------------------------------------------------------------
    def send_by_address(self, aid: str, by_addr: dict[int, str]) -> None:
        scr = self.term["screens"][self.screen]
        fields = {}
        for i in scr.get("inputs") or []:
            addr = (i["row"] - 1) * self.cols + (i["col"] - 1)
            if addr in by_addr:
                fields[i["name"]] = by_addr[addr]
        self.send(aid, fields)

    def send(self, aid: str, fields: dict | None = None) -> None:
        if self.closed:
            return
        scr = self.term["screens"][self.screen]
        clean = {i["name"]: "" for i in scr.get("inputs") or []}
        for name, value in (fields or {}).items():
            if name in clean:
                clean[name] = str(value).replace("\x00", "").strip()
        self.message = ""
        action = (scr.get("keys") or {}).get(aid.upper())
        if action is None:
            self.message = f"INVALID KEY {aid.upper()}"
            return
        self._do(action, self._ctx(clean))

    def _do(self, action: dict, ctx: dict) -> None:
        if "switch" in action:
            value = str(expr.resolve(action["switch"], ctx) or "")
            branch = (action.get("cases") or {}).get(value, action.get("default"))
            if branch:
                self._do(branch, ctx)
            return
        if "operation" in action:
            res = self.app.invoke(
                action["operation"],
                path=expr.resolve(action.get("path") or {}, ctx),
                query=expr.resolve(action.get("query") or {}, ctx),
                body=expr.resolve(action.get("body"), ctx),
                caller="terminal",
            )
            if res.ok:
                nxt = action.get("on_success") or {}
                ctx = {**ctx, "result": res.body}
            else:
                nxt = action.get("on_error") or {"message": "$error.message"}
                ctx = {**ctx, "error": res.body}
            self._do(nxt, ctx)
            return
        if "set" in action:
            self.vars.update(expr.resolve(action["set"], ctx))
        for name in action.get("clear") or []:
            self.vars.pop(name, None)
        if "message" in action:
            self.message = str(expr.resolve(action["message"], ctx) or "")
        if action.get("goto"):
            self.screen = action["goto"]
        if action.get("disconnect"):
            self.closed = True
