"""Every process and interface combination: SOAP, REST, web UI (real browser), 3270, mainframe API, SQL."""
import httpx
import pytest

from ats import vbexpr
from ats.__main__ import Served, run_everything
from ats.runtime import soap


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    with Served(tmp_path_factory.mktemp("data")) as s:
        yield s


def test_every_process_passes_every_scenario(srv):
    rows, bad = run_everything(srv.studio)
    summary = {r["process"]: (r["mode"], r["passed"], r["total"]) for r in rows}
    assert bad == 0, summary
    assert set(summary) == {"account-closure", "address-change", "batch-evaluate", "case-triage", "loan-account-recon",
                            "loan-qc-review", "wire-release"}
    assert sum(r["total"] for r in rows) >= 40
    assert {r["mode"] for r in rows} == {"A/B", "single"}


def test_every_ready_automation_onboards_without_critical_gaps(srv):
    for automation_id in srv.studio.ws.automations():
        ob = srv.studio.onboard(automation_id)
        if automation_id == "uipath-legacy-branch-report":
            assert ob["readiness"]["state"] == "BLOCKED"
        else:
            assert ob["readiness"]["state"] == "READY", (automation_id, ob["readiness"]["blockers"])
            assert ob["summary"]["opaque"] == 0


def test_onboarding_maps_soap_and_new_terminal_screens(srv):
    wire = {(r["application_id"], r["operation"]) for r in srv.studio.onboard("uipath-wire-release")["required_operations"]}
    assert {("payments", "get-wire"), ("payments", "release-wire"), ("payments", "hold-wire"), ("payments", "reject-wire"),
            ("risk", "screen-wire"), ("corebank", "post-memo")} <= wire
    addr = {(r["application_id"], r["operation"]) for r in srv.studio.onboard("uipath-address-change")["required_operations"]}
    assert {("crm", "login"), ("crm", "complete-request"), ("crm", "reject-request"), ("kyc", "get-identity"),
            ("corebank", "update-address")} <= addr
    close = {(r["application_id"], r["operation"]) for r in srv.studio.onboard("uipath-account-closure")["required_operations"]}
    assert ("corebank", "close-account") in close


def test_defective_wire_agent_that_skips_screening_is_caught(srv):
    st = srv.studio
    c = st.execute_comparison(st.new_comparison("wire-watchlist-block", "uipath-wire-release", "agentic-wire-release",
                                                replacement_variant="defective-no-screening")["id"])
    assert c["sides"]["original"]["status"] == "PASSED"
    assert c["sides"]["replacement"]["status"] == "FAILED"
    assert "screening-count" in c["sides"]["replacement"]["failed"] and "wire-state" in c["sides"]["replacement"]["failed"]
    assert c["certificate"]["result"] == "FAILED"


def test_interface_substitution_terminal_vs_api_leaves_same_state(srv):
    st = srv.studio
    c = st.execute_comparison(st.new_comparison("closure-zero-balance", "uipath-account-closure", "agentic-account-closure")["id"])
    assert c["differences"] == [] and c["certificate"]["result"] == "EQUIVALENT_IN_SCOPE"
    a, b = st.docs.get("run", c["original_run"]), st.docs.get("run", c["replacement_run"])
    assert any(call["caller"] == "terminal" for call in a["calls"])      # original really used the 3270 screens
    assert all(call["caller"] != "terminal" for call in b["calls"])      # replacement really used the API


def test_ui_only_automation_runs_in_a_real_browser(srv):
    pytest.importorskip("playwright")
    run = srv.studio.execute_run(srv.studio.new_run("triage-all-new", "playwright-case-triage")["id"])
    assert run["status"] == "PASSED", run.get("error")
    driver = run["runner"]["output"]["trace"]["driver"]
    if driver != "playwright-chromium":
        pytest.skip(f"Chromium not installed here; the bot fell back to {driver}")
    assert any(c["caller"] == "ui:qa.bot" and c["operation"] == "evaluate-case" for c in run["calls"])


def test_soap_gateway_contract(srv):
    env = srv.studio.launch("wire-clear")
    try:
        url = env["bindings"]["payments"]["soap"]
        post = lambda xml: httpx.post(url, content=xml.encode(), headers={"Content-Type": "text/xml"})
        ok = post(soap.envelope("GetWire", {"wireId": "W-5001"}, "urn:demo:payments"))
        assert ok.status_code == 200 and "<status>PENDING</status>" in ok.text and "GetWireResponse" in ok.text
        assert "_rev" not in ok.text  # internal bookkeeping never leaks into the contract
        missing = post(soap.envelope("GetWire", {"wireId": "nope"}, "urn:demo:payments"))
        parsed = soap.parse_response(missing.text)
        assert missing.status_code == 500 and parsed["fault_code"] == "WIRE_NOT_FOUND" and "soap:Client" in missing.text
        unknown = soap.parse_response(post(soap.envelope("CancelWire", {"wireId": "W-5001"}, "urn:x")).text)
        assert unknown["fault_code"] == "UNMODELED_OPERATION"
        dtd = post('<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Envelope><Body><GetWire/></Body></Envelope>')
        assert soap.parse_response(dtd.text)["fault_code"] == "VALIDATION_ERROR"
        assert httpx.get(url + "?wsdl").status_code == 501  # no WSDL: stated, not faked
        # the risk twin reads the wire through its dependency and applies decimal thresholds
        risk, token = env["bindings"]["risk"]["http"], env["bindings"]["risk"]["auth"]["bearer"]
        hdr = {"Authorization": f"Bearer {token}"}
        assert httpx.post(f"{risk}/screenings", json={"wire_id": "W-5002"}, headers=hdr).json()["decision"] == "REVIEW"
        assert httpx.post(f"{risk}/screenings", json={"wire_id": "W-5003"}, headers=hdr).json()["decision"] == "BLOCK"
        assert httpx.post(f"{risk}/screenings", json={"wire_id": "nope"}, headers=hdr).status_code == 404
    finally:
        srv.studio.destroy(env["env_id"])


def test_vb_regex_extraction_is_supported_and_bounded():
    xml = "<wire><status>PENDING</status><account_id>4000001001</account_id></wire>"
    scope = {"x": xml, "j": '{"decision":"BLOCK","reason":"WATCHLIST"}'}
    assert vbexpr.evaluate('Regex.Match(x, "<status>([A-Z]+)</status>").Groups(1).Value', scope) == "PENDING"
    assert vbexpr.evaluate('System.Text.RegularExpressions.Regex.Match(x, "<account_id>([0-9]+)<").Groups(1).Value', scope) == "4000001001"
    assert vbexpr.evaluate('Regex.Match(j, """decision"":""([A-Z]+)""").Groups(1).Value', scope) == "BLOCK"
    assert vbexpr.evaluate('Regex.Match(x, "<nope>").Success', scope) is False
    assert vbexpr.evaluate('Regex.Match(x, "<missing>(.*)<").Groups(1).Value', scope) == ""
    assert vbexpr.analyse('Regex.Match(x, "a(b)").Groups(1).Value') == {"x"}
    with pytest.raises(vbexpr.Unsupported):
        vbexpr.evaluate('Regex.Replace(x, "a", "b")', scope)
    with pytest.raises(vbexpr.Unsupported):
        vbexpr.evaluate('Regex.Match(x, "' + "a" * 300 + '").Value', scope)
    with pytest.raises(vbexpr.Unsupported):
        vbexpr.evaluate("System.IO.File.ReadAllText(x)", scope)
