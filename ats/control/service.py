"""Control-plane logic: onboarding, runs, comparisons, suites, manual environments.

Run pipeline (each stage is recorded with status and duration):
  GATE -> PROVISION+SEED -> PREFLIGHT -> EXECUTE -> COLLECT -> ASSERT -> CLEANUP

Local-profile limits, stated plainly: jobs run on an in-process thread pool
and their records live in SQLite. A control-plane restart marks unfinished
runs as ERROR and discards their environments; there is no lease/fencing or
multi-worker recovery yet (that is the PostgreSQL design in the blueprint).
"""
from __future__ import annotations

import copy
import io
import json
import re
import secrets
import shutil
import sqlite3
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from .. import onboarding, runners, validation
from ..runtime.environment import Environment, EnvironmentError_, EnvironmentManager
from ..spec import SpecError, capability_manifest, digest, prepare
from ..workspace import Workspace

TERMINAL = {"PASSED", "FAILED", "ERROR", "BLOCKED", "DONE"}
MAX_ZIP_BYTES, MAX_ZIP_FILES, MAX_UNZIPPED = 20 * 1024 * 1024, 500, 100 * 1024 * 1024


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class DocStore:
    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS docs (kind TEXT, id TEXT, created REAL, body TEXT, PRIMARY KEY (kind, id))")

    def put(self, kind: str, doc: dict) -> dict:
        with self.lock:
            self.db.execute("INSERT INTO docs (kind, id, created, body) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(kind, id) DO UPDATE SET body = excluded.body",
                            (kind, doc["id"], time.time(), json.dumps(doc, default=str)))
        return doc

    def get(self, kind: str, doc_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT body FROM docs WHERE kind = ? AND id = ?", (kind, doc_id)).fetchone()
        if row is None:
            raise NotFound(f"{kind} {doc_id} not found")
        return json.loads(row[0])

    def list(self, kind: str, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM docs WHERE kind = ? ORDER BY created DESC LIMIT ?", (kind, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def apply_mutation(specs: dict[str, dict], mutation: dict) -> dict[str, dict]:
    """Deliberately break one twin rule (negative test: the oracle must notice)."""
    specs = dict(specs)
    spec = copy.deepcopy({k: v for k, v in specs[mutation["app"]].items() if not k.startswith("_")})
    op = next(o for o in spec["operations"] if o["id"] == mutation["operation"])
    for step in op["steps"]:
        for rule in (step.get("decide") or {}).get("rules") or []:
            if rule.get("id") == mutation["rule_id"]:
                rule["set"] = mutation["set"]
                specs[mutation["app"]] = prepare(spec)
                return specs
    raise Conflict(f"mutation {mutation['id']}: rule {mutation['rule_id']} not found")


class Studio:
    def __init__(self, repo_root: Path, data_dir: Path, base_url: str):
        self.repo_root, self.data = Path(repo_root), Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.upload_root = self.repo_root / "workspace"
        self.ws = Workspace([self.repo_root / "samples", self.upload_root])
        self.envs = EnvironmentManager(self.data / "environments", base_url)
        self.docs = DocStore(self.data / "control.db")
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ats-job")
        self.manual: dict[str, dict] = {}
        self.recovered = self._recover()

    def _recover(self) -> dict:
        interrupted = 0
        for kind in ("run", "comparison", "suite"):
            for doc in self.docs.list(kind, 1000):
                if doc.get("status") not in TERMINAL:
                    doc.update(status="ERROR", error="interrupted by a control-plane restart; environment discarded")
                    self.docs.put(kind, doc)
                    interrupted += 1
        return {"interrupted_jobs": interrupted, "orphan_environments_removed": self.envs.sweep_orphans()}

    # -- catalogue --------------------------------------------------------
    def applications(self) -> list[dict]:
        specs = self.ws.specs()
        autos = self.ws.automations()
        used: dict[str, list] = {}
        for a in autos.values():
            try:
                for app in onboarding.analyse(a, specs).get("applications", []):
                    used.setdefault(app, []).append(a["id"])
            except Exception:
                continue
        out = []
        for app_id, s in specs.items():
            levels = sorted({o.get("verification", "hypothesis") for o in s["operations"]})
            out.append({"application_id": app_id, "name": s.get("name", app_id), "spec_version": s["spec_version"],
                        "digest": s["_digest"], "interfaces": s["interfaces"], "operations": len(s["operations"]),
                        "entities": list(s["entities"]), "dependencies": [d["application_id"] for d in s.get("dependencies") or []],
                        "verification_levels": levels, "fidelity": s.get("fidelity") or {}, "used_by": used.get(app_id, []),
                        "has_ui": bool(s.get("ui")), "has_terminal": bool(s.get("terminal"))})
        return out

    def application(self, app_id: str) -> dict:
        specs = self.ws.specs()
        if app_id not in specs:
            raise NotFound(f"application {app_id} not found")
        s = specs[app_id]
        return {"spec": {k: v for k, v in s.items() if not k.startswith("_")}, "digest": s["_digest"],
                "capabilities": capability_manifest(s),
                "source": Path(s["_file"]).read_text(encoding="utf-8")}

    def automation(self, automation_id: str) -> dict:
        autos = self.ws.automations()
        if automation_id not in autos:
            raise NotFound(f"automation {automation_id} not found")
        return autos[automation_id]

    def scenario(self, scenario_id: str) -> dict:
        scs = self.ws.scenarios()
        if scenario_id not in scs:
            raise NotFound(f"scenario {scenario_id} not found")
        return scs[scenario_id]

    def coverage(self) -> list[dict]:
        """Which processes exist, through which interface combinations, with how many scenarios."""
        autos, scenarios = self.ws.automations(), self.ws.scenarios()
        out = []
        for process in sorted({a.get("process") for a in autos.values()} | {s["process"] for s in scenarios.values()}):
            mine = [a for a in autos.values() if a.get("process") == process]
            pick = lambda role: [{"id": a["id"], "name": a.get("name"), "tool": a.get("tool"), "combination": a.get("combination", "")}
                                 for a in mine if a.get("role") == role]
            apps = sorted({app for s in scenarios.values() if s["process"] == process for app in s["apps"]})
            out.append({"process": process, "originals": pick("original"), "replacements": pick("replacement"),
                        "scenarios": sum(1 for s in scenarios.values() if s["process"] == process), "applications": apps})
        return out

    # -- onboarding -------------------------------------------------------
    def onboard(self, automation_id: str) -> dict:
        automation = self.automation(automation_id)
        result = onboarding.analyse(automation, self.ws.specs())
        doc = {"id": automation_id, "analysed_at": _now(), "automation": {k: v for k, v in automation.items() if not k.startswith("_")},
               **result}
        return self.docs.put("onboarding", doc)

    def gaps(self) -> list[dict]:
        out = []
        for a in self.ws.automations():
            out += self.onboard(a)["gaps"]
        return out

    def import_package(self, automation_id: str, data: bytes, tool: str = "uipath") -> dict:
        """Quarantined import of a zipped package: limits, no traversal, nothing executed."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,60}$", automation_id):
            raise Conflict("automation id must be lowercase letters, digits and dashes")
        if len(data) > MAX_ZIP_BYTES:
            raise Conflict("archive larger than 20 MB")
        target = (self.upload_root / "automations" / automation_id).resolve()
        if target.exists():
            raise Conflict(f"automation {automation_id} already exists")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise Conflict("not a zip archive") from exc
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise Conflict("archive has too many files or expands beyond 100 MB")
        for i in infos:
            dest = (target / i.filename).resolve()
            if target not in dest.parents:
                raise Conflict(f"archive entry escapes the package directory: {i.filename}")
        target.mkdir(parents=True)
        for i in infos:
            dest = (target / i.filename).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(i))
        if not (target / "automation.yaml").exists():
            entry = "Main.xaml"
            pj = target / "project.json"
            if pj.exists():
                entry = json.loads(pj.read_text(encoding="utf-8-sig")).get("main", entry)
            args = {}
            if tool == "uipath" and (target / entry).exists():
                from ..importers import uipath
                try:
                    args = {a["name"]: "" for a in uipath.arguments_of(uipath.safe_parse(target / entry)) if a["direction"] != "out"}
                except uipath.ImportError_:
                    args = {}  # onboarding below reports the rejected file as a blocking gap
            manifest = {"id": automation_id, "name": automation_id, "tool": tool, "version": "0.0.0", "role": "original",
                        "process": "unassigned", "entry": entry, "runner": "xaml-replay" if tool == "uipath" else "command",
                        "arguments": args, "outputs": {}}
            (target / "automation.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        return self.onboard(automation_id)

    def set_arguments(self, automation_id: str, arguments: dict) -> dict:
        automation = self.automation(automation_id)
        path = Path(automation["_dir"]) / "automation.yaml"
        if self.upload_root.resolve() not in path.resolve().parents:
            raise Conflict("sample automations are read-only; edit their automation.yaml in Git")
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["arguments"] = {str(k): str(v) for k, v in arguments.items()}
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return self.onboard(automation_id)

    # -- manual environments ---------------------------------------------
    def _specs_for(self, scenario: dict, mutation_id: str | None) -> dict[str, dict]:
        specs = self.ws.specs()
        missing = [a for a in scenario["apps"] if a not in specs]
        if missing:
            raise Conflict(f"scenario needs applications without a valid twin: {missing}")
        chosen = {a: specs[a] for a in scenario["apps"]}
        if mutation_id:
            muts = self.ws.mutations()
            if mutation_id not in muts:
                raise NotFound(f"mutation {mutation_id} not found")
            chosen = apply_mutation(chosen, muts[mutation_id])
        return chosen

    def launch(self, scenario_id: str, mutation_id: str | None = None) -> dict:
        scenario = self.scenario(scenario_id)
        fixture = self.ws.fixtures()[scenario["fixture"]]
        env = self.envs.provision(self._specs_for(scenario, mutation_id), fixture, scenario, "manual")
        self.manual[env.id] = {"env_id": env.id, "scenario_id": scenario_id, "mutation_id": mutation_id, "created_at": _now()}
        return self.environment(env.id)

    def environment(self, env_id: str) -> dict:
        env = self.envs.get(env_id)
        if env is None:
            raise NotFound(f"environment {env_id} not found")
        return {**self.manual.get(env_id, {"env_id": env_id}), "status": env.status, "clock": env.clock,
                "bindings": env.bindings(), "applications": list(env.apps)}

    def environments(self) -> list[dict]:
        return [self.environment(e) for e in list(self.envs.envs)]

    def destroy(self, env_id: str) -> None:
        self.manual.pop(env_id, None)
        if not self.envs.destroy(env_id):
            raise NotFound(f"environment {env_id} not found")

    # -- runs -------------------------------------------------------------
    def new_run(self, scenario_id: str, automation_id: str, mutation_id: str | None = None,
                variant: str | None = None, label: str = "run") -> dict:
        self.scenario(scenario_id)
        automation = self.automation(automation_id)
        if variant and variant not in (automation.get("variants") or {}):
            raise NotFound(f"automation {automation_id} has no variant {variant}")
        doc = {"id": f"{label}-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}", "kind": "run", "status": "QUEUED",
               "scenario_id": scenario_id, "automation_id": automation_id, "mutation_id": mutation_id, "variant": variant,
               "created_at": _now(), "stages": [], "assertions": [], "runner": None, "env_id": None, "error": None}
        return self.docs.put("run", doc)

    def submit_run(self, **kw: Any) -> dict:
        doc = self.new_run(**kw)
        self.pool.submit(self.execute_run, doc["id"])
        return doc

    def execute_run(self, run_id: str) -> dict:
        doc = self.docs.get("run", run_id)
        env: Environment | None = None

        def stage(name: str, status: str, started: float, detail: str = "") -> None:
            doc["stages"].append({"name": name, "status": status, "ms": int((time.monotonic() - started) * 1000), "detail": detail})
            self.docs.put("run", doc)

        def finish(status: str, error: str | None = None) -> dict:
            doc.update(status=status, error=error, finished_at=_now())
            return self.docs.put("run", doc)

        try:
            scenario = self.scenario(doc["scenario_id"])
            automation = dict(self.automation(doc["automation_id"]))
            if doc.get("variant"):
                automation["env"] = automation["variants"][doc["variant"]].get("env") or {}
            t = time.monotonic()
            doc["status"] = "GATE"
            ob = self.onboard(automation["id"])
            doc["confidence"] = ob["readiness"]["confidence"]
            if ob["readiness"]["state"] != "READY":
                stage("GATE", "BLOCKED", t, "; ".join(ob["readiness"]["blockers"][:5]))
                return finish("BLOCKED", "onboarding readiness gate is not met: " + "; ".join(ob["readiness"]["blockers"][:3]))
            stage("GATE", "OK", t, f"{ob['summary'].get('supported')}/{ob['summary'].get('nodes')} steps supported")

            t = time.monotonic()
            doc["status"] = "PROVISIONING"
            specs = self._specs_for(scenario, doc.get("mutation_id"))
            fixture = self.ws.fixtures()[scenario["fixture"]]
            doc["lock"] = {"twins": {a: {"spec_version": s["spec_version"], "digest": s["_digest"]} for a, s in specs.items()},
                           "fixture": {"id": fixture["id"], "version": fixture.get("version"), "digest": digest(fixture)},
                           "scenario_digest": digest(scenario), "automation": {"id": automation["id"], "version": automation.get("version"),
                                                                                 "package_hash": ob.get("package_hash")},
                           "mutation": doc.get("mutation_id"), "variant": doc.get("variant")}
            try:
                env = self.envs.provision(specs, fixture, scenario, "run")
            except EnvironmentError_ as exc:
                stage("PROVISION+SEED", "ERROR", t, str(exc))
                return finish("ERROR", f"environment could not be provisioned: {exc}")
            doc["env_id"] = env.id
            stage("PROVISION+SEED", "OK", t, f"environment {env.id}, {len(env.apps)} twins, clock {env.clock}")

            t = time.monotonic()
            doc["status"] = "RUNNING"
            bindings = env.bindings()
            problems = runners.preflight(automation, bindings, scenario.get("inputs") or {})
            stage("PREFLIGHT", "BLOCKED" if problems else "OK", t, "; ".join(problems))
            t = time.monotonic()
            result = runners.execute(automation, bindings, scenario.get("inputs") or {}, env.dir / "_runner")
            doc["runner"] = result
            stage("EXECUTE", result["status"], t, result.get("error") or f"{len(result['log'])} steps logged")

            t = time.monotonic()
            doc["status"] = "COMPARING"
            snapshot = env.snapshot()
            doc["calls"] = snapshot["calls"][-400:]
            doc["business_view"] = validation.business_view(snapshot, result.get("output") or {},
                                                            set(scenario.get("ignore_fields") or []))
            stage("COLLECT", "OK", t, f"{len(snapshot['calls'])} twin calls recorded")

            t = time.monotonic()
            keys = {a: {e: ent["key"] for e, ent in s["entities"].items()} for a, s in specs.items()}
            results = validation.evaluate(scenario.get("assertions") or [], snapshot, result, keys)
            unmodeled = [c for c in snapshot["calls"] if c["operation"].startswith("(unmodeled)")]
            results.append({"id": "no-unmodeled-requests", "type": "calls", "critical": True, "passed": not unmodeled,
                            "expected": 0, "actual": len(unmodeled),
                            "detail": "; ".join(sorted({c['operation'] for c in unmodeled}))[:300]})
            doc["assertions"] = results
            doc["verdict"] = validation.verdict(results)
            stage("ASSERT", doc["verdict"], t, f"{sum(r['passed'] for r in results)}/{len(results)} assertions passed")
            if result["status"] == "ERROR":
                return finish("ERROR", result.get("error"))
            if result["status"] == "BLOCKED":
                return finish("BLOCKED", result.get("error"))
            return finish("FAILED" if doc["verdict"] == "FAILED" else "PASSED")
        except (NotFound, Conflict, SpecError) as exc:
            return finish("BLOCKED", str(exc))
        except Exception as exc:  # infrastructure failure, never reported as an assertion result
            return finish("ERROR", f"{type(exc).__name__}: {exc}")
        finally:
            t = time.monotonic()
            if env is not None:
                self.envs.destroy(env.id)
                leftover = env.dir.exists()
                doc["cleanup"] = {"status": "FAILED" if leftover else "OK"}
                doc["stages"].append({"name": "CLEANUP", "status": doc["cleanup"]["status"],
                                      "ms": int((time.monotonic() - t) * 1000), "detail": "environment destroyed" if not leftover else "directory left behind"})
                self.docs.put("run", doc)

    # -- comparison -------------------------------------------------------
    def new_comparison(self, scenario_id: str, original_id: str, replacement_id: str, mutation_id: str | None = None,
                       replacement_variant: str | None = None) -> dict:
        self.scenario(scenario_id), self.automation(original_id), self.automation(replacement_id)
        doc = {"id": f"cmp-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}", "kind": "comparison", "status": "RUNNING",
               "scenario_id": scenario_id, "original_id": original_id, "replacement_id": replacement_id,
               "mutation_id": mutation_id, "replacement_variant": replacement_variant, "created_at": _now()}
        return self.docs.put("comparison", doc)

    def submit_comparison(self, **kw: Any) -> dict:
        doc = self.new_comparison(**kw)
        self.pool.submit(self.execute_comparison, doc["id"])
        return doc

    def execute_comparison(self, cmp_id: str) -> dict:
        doc = self.docs.get("comparison", cmp_id)
        try:
            a = self.execute_run(self.new_run(doc["scenario_id"], doc["original_id"], doc.get("mutation_id"), None, "orig")["id"])
            doc["original_run"] = a["id"]
            self.docs.put("comparison", doc)
            b = self.execute_run(self.new_run(doc["scenario_id"], doc["replacement_id"], doc.get("mutation_id"),
                                              doc.get("replacement_variant"), "repl")["id"])
            doc["replacement_run"] = b["id"]
            sides = {"original": a, "replacement": b}
            doc["sides"] = {k: {"run_id": r["id"], "status": r["status"], "verdict": r.get("verdict"), "env_id": r.get("env_id"),
                                "runner": (r.get("runner") or {}).get("runner"),
                                "evidence_class": (r.get("runner") or {}).get("evidence_class"),
                                "failed": [x["id"] for x in r.get("assertions") or [] if not x["passed"]],
                                "error": r.get("error")} for k, r in sides.items()}
            if any(r["status"] in ("ERROR", "BLOCKED") or not r.get("business_view") for r in (a, b)):
                doc.update(status="DONE", differences=[], certificate={
                    "result": "INCONCLUSIVE", "confidence": "none", "category": "none", "may_qualify_migration": False,
                    "limits": ["At least one side did not produce evidence (infrastructure error or blocked runner)."]})
                return self.docs.put("comparison", doc)
            doc["differences"] = validation.diff(a["business_view"], b["business_view"])
            specs = self.ws.specs()
            used = set()
            open_gaps = 0
            for auto_id in (doc["original_id"], doc["replacement_id"]):
                ob = self.docs.get("onboarding", auto_id)
                open_gaps += sum(1 for g in ob["gaps"] if g["status"] == "open")
                used |= {r["verification"] for r in ob["required_operations"]}
            del specs
            doc["certificate"] = validation.certificate(
                {"verdict": a.get("verdict", "FAILED"), "runner": a["runner"]},
                {"verdict": b.get("verdict", "FAILED"), "runner": b["runner"]}, doc["differences"], used, open_gaps)
            doc["certificate"]["lock"] = a.get("lock")
            doc["status"] = "DONE"
        except Exception as exc:
            doc.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
        doc["finished_at"] = _now()
        return self.docs.put("comparison", doc)

    # -- suites -----------------------------------------------------------
    def submit_suite(self, original_id: str, replacement_id: str, replacement_variant: str | None = None,
                     mutation_id: str | None = None) -> dict:
        process = self.automation(original_id).get("process")
        if self.automation(replacement_id).get("process") != process:
            raise Conflict("original and replacement automate different processes")
        scenario_ids = sorted(s["id"] for s in self.ws.scenarios().values() if s.get("process") == process)
        doc = {"id": f"suite-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}", "kind": "suite", "status": "RUNNING",
               "original_id": original_id, "replacement_id": replacement_id, "replacement_variant": replacement_variant,
               "mutation_id": mutation_id, "scenario_ids": scenario_ids, "results": [], "created_at": _now()}
        self.docs.put("suite", doc)
        self.pool.submit(self._execute_suite, doc["id"])
        return doc

    def _execute_suite(self, suite_id: str) -> None:
        doc = self.docs.get("suite", suite_id)
        try:
            for sid in doc["scenario_ids"]:
                c = self.execute_comparison(self.new_comparison(sid, doc["original_id"], doc["replacement_id"],
                                                                doc.get("mutation_id"), doc.get("replacement_variant"))["id"])
                doc["results"].append({"scenario_id": sid, "comparison_id": c["id"],
                                       "result": (c.get("certificate") or {}).get("result", "ERROR"),
                                       "original": (c.get("sides") or {}).get("original", {}).get("status"),
                                       "replacement": (c.get("sides") or {}).get("replacement", {}).get("status"),
                                       "differences": len(c.get("differences") or [])})
                self.docs.put("suite", doc)
            doc["status"] = "DONE"
        except Exception as exc:
            doc.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
        doc["finished_at"] = _now()
        self.docs.put("suite", doc)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        for env_id in list(self.envs.envs):
            self.envs.destroy(env_id)
        shutil.rmtree(self.data / "environments", ignore_errors=True)
