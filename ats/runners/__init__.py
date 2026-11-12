"""Runner adapters: preflight -> execute -> collect.

  xaml-replay   simulated replay of the supported UiPath XAML subset
  command       any executable (agent framework, Playwright/Selenium script,
                Python/Node/Java batch job) that honours the bindings contract
  uipath-robot  hands the package to a licensed UiRobot.exe if one is configured

The bindings contract for ``command`` runners:
  env ATS_BINDINGS = path to a JSON file {"apps": {...}, "inputs": {...}, "output_path": "..."}
  the process writes its result object as JSON to output_path and exits 0.
A zero exit code is never treated as success on its own: assertions decide.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..expr import lookup
from .htmldriver import Blocked
from .xaml_replay import UnsupportedActivity, WorkflowFault, XamlReplay

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TPL = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
RUNNERS = {"xaml-replay", "command", "uipath-robot"}


def resolve_arguments(arguments: dict, bindings: dict, inputs: dict) -> tuple[dict, list[str]]:
    """Fill ``{{app.surface}}`` templates. Unresolved templates are returned as problems, never guessed."""
    ctx = {**bindings, "inputs": inputs}
    problems: list[str] = []

    def sub(name: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        def rep(m: re.Match) -> str:
            found = lookup(ctx, m.group(1))
            if found is None:
                problems.append(f"argument {name}: binding '{m.group(1)}' is not available in this environment")
                return ""
            return str(found)

        return _TPL.sub(rep, value)

    return {k: sub(k, v) for k, v in (arguments or {}).items()}, problems


def preflight(automation: dict, bindings: dict, inputs: dict) -> list[str]:
    problems = []
    runner = automation.get("runner")
    if runner not in RUNNERS:
        problems.append(f"unknown runner '{runner}'")
    _, unresolved = resolve_arguments(automation.get("arguments") or {}, bindings, inputs)
    problems += unresolved
    if runner == "uipath-robot" and not _uirobot():
        problems.append("no licensed UiPath robot is configured on this worker (set ATS_UIROBOT_PATH)")
    if runner == "command" and not automation.get("command"):
        problems.append("command runner needs a 'command' list")
    return problems


def _uirobot() -> str | None:
    path = os.environ.get("ATS_UIROBOT_PATH")
    return path if path and Path(path).exists() else None


def execute(automation: dict, bindings: dict, inputs: dict, workdir: Path, timeout: float = 120.0) -> dict:
    """Returns {status, output, log, error, runner}. status: COMPLETED | FAULTED | BLOCKED | ERROR."""
    runner = automation["runner"]
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    result = {"runner": runner, "status": "ERROR", "output": {}, "log": [], "error": None,
              "evidence_class": {"xaml-replay": "simulated-replay", "command": "native-runner",
                                 "uipath-robot": "licensed-original-runner"}.get(runner, "unknown")}
    problems = preflight(automation, bindings, inputs)
    if problems:
        result.update(status="BLOCKED", error="; ".join(problems))
        return result
    args, _ = resolve_arguments(automation.get("arguments") or {}, bindings, inputs)
    try:
        if runner == "xaml-replay":
            prefixes = [b[k] for b in bindings.values() for k in ("http", "web") if k in b]
            hosts = [(b["tn3270"]["host"], b["tn3270"]["port"]) for b in bindings.values() if "tn3270" in b]
            replay = XamlReplay(Path(automation["_dir"]), prefixes, hosts)
            try:
                scope = replay.run(automation.get("entry", "Main.xaml"), args)
                result.update(status="COMPLETED",
                              output={k: scope.get(v) for k, v in (automation.get("outputs") or {}).items()})
            except WorkflowFault as exc:
                result.update(status="FAULTED", error=str(exc))
            except (UnsupportedActivity, Blocked) as exc:
                result.update(status="BLOCKED", error=str(exc))
            result["log"] = replay.log
        else:
            out_path = workdir / "result.json"
            bfile = workdir / "bindings.json"
            bfile.write_text(json.dumps({"apps": bindings, "inputs": inputs, "arguments": args,
                                         "output_path": str(out_path)}, indent=2), encoding="utf-8")
            if runner == "command":
                cmd = [sys.executable if c == "python" else c for c in automation["command"]]
            else:
                # Unverified hand-off: this code path has never been exercised against a real robot.
                cmd = [_uirobot(), "execute", "--file", str(Path(automation["_dir"]) / "project.json"),
                       "--input", json.dumps(args)]
            env = {**os.environ, "ATS_BINDINGS": str(bfile), **(automation.get("env") or {}),
                   "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
            proc = subprocess.run(cmd, cwd=automation["_dir"], env=env, capture_output=True, text=True, timeout=timeout)
            tail = (proc.stdout or "").strip().splitlines()[-40:]
            result["log"] = [{"n": i + 1, "activity": "stdout", "info": line} for i, line in enumerate(tail)]
            if proc.returncode != 0:
                result.update(status="FAULTED", error=f"exit code {proc.returncode}: {(proc.stderr or '').strip()[-600:]}")
            elif not out_path.exists():
                result.update(status="FAULTED", error="process exited 0 but wrote no result file")
            else:
                result.update(status="COMPLETED", output=json.loads(out_path.read_text(encoding="utf-8")))
    except subprocess.TimeoutExpired:
        result.update(status="ERROR", error=f"runner exceeded {timeout}s deadline")
    except Exception as exc:  # infrastructure failure, reported separately from assertion failures
        result.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
    return result
