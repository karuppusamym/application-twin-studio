"""Tiny, dependency-free helper for automations that run against a twin.

Any agent framework can use it (or ignore it and read the JSON itself):

    from ats.sdk import Twin
    twin = Twin.from_env()
    case = twin.http("qaqc").get("/cases/QC-1001")
    host = twin.terminal("corebank")
    twin.finish({"status": "DONE"})
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Any

from .runtime.tn3270 import Tn3270Client


class HttpResult:
    def __init__(self, status: int, text: str):
        self.status, self.text = status, text

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class HttpApp:
    def __init__(self, base: str, bearer: str | None):
        self.base, self.bearer = base.rstrip("/"), bearer

    def request(self, method: str, path: str, body: Any = None, headers: dict | None = None) -> HttpResult:
        h = {"Accept": "application/json", **(headers or {})}
        if self.bearer:
            h["Authorization"] = f"Bearer {self.bearer}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return HttpResult(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, exc.read().decode())

    def get(self, path: str, **kw: Any) -> HttpResult:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> HttpResult:
        return self.request("POST", path, body if body is not None else {}, **kw)


class Twin:
    def __init__(self, doc: dict):
        self.apps, self.inputs, self.arguments = doc["apps"], doc.get("inputs") or {}, doc.get("arguments") or {}
        self.output_path = doc["output_path"]

    @classmethod
    def from_env(cls) -> "Twin":
        with open(os.environ["ATS_BINDINGS"], encoding="utf-8") as fh:
            return cls(json.load(fh))

    def http(self, app: str) -> HttpApp:
        b = self.apps[app]
        return HttpApp(b["http"], (b.get("auth") or {}).get("bearer"))

    def terminal(self, app: str) -> Tn3270Client:
        t = self.apps[app]["tn3270"]
        return Tn3270Client(t["host"], t["port"])

    def sql(self, app: str) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.apps[app]['sql']['path']}?mode=ro", uri=True)

    def credentials(self, app: str) -> dict:
        return self.apps[app].get("credentials") or {}

    def finish(self, result: dict) -> None:
        with open(self.output_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
