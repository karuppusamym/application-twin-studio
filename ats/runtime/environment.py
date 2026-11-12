"""Isolated scenario environments.

One environment per run: its own directory, one database per application,
its own bearer token, fault plan, virtual clock and TN3270 ports. Nothing
mutable is shared between two environments, which is what lets the original
and the replacement automation run from identical seeds without touching
each other.
"""
from __future__ import annotations

import copy
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any

from .engine import Result, TwinApp
from .screens import ScreenSession
from .store import Store
from .tn3270 import Tn3270Server
from .web import WebUI


class EnvironmentError_(Exception):
    pass


class FaultPlan:
    """Faults are explicit data keyed by operation and call occurrence."""

    def __init__(self, faults: list | None):
        self.faults = faults or []
        self.counts: dict[tuple, int] = {}
        self.lock = threading.Lock()

    def take(self, app: str, op: str) -> dict | None:
        with self.lock:
            n = self.counts[(app, op)] = self.counts.get((app, op), 0) + 1
        for f in self.faults:
            occ = f.get("occurrence", 1)
            if f["app"] == app and f["operation"] == op and (occ == "all" or n == occ or (isinstance(occ, list) and n in occ)):
                return f
        return None


def apply_patches(fixture: dict, scenario: dict | None) -> dict:
    """Scenario-specific anomalies are applied to a copy; the fixture is immutable input."""
    fx = copy.deepcopy(fixture)
    sc = scenario or {}
    data = fx.setdefault("data", {})
    for p in sc.get("patch") or []:
        rows = data.get(p["app"], {}).get(p["entity"], [])
        hit = [r for r in rows if str(r.get(p["key_field"])) == str(p["key"])]
        if not hit:
            raise EnvironmentError_(f"patch target {p['app']}.{p['entity']} {p['key']} not in fixture")
        hit[0].update(p["set"])
    for p in sc.get("remove") or []:
        rows = data.get(p["app"], {}).get(p["entity"], [])
        data[p["app"]][p["entity"]] = [r for r in rows if str(r.get(p["key_field"])) != str(p["key"])]
    for p in sc.get("add") or []:
        data.setdefault(p["app"], {}).setdefault(p["entity"], []).append(p["row"])
    return fx


