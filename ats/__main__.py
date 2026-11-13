"""Command line entry point.

  python -m ats serve [--port 8700]         start the studio (portal + API + twin gateway)
  python -m ats validate                    validate every ATS-Spec in the workspace
  python -m ats onboard <automation-id>     print the onboarding / readiness report
  python -m ats compare <scenario> <original> <replacement> [--mutation ID] [--variant NAME] [--repeat K]
  python -m ats suite <original> <replacement> [--repeat K]   run every scenario of the process, A vs B
  python -m ats all [--repeat K]            run every process and print the coverage matrix

  --repeat K runs the replacement (or a single-run automation) K times; it only passes if all K pass.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from .control.app import create_app
from .control.service import Studio

REPO_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    """Runs the studio on a background thread; used by the CLI one-shot commands and the tests."""

    def __init__(self, data_dir: Path, port: int | None = None):
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.studio = Studio(REPO_ROOT, data_dir, self.base)
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.studio), host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Served":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("studio did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.studio.shutdown()


def _print_comparison(c: dict) -> None:
    cert = c.get("certificate") or {}
    print(f"\n{c['scenario_id']}: {cert.get('result')}  (confidence: {cert.get('confidence')})")
    for side, s in (c.get("sides") or {}).items():
        print(f"  {side:<12} {s['status']:<8} runner={s['runner']} failed={s['failed'] or '-'}" + (f" error={s['error']}" if s.get("error") else ""))
    for d in (c.get("differences") or [])[:12]:
        print(f"  DIFF {d['path']}: original={d['original']!r} replacement={d['replacement']!r}")
    if c.get("consistency"):
        print(f"  attempts     {c['consistency']['passed']}/{c['consistency']['attempts']} passed and matched the original")
    for limit in cert.get("limits") or []:
        print(f"  limit: {limit}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ats", description="Application Twin Studio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--data", default=str(REPO_ROOT / "data"))
    sub.add_parser("validate")
    o = sub.add_parser("onboard")
    o.add_argument("automation")
    c = sub.add_parser("compare")
    c.add_argument("scenario"), c.add_argument("original"), c.add_argument("replacement")
    c.add_argument("--mutation"), c.add_argument("--variant"), c.add_argument("--repeat", type=int, default=1)
    u = sub.add_parser("suite")
    u.add_argument("original"), u.add_argument("replacement")
    u.add_argument("--mutation"), u.add_argument("--variant"), u.add_argument("--repeat", type=int, default=1)
    al = sub.add_parser("all")
    al.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        studio = Studio(REPO_ROOT, Path(args.data), f"http://127.0.0.1:{args.port}")
        print(f"Application Twin Studio  ->  http://127.0.0.1:{args.port}   (playable demo: /demo, API docs: /v1/docs)")
        try:
            uvicorn.run(create_app(studio), host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            studio.shutdown()
        return 0

    data = REPO_ROOT / "data" / "cli"
    if args.cmd == "validate":
        from .workspace import Workspace
        ws = Workspace([REPO_ROOT / "samples", REPO_ROOT / "workspace"])
        specs = ws.specs()
        for app_id, sp in specs.items():
            print(f"OK   {app_id} {sp['spec_version']} {sp['_digest'][:19]}")
        for name, errors in ws.spec_problems.items():
            print(f"FAIL {name}")
            for e in errors:
                print(f"       {e}")
        return 1 if ws.spec_problems else 0

    with Served(data) as srv:
        st = srv.studio
        if args.cmd == "onboard":
            ob = st.onboard(args.automation)
            print(json.dumps({k: ob[k] for k in ("automation_id", "summary", "readiness", "required_operations")}, indent=2))
            for g in ob["gaps"]:
                print(f"{g['id']} [{g['severity']}] {g['summary']}")
            return 0 if ob["readiness"]["state"] == "READY" else 2
        if args.cmd == "compare":
            cmp_doc = st.execute_comparison(st.new_comparison(args.scenario, args.original, args.replacement,
                                                              args.mutation, args.variant, args.repeat)["id"])
            _print_comparison(cmp_doc)
            return 0 if (cmp_doc.get("certificate") or {}).get("result") == "EQUIVALENT_IN_SCOPE" else 1
        if args.cmd == "suite":
            process = st.automation(args.original).get("process")
            ids = sorted(x["id"] for x in st.ws.scenarios().values() if x.get("process") == process)
            bad = 0
            for sid in ids:
                cmp_doc = st.execute_comparison(st.new_comparison(sid, args.original, args.replacement, args.mutation, args.variant,
                                                                  args.repeat)["id"])
                _print_comparison(cmp_doc)
                bad += (cmp_doc.get("certificate") or {}).get("result") != "EQUIVALENT_IN_SCOPE"
            print(f"\n{len(ids) - bad}/{len(ids)} scenarios equivalent in scope")
            return 1 if bad else 0
        if args.cmd == "all":
            rows, bad = run_everything(st, verbose=True, repeat=args.repeat)
            print(f"\n{'PROCESS':<20} {'ORIGINAL -> REPLACEMENT':<58} {'MODE':<8} RESULT")
            for r in rows:
                print(f"{r['process']:<20} {r['pair']:<58} {r['mode']:<8} {r['passed']}/{r['total']}")
            print(f"\n{sum(r['passed'] for r in rows)}/{sum(r['total'] for r in rows)} scenario checks passed")
            return 1 if bad else 0
    return 0


def run_everything(st: Studio, verbose: bool = False, repeat: int = 1) -> tuple[list[dict], int]:
    """Every process: A/B where a replacement exists, single runs otherwise. Blocked automations are skipped."""
    autos, scenarios = st.ws.automations(), st.ws.scenarios()
    rows, bad = [], 0
    for process in sorted({s["process"] for s in scenarios.values()}):
        ids = sorted(s["id"] for s in scenarios.values() if s["process"] == process)
        originals = [a for a in autos.values() if a.get("process") == process and a.get("role") == "original"]
        replacements = [a for a in autos.values() if a.get("process") == process and a.get("role") == "replacement"]
        for orig in originals:
            if not replacements:
                passed = 0
                for sid in ids:
                    runs = [st.execute_run(st.new_run(sid, orig["id"], attempt=i if repeat > 1 else None)["id"])
                            for i in range(1, repeat + 1)]
                    ok = sum(r["status"] == "PASSED" for r in runs)
                    passed += ok == repeat
                    if verbose:
                        print(f"{sid}: {'PASSED' if ok == repeat else 'FAILED'}" + (f"  ({ok}/{repeat} attempts)" if repeat > 1 else ""))
                rows.append({"process": process, "pair": orig["id"], "mode": "single", "passed": passed, "total": len(ids),
                             "combination": orig.get("combination", "")})
                bad += len(ids) - passed
            for repl in replacements:
                passed = 0
                for sid in ids:
                    c = st.execute_comparison(st.new_comparison(sid, orig["id"], repl["id"], repeat=repeat)["id"])
                    ok = (c.get("certificate") or {}).get("result") == "EQUIVALENT_IN_SCOPE"
                    passed += ok
                    if verbose:
                        cons = c.get("consistency")
                        print(f"{sid}: {(c.get('certificate') or {}).get('result')}"
                              + (f"  ({cons['passed']}/{cons['attempts']} attempts)" if cons else ""))
                rows.append({"process": process, "pair": f"{orig['id']} -> {repl['id']}", "mode": "A/B", "passed": passed,
                             "total": len(ids), "combination": f"{orig.get('combination', '')}  vs  {repl.get('combination', '')}"})
                bad += len(ids) - passed
    return rows, bad


if __name__ == "__main__":
    sys.exit(main())
