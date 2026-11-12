"""Command line entry point.

  python -m ats serve [--port 8700]         start the studio (portal + API + twin gateway)
  python -m ats validate                    validate every ATS-Spec in the workspace
  python -m ats onboard <automation-id>     print the onboarding / readiness report
  python -m ats compare <scenario> <original> <replacement> [--mutation ID] [--variant NAME]
  python -m ats suite <original> <replacement>   run every scenario of the process, A vs B
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
    c.add_argument("--mutation"), c.add_argument("--variant")
    u = sub.add_parser("suite")
    u.add_argument("original"), u.add_argument("replacement")
    u.add_argument("--mutation"), u.add_argument("--variant")
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
                                                              args.mutation, args.variant)["id"])
            _print_comparison(cmp_doc)
            return 0 if (cmp_doc.get("certificate") or {}).get("result") == "EQUIVALENT_IN_SCOPE" else 1
        if args.cmd == "suite":
            process = st.automation(args.original).get("process")
            ids = sorted(x["id"] for x in st.ws.scenarios().values() if x.get("process") == process)
            bad = 0
            for sid in ids:
                cmp_doc = st.execute_comparison(st.new_comparison(sid, args.original, args.replacement, args.mutation, args.variant)["id"])
                _print_comparison(cmp_doc)
                bad += (cmp_doc.get("certificate") or {}).get("result") != "EQUIVALENT_IN_SCOPE"
            print(f"\n{len(ids) - bad}/{len(ids)} scenarios equivalent in scope")
            return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