class Environment:
    def __init__(self, env_id: str, root: Path, specs: dict[str, dict], base_url: str, faults: list | None = None):
        self.id = env_id
        self.dir = Path(root) / env_id
        self.specs = specs
        self.base_url = base_url.rstrip("/")
        self.faults = FaultPlan(faults)
        self.clock = "2026-01-01T00:00:00Z"
        self.token = "ats-test-" + secrets.token_hex(12)
        self.apps: dict[str, TwinApp] = {}
        self.web: dict[str, WebUI] = {}
        self.tn: dict[str, Tn3270Server] = {}
        self.term_sessions: dict[str, ScreenSession] = {}
        self.credentials: dict = {}
        self.calls: list[dict] = []
        self.status = "PROVISIONING"
        self._trace = 0
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------
    def provision(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=False)
        for app_id, spec in self.specs.items():
            for dep in spec.get("dependencies") or []:
                if dep.get("required", True) and dep["application_id"] not in self.specs:
                    raise EnvironmentError_(f"{app_id} requires dependency {dep['application_id']} which is not bound")
            store = Store(self.dir / f"{app_id}.db", spec["entities"])
            app = TwinApp(spec, store, self)
            self.apps[app_id] = app
            protocols = {i["protocol"] for i in spec["interfaces"]}
            if "web" in protocols and spec.get("ui"):
                self.web[app_id] = WebUI(app, f"/env/{self.id}/{app_id}")
            if "tn3270" in protocols and spec.get("terminal"):
                self.tn[app_id] = Tn3270Server(lambda app=app: ScreenSession(app))

    def seed(self, fixture: dict, anomalies: list | None = None) -> None:
        """All-or-nothing: a partially seeded environment is never runnable."""
        self.clock = fixture.get("clock", self.clock)
        self.credentials = fixture.get("credentials") or {}
        data = fixture.get("data") or {}
        for app_id, entities in data.items():
            if app_id not in self.apps:
                continue  # fixture may cover more apps than this environment binds
            app = self.apps[app_id]
            for entity, rows in entities.items():
                if entity not in app.spec["entities"]:
                    raise EnvironmentError_(f"fixture: {app_id} has no entity {entity}")
                for row in rows:
                    app.store.insert(entity, row)
        allowed = set(anomalies or [])
        problems = []
        for ref in fixture.get("references") or []:
            if ref["from"] in allowed:
                continue
            fa, fe, ff = ref["from"].split(".")
            ta, te, tf = ref["to"].split(".")
            if fa not in self.apps or ta not in self.apps:
                continue
            targets = {str(r[tf]) for r in self.apps[ta].store.query(te)}
            for row in self.apps[fa].store.query(fe):
                if row[ff] not in (None, "") and str(row[ff]) not in targets:
                    problems.append(f"{ref['from']}={row[ff]} has no match in {ref['to']}")
        if problems:
            self.status = "SEED_FAILED"
            raise EnvironmentError_("fixture reference check failed: " + "; ".join(problems))
        self.status = "READY"

    def destroy(self) -> None:
        self.status = "DESTROYED"
        for srv in self.tn.values():
            srv.close()
        for app in self.apps.values():
            app.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- runtime services -------------------------------------------------
    def next_trace(self) -> str:
        with self._lock:
            self._trace += 1
            return f"{self.id}-{self._trace:05d}"

    def record_call(self, app: str, op: str, caller: str, fault: dict | None) -> None:
        with self._lock:
            self.calls.append({"n": len(self.calls) + 1, "app": app, "operation": op, "caller": caller,
                               "fault": fault["type"] if fault else None})

    def call(self, caller_app: str, dep: str, op: str, path: dict, query: dict, body: Any) -> Result:
        """The gateway: routes by application id inside this environment only."""
        target = self.apps.get(dep)
        if target is None:
            return Result(503, {"code": "DEPENDENCY_UNAVAILABLE", "message": f"{dep} is not bound in this environment",
                                "trace_id": self.next_trace(), "retryable": False, "field_errors": []})
        return target.invoke(op, path=path, query=query, body=body, caller=f"app:{caller_app}")

    def open_terminal(self, app_id: str) -> str:
        sid = secrets.token_hex(8)
        self.term_sessions[sid] = ScreenSession(self.apps[app_id])
        return sid

    def bindings(self) -> dict:
        out: dict = {}
        for app_id, app in self.apps.items():
            root = f"{self.base_url}/env/{self.id}/{app_id}"
            b: dict = {"interfaces": {}}
            for itf in app.spec["interfaces"]:
                proto = itf["protocol"]
                if proto == "http":
                    b["http"] = root
                    if (itf.get("auth") or {}).get("type") == "bearer":
                        b["auth"] = {"bearer": self.token}
                elif proto == "web" and app_id in self.web:
                    b["web"] = root + "/ui"
                elif proto == "tn3270" and app_id in self.tn:
                    b["tn3270"] = {"host": self.tn[app_id].host, "port": self.tn[app_id].port}
                elif proto == "sql":
                    b["sql"] = {"engine": "sqlite", "path": str(app.store.path)}
                b["interfaces"][itf["id"]] = proto
            if app_id in self.credentials:
                b["credentials"] = self.credentials[app_id]
            out[app_id] = b
        return out

    def snapshot(self) -> dict:
        return {
            "state": {a: app.store.snapshot() for a, app in self.apps.items()},
            "events": {a: app.store.events() for a, app in self.apps.items()},
            "calls": list(self.calls),
        }


def find_cycle(specs: dict[str, dict]) -> list[str] | None:
    graph = {a: [d["application_id"] for d in s.get("dependencies") or []] for a, s in specs.items()}
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> list[str] | None:
        if state.get(node) == 1:
            return trail[trail.index(node):] + [node]
        if state.get(node) == 2 or node not in graph:
            return None
        state[node] = 1
        for nxt in graph[node]:
            found = visit(nxt, trail + [node])
            if found:
                return found
        state[node] = 2
        return None

    for n in graph:
        found = visit(n, [])
        if found:
            return found
    return None


class EnvironmentManager:
    def __init__(self, root: Path, base_url: str):
        self.root = Path(root)
        self.base_url = base_url
        self.envs: dict[str, Environment] = {}
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def sweep_orphans(self) -> int:
        """Environments are in-memory twins; directories left by a crashed process are orphans."""
        n = 0
        for child in self.root.iterdir():
            if child.is_dir() and child.name not in self.envs:
                shutil.rmtree(child, ignore_errors=True)
                n += 1
        return n

    def provision(self, specs: dict[str, dict], fixture: dict, scenario: dict | None = None, label: str = "env") -> Environment:
        cycle = find_cycle(specs)
        if cycle:
            raise EnvironmentError_("dependency cycle without defined semantics: " + " -> ".join(cycle))
        env_id = f"{label}-{secrets.token_hex(4)}"
        env = Environment(env_id, self.root, specs, self.base_url, (scenario or {}).get("faults"))
        try:
            env.provision()
            env.seed(apply_patches(fixture, scenario), (scenario or {}).get("intentional_anomalies"))
        except Exception:
            env.destroy()
            raise
        with self.lock:
            self.envs[env_id] = env
        return env

    def get(self, env_id: str) -> Environment | None:
        return self.envs.get(env_id)

    def destroy(self, env_id: str) -> bool:
        with self.lock:
            env = self.envs.pop(env_id, None)
        if env:
            env.destroy()
        return env is not None
