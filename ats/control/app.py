"""HTTP surface: /v1 control API, the per-environment twin gateway, and the portal.

Gateway URL shape:  /env/{environment}/{application}/...   (API routes and /ui pages)
The environment id in the path is the routing context, so an unmodified
automation needs no custom headers. Seed and reset are not reachable here.

Local profile: binds to loopback and has no user authentication. OIDC and
workspace RBAC belong to the enterprise profile and are not implemented.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from ..runtime import soap
from ..runtime.environment import EnvironmentError_
from ..spec import SpecError
from .service import Conflict, NotFound, Studio

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _err(status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "trace_id": None, "retryable": retryable, "field_errors": []}, status)


def create_app(studio: Studio) -> FastAPI:
    app = FastAPI(title="Application Twin Studio", version="0.1.0", docs_url="/v1/docs", openapi_url="/v1/openapi.json")
    app.state.studio = studio

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound):
        return _err(404, "NOT_FOUND", str(exc))

    @app.exception_handler(Conflict)
    async def _cf(_: Request, exc: Conflict):
        return _err(409, "CONFLICT", str(exc))

    @app.exception_handler(EnvironmentError_)
    async def _ee(_: Request, exc: EnvironmentError_):
        return _err(409, "ENVIRONMENT_NOT_RUNNABLE", str(exc))

    @app.exception_handler(SpecError)
    async def _se(_: Request, exc: SpecError):
        return _err(422, "SPEC_INVALID", "; ".join(exc.errors))

    # ---- control API (sync handlers run in FastAPI's threadpool) ---------
    @app.get("/v1/health")
    def health():
        specs = studio.ws.specs()
        return {"status": "ok", "applications": len(specs), "spec_problems": studio.ws.spec_problems,
                "environments": len(studio.envs.envs), "recovered": studio.recovered}

    @app.get("/v1/applications")
    def applications():
        return {"items": studio.applications(), "spec_problems": studio.ws.spec_problems}

    @app.get("/v1/applications/{app_id}")
    def application(app_id: str):
        return studio.application(app_id)

    @app.get("/v1/automations")
    def automations():
        return {"items": [{k: v for k, v in a.items() if not k.startswith("_")} for a in studio.ws.automations().values()]}

    @app.post("/v1/automations/import", status_code=201)
    async def import_package(request: Request, id: str, tool: str = "uipath"):
        data = await request.body()
        return await run_in_threadpool(studio.import_package, id, data, tool)

    @app.put("/v1/automations/{automation_id}/arguments")
    def set_arguments(automation_id: str, body: dict):
        return studio.set_arguments(automation_id, body.get("arguments") or {})

    @app.post("/v1/onboardings", status_code=201)
    def onboard(body: dict):
        return studio.onboard(body["automation_id"])

    @app.get("/v1/onboardings")
    def onboardings():
        return {"items": [studio.onboard(a) for a in studio.ws.automations()]}

    @app.get("/v1/onboardings/{automation_id}/readiness")
    def readiness(automation_id: str):
        ob = studio.onboard(automation_id)
        return {"automation_id": automation_id, **ob["readiness"], "open_gaps": len(ob["gaps"]),
                "required_operations": ob["required_operations"], "package_hash": ob.get("package_hash")}

    @app.get("/v1/gaps")
    def gaps():
        return {"items": studio.gaps()}

    @app.get("/v1/scenarios")
    def scenarios():
        return {"items": list(studio.ws.scenarios().values()), "mutations": list(studio.ws.mutations().values())}

    @app.get("/v1/coverage")
    def coverage():
        return {"items": studio.coverage()}

    @app.get("/v1/environments")
    def environments():
        return {"items": studio.environments()}

    @app.post("/v1/environments", status_code=201)
    def launch(body: dict):
        return studio.launch(body["scenario_id"], body.get("mutation_id"))

    @app.get("/v1/environments/{env_id}")
    def environment(env_id: str):
        return studio.environment(env_id)

    @app.get("/v1/environments/{env_id}/snapshot")
    def snapshot(env_id: str):
        env = studio.envs.get(env_id)
        if env is None:
            raise NotFound(f"environment {env_id} not found")
        return env.snapshot()

    @app.delete("/v1/environments/{env_id}", status_code=204)
    def destroy(env_id: str):
        studio.destroy(env_id)
        return Response(status_code=204)

    @app.post("/v1/runs", status_code=202)
    def submit_run(body: dict):
        return studio.submit_run(scenario_id=body["scenario_id"], automation_id=body["automation_id"],
                                 mutation_id=body.get("mutation_id"), variant=body.get("variant"))

    @app.get("/v1/runs")
    def runs():
        keep = ("id", "status", "scenario_id", "automation_id", "verdict", "created_at", "mutation_id", "variant", "error")
        return {"items": [{k: r.get(k) for k in keep} for r in studio.docs.list("run", 60)]}

    @app.get("/v1/runs/{run_id}")
    def run(run_id: str):
        return studio.docs.get("run", run_id)

    @app.post("/v1/comparisons", status_code=202)
    def submit_comparison(body: dict):
        return studio.submit_comparison(scenario_id=body["scenario_id"], original_id=body["original_id"],
                                        replacement_id=body["replacement_id"], mutation_id=body.get("mutation_id"),
                                        replacement_variant=body.get("replacement_variant"))

    @app.get("/v1/comparisons")
    def comparisons():
        return {"items": studio.docs.list("comparison", 40)}

    @app.get("/v1/comparisons/{cmp_id}")
    def comparison(cmp_id: str):
        doc = studio.docs.get("comparison", cmp_id)
        for side in ("original_run", "replacement_run"):
            if doc.get(side):
                doc[side + "_detail"] = studio.docs.get("run", doc[side])
        return doc

    @app.post("/v1/suites", status_code=202)
    def submit_suite(body: dict):
        return studio.submit_suite(body["original_id"], body["replacement_id"], body.get("replacement_variant"),
                                   body.get("mutation_id"))

    @app.get("/v1/suites/{suite_id}")
    def suite(suite_id: str):
        return studio.docs.get("suite", suite_id)

    # ---- twin gateway -----------------------------------------------------
    def _gateway(env_id: str, app_id: str, method: str, path: str, query: dict, headers: dict, cookies: dict, raw: bytes):
        env = studio.envs.get(env_id)
        if env is None or env.status != "READY":
            return _err(404, "ENVIRONMENT_NOT_FOUND", f"no ready environment {env_id}")
        twin = env.apps.get(app_id)
        if twin is None:
            return _err(404, "APPLICATION_NOT_BOUND", f"{app_id} is not bound in environment {env_id}")
        ctype = headers.get("content-type", "")

        # terminal bridge for the portal's web terminal (drives the same screen state machine as TN3270)
        if path.startswith("/_term"):
            if not twin.spec.get("terminal"):
                return _err(404, "NO_TERMINAL", f"{app_id} has no terminal interface")
            parts = [p for p in path.split("/") if p][1:]
            if not parts:
                sid = env.open_terminal(app_id)
            else:
                sid = parts[0]
                session = env.term_sessions.get(sid)
                if session is None:
                    return _err(404, "NO_SESSION", "terminal session not found")
                if method == "POST":
                    body = json.loads(raw or b"{}")
                    session.send(body.get("aid", "ENTER"), body.get("fields") or {})
            session = env.term_sessions[sid]
            return JSONResponse({"session": sid, "closed": session.closed, **session.layout(), "text": session.text_rows()})

        if path == "/ui" or path.startswith("/ui/") or path in ("", "/"):
            ui = env.web.get(app_id)
            if ui is None:
                return _err(404, "NO_WEB_UI", f"{app_id} has no web interface")
            form = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()} if method == "POST" else {}
            out = ui.handle(method, path or "/", query, form, cookies)
            if out["status"] == 303:
                resp = RedirectResponse(out["location"], status_code=303)
                if out.get("cookie"):
                    resp.set_cookie(out["cookie"][0], out["cookie"][1], httponly=True, samesite="lax",
                                    path=f"/env/{env_id}/{app_id}")
                return resp
            return HTMLResponse(out["html"], status_code=out["status"])

        soap_itf = next((i for i in twin.spec["interfaces"] if i["protocol"] == "soap" and i.get("path", "/soap") == path), None)
        if soap_itf is not None:
            xml = lambda text, status=200: Response(text, status_code=status, media_type="text/xml; charset=utf-8")
            if method != "POST":
                env.record_call(app_id, f"(unmodeled) {method} {path} (WSDL is not generated)", "soap", None)
                return xml(soap.fault("UNMODELED_OPERATION", "this twin does not serve a WSDL", True), 501)
            try:
                element, params = soap.parse_envelope(raw)
            except soap.SoapError as exc:
                return xml(soap.fault("VALIDATION_ERROR", str(exc), True), 500)
            sop = next((o for o in twin.spec["operations"] if o.get("soap_operation") == element and o["interface"] == soap_itf["id"]), None)
            if sop is None:
                env.record_call(app_id, f"(unmodeled) SOAP {element}", "soap", None)
                return xml(soap.fault("UNMODELED_OPERATION", f"{app_id} twin does not model SOAP operation {element}", True), 500)
            res = twin.invoke(sop["id"], body=params, headers=headers, caller="api")
            if res.ok:
                return xml(soap.envelope(element + "Response", res.body, soap_itf.get("namespace", f"urn:ats:{app_id}")))
            return xml(soap.fault(res.body["code"], res.body["message"], res.status < 500, res.body.get("trace_id")), 500)

        hit = twin.match(method, path)
        if hit is None:
            # An unmatched request fails visibly and is recorded - never a fabricated success.
            env.record_call(app_id, f"(unmodeled) {method} {path}", "api", None)
            res = twin.error_result("UNMODELED_OPERATION", f"{app_id} twin does not model {method} {path}")
            return JSONResponse(res.body, res.status)
        op, params = hit
        auth = (twin.interfaces[op["interface"]].get("auth") or {})
        if auth.get("type") == "bearer" and headers.get("authorization") != f"Bearer {env.token}":
            res = twin.error_result("UNAUTHORIZED")
            return JSONResponse(res.body, res.status, headers={"WWW-Authenticate": "Bearer"})
        body = None
        if raw:
            if "json" in ctype or raw.lstrip()[:1] in (b"{", b"["):
                try:
                    body = json.loads(raw)
                except ValueError:
                    res = twin.error_result("VALIDATION_ERROR", "request body is not valid JSON")
                    return JSONResponse(res.body, res.status)
            else:
                body = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        res = twin.invoke(op["id"], path=params, query=query, body=body, headers=headers, caller="api")
        if res.body is None:
            return Response(status_code=res.status, headers=res.headers)
        return JSONResponse(res.body, res.status, headers=res.headers)

    @app.api_route("/env/{env_id}/{app_id}", methods=["GET", "POST"])
    @app.api_route("/env/{env_id}/{app_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def gateway(request: Request, env_id: str, app_id: str, path: str = ""):
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        return await run_in_threadpool(_gateway, env_id, app_id, request.method, "/" + path if path else "",
                                       dict(request.query_params), headers, dict(request.cookies), raw)

    # ---- portal -----------------------------------------------------------
    @app.get("/")
    def portal():
        return FileResponse(REPO_ROOT / "portal" / "index.html")

    @app.get("/demo")
    def demo():
        return FileResponse(REPO_ROOT / "demo" / "index.html")

    return app
