"""End to end through the real HTTP gateway and TN3270 ports: onboarding, runs, A/B comparison, negative tests."""
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ats.__main__ import Served

ORIG, REPL = "uipath-loan-qc-review", "agentic-loan-qc-review"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    with Served(tmp_path_factory.mktemp("data")) as s:
        yield s


def compare(srv, scenario, **kw):
    st = srv.studio
    return st.execute_comparison(st.new_comparison(scenario, ORIG, REPL, **kw)["id"])


def test_onboarding_maps_ui_api_and_terminal(srv):
    ob = srv.studio.onboard(ORIG)
    assert ob["readiness"]["state"] == "READY" and ob["readiness"]["confidence"] == "exploratory"
    assert ob["summary"]["opaque"] == 0
    ops = {(r["application_id"], r["operation"]) for r in ob["required_operations"]}
    assert {("qaqc", "login"), ("qaqc", "evaluate-case"), ("qaqc", "add-note"), ("corebank", "logon"), ("corebank", "post-memo")} <= ops
    assert all(g["severity"] == "evidence" for g in ob["gaps"])


def test_unsupported_automation_is_blocked_and_cannot_run(srv):
    ob = srv.studio.onboard("uipath-legacy-branch-report")
    assert ob["readiness"]["state"] == "BLOCKED"
    kinds = {g["kind"] for g in ob["gaps"]}
    assert {"unsupported_activity", "missing_application"} <= kinds
    run = srv.studio.execute_run(srv.studio.new_run("matched-case", "uipath-legacy-branch-report")["id"])
    assert run["status"] == "BLOCKED" and run["env_id"] is None  # gate stops it before any environment exists


@pytest.mark.parametrize("scenario", ["matched-case", "kyc-mismatch", "missing-document", "kyc-outage",
                                      "note-timeout-after-commit", "host-account-closed", "case-not-found"])
def test_original_and_replacement_are_equivalent(srv, scenario):
    c = compare(srv, scenario)
    assert c["sides"]["original"]["status"] == "PASSED", c["sides"]
    assert c["sides"]["replacement"]["status"] == "PASSED", c["sides"]
    assert c["differences"] == []
    cert = c["certificate"]
    assert cert["result"] == "EQUIVALENT_IN_SCOPE"
    # demonstration rules + simulated replay can never qualify a migration
    assert cert["confidence"] == "exploratory" and cert["may_qualify_migration"] is False
    assert c["sides"]["original"]["env_id"] != c["sides"]["replacement"]["env_id"]


def test_wrong_twin_rule_is_detected_even_though_both_runners_agree(srv):
    c = compare(srv, "kyc-mismatch", mutation_id="qaqc-kyc-rule-wrong")
    assert c["differences"] == []  # the two runners agree with each other...
    assert c["certificate"]["result"] == "FAILED"  # ...and the independent oracle still fails them
    assert "case-state" in c["sides"]["original"]["failed"] and "case-state" in c["sides"]["replacement"]["failed"]


def test_defective_replacement_is_detected(srv):
    c = compare(srv, "matched-case", replacement_variant="defective-skip-memo")
    assert c["sides"]["original"]["status"] == "PASSED"
    assert c["sides"]["replacement"]["status"] == "FAILED"
    assert c["certificate"]["result"] == "FAILED"
    assert any(d["path"].startswith("state.corebank.memo") for d in c["differences"])


def test_backend_only_automation(srv):
    run = srv.studio.execute_run(srv.studio.new_run("batch-evaluate-new", "backend-batch-evaluate")["id"])
    assert run["status"] == "PASSED", [a for a in run["assertions"] if not a["passed"]]
    assert run["cleanup"]["status"] == "OK"


def test_repeated_runs_are_deterministic(srv):
    views = []
    for _ in range(10):
        run = srv.studio.execute_run(srv.studio.new_run("kyc-timeout-recovers", REPL)["id"])
        assert run["status"] == "PASSED"
        views.append(json.dumps(run["business_view"], sort_keys=True))
    assert len(set(views)) == 1


def test_concurrent_runs_do_not_interfere(srv):
    scenarios = ["matched-case", "kyc-mismatch", "missing-document", "customer-mismatch"] * 5
    with ThreadPoolExecutor(20) as pool:
        runs = list(pool.map(lambda s: srv.studio.execute_run(srv.studio.new_run(s, REPL)["id"]), scenarios))
    assert [r["status"] for r in runs] == ["PASSED"] * 20
    assert len({r["env_id"] for r in runs}) == 20
    assert srv.studio.envs.envs == {}  # every environment was destroyed


def test_gateway_auth_isolation_and_unmodeled_requests(srv):
    a = srv.studio.launch("matched-case")
    b = srv.studio.launch("matched-case")
    try:
        qa, tok_a = a["bindings"]["qaqc"]["http"], a["bindings"]["qaqc"]["auth"]["bearer"]
        qb, tok_b = b["bindings"]["qaqc"]["http"], b["bindings"]["qaqc"]["auth"]["bearer"]
        assert httpx.get(f"{qa}/cases/QC-1001").status_code == 401
        # a credential from environment B is useless in environment A
        assert httpx.get(f"{qa}/cases/QC-1001", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 401
        ha = {"Authorization": f"Bearer {tok_a}"}
        assert httpx.post(f"{qa}/cases/QC-1001/evaluate", headers=ha).json()["state"] == "COMPLETED"
        assert httpx.get(f"{qb}/cases/QC-1001", headers={"Authorization": f"Bearer {tok_b}"}).json()["state"] == "NEW"
        r = httpx.delete(f"{qa}/cases/QC-1001", headers=ha)
        assert r.status_code == 501 and r.json()["code"] == "UNMODELED_OPERATION"
        # management actions are not reachable through the business gateway
        assert httpx.post(f"{qa}/_seed", headers=ha).status_code == 501
        # web UI and API agree, and the UI is reachable by a plain browser
        ui = a["bindings"]["qaqc"]["web"]
        with httpx.Client(follow_redirects=True) as browser:
            browser.post(f"{ui}/login", data={"username": "qa.bot", "password": "Twin#2026"})
            assert "id='field-state' data-field='state'>COMPLETED<" in browser.get(f"{ui}/cases/QC-1001").text
        # portal web terminal drives the same host state machine
        host = a["bindings"]["corebank"]["http"]
        t = httpx.post(f"{host}/_term").json()
        assert "SIGN ON" in "\n".join(t["text"])
        t = httpx.post(f"{host}/_term/{t['session']}", json={"aid": "ENTER", "fields": {"userid": "QCBOT01", "password": "TWIN2026"}}).json()
        assert t["screen"] == "MENU"
    finally:
        srv.studio.destroy(a["env_id"])
        srv.studio.destroy(b["env_id"])
    assert httpx.get(f"{qa}/cases/QC-1001", headers=ha).status_code == 404  # destroyed means gone


def test_malicious_archives_are_rejected(srv):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.xaml", "<x/>")
    r = httpx.post(f"{srv.base}/v1/automations/import", params={"id": "evil-pkg"}, content=buf.getvalue())
    assert r.status_code == 409 and "escapes" in r.json()["message"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Main.xaml", '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Activity/>')
    r = httpx.post(f"{srv.base}/v1/automations/import", params={"id": "entity-pkg"}, content=buf.getvalue())
    try:
        assert r.status_code == 201
        assert r.json()["readiness"]["state"] == "BLOCKED" and "entity declarations are rejected" in r.json()["error"]
    finally:
        import shutil
        shutil.rmtree(srv.studio.upload_root / "automations" / "entity-pkg", ignore_errors=True)


def test_api_run_lifecycle(srv):
    r = httpx.post(f"{srv.base}/v1/comparisons", json={"scenario_id": "resume-pending-case", "original_id": ORIG, "replacement_id": REPL})
    assert r.status_code == 202
    cmp_id = r.json()["id"]
    import time
    for _ in range(200):
        doc = httpx.get(f"{srv.base}/v1/comparisons/{cmp_id}").json()
        if doc["status"] != "RUNNING":
            break
        time.sleep(0.1)
    assert doc["status"] == "DONE" and doc["certificate"]["result"] == "EQUIVALENT_IN_SCOPE"
    stages = [s["name"] for s in doc["original_run_detail"]["stages"]]
    assert stages == ["GATE", "PROVISION+SEED", "PREFLIGHT", "EXECUTE", "COLLECT", "ASSERT", "CLEANUP"]
    assert httpx.get(f"{srv.base}/v1/runs/nope").status_code == 404
