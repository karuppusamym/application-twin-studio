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


def test_host_web_front_end_replaces_the_terminal(srv):
    """3270 bot vs a bot on a HATS-style web front end over the same host transactions."""
    st = srv.studio
    for sid in ("closure-zero-balance", "closure-on-hold", "closure-not-found"):
        c = st.execute_comparison(st.new_comparison(sid, "uipath-account-closure", "uipath-web-account-closure")["id"])
        assert c["differences"] == [] and c["certificate"]["result"] == "EQUIVALENT_IN_SCOPE", sid
        b = st.docs.get("run", c["replacement_run"])
        callers = {call["caller"] for call in b["calls"]}
        assert "ui:QCBOT01" in callers and "terminal" not in callers, (sid, callers)


def test_host_web_pages_show_the_host_message_line(srv):
    env = srv.studio.launch("closure-on-hold")
    try:
        web = env["bindings"]["corebank"]["web"]
        with httpx.Client(follow_redirects=True) as c:
            assert "Please sign in" in c.get(f"{web}/inquiry").text           # pages need a sign-on
            bad = c.post(f"{web}/signon", data={"userid": "QCBOT01", "password": "wrong"})
            assert "INVALID USERID OR PASSWORD" in bad.text
            c.post(f"{web}/signon", data={"userid": "QCBOT01", "password": "TWIN2026"})
            missing = c.post(f"{web}/inquiry", data={"account_id": "4999999999"})
            assert "id='error-message'" in missing.text and "ACCOUNT NOT FOUND" in missing.text
            page = c.post(f"{web}/inquiry", data={"account_id": "4000002003"})
            assert "<dd id='field-status' data-field='status'>HOLD</dd>" in page.text
            rejected = c.post(f"{web}/accounts/4000002003/_action/btn-close")
            assert "ACCOUNT ON HOLD - CLOSE REJECTED</div>" in rejected.text and "ACCOUNT_ON_HOLD:" not in rejected.text
    finally:
        srv.studio.destroy(env["env_id"])


def test_collateral_change_is_caught_even_when_every_business_check_passes(srv):
    st = srv.studio
    run = st.execute_run(st.new_run("closure-zero-balance", "agentic-account-closure", variant="defective-collateral-change")["id"])
    failed = {a["id"]: a for a in run["assertions"] if not a["passed"]}
    assert run["status"] == "FAILED" and set(failed) == {"no-unexpected-changes"}
    assert "corebank.account 4000002002" in failed["no-unexpected-changes"]["detail"]
    good = st.execute_run(st.new_run("closure-zero-balance", "agentic-account-closure")["id"])
    assert good["status"] == "PASSED"


def test_repeated_runs_catch_an_inconsistent_agent_that_passes_once(srv):
    st = srv.studio
    once = st.execute_comparison(st.new_comparison("wire-watchlist-block", "uipath-wire-release", "agentic-wire-release",
                                                   replacement_variant="flaky-screening")["id"])
    assert once["certificate"]["result"] == "EQUIVALENT_IN_SCOPE"          # a single run is fooled
    assert any("ran once" in x for x in once["certificate"]["limits"])     # ...and says so
    five = st.execute_comparison(st.new_comparison("wire-watchlist-block", "uipath-wire-release", "agentic-wire-release",
                                                   replacement_variant="flaky-screening", repeat=5)["id"])
    assert five["consistency"] == {"passed": 4, "attempts": 5, "all_passed": False}
    assert five["certificate"]["result"] == "FAILED"
    assert [x["attempt"] for x in five["attempts"] if x["status"] != "PASSED"] == [3]
    steady = st.execute_comparison(st.new_comparison("wire-watchlist-block", "uipath-wire-release", "agentic-wire-release",
                                                     repeat=3)["id"])
    assert steady["consistency"]["all_passed"] and steady["certificate"]["result"] == "EQUIVALENT_IN_SCOPE"


def test_unexpected_change_detection_unit():
    from ats import validation
    keys = {"bank": {"account": "id"}}
    before = {"state": {"bank": {"account": [{"id": "A", "status": "OPEN", "_rev": 1}, {"id": "B", "status": "OPEN", "_rev": 1}]}}}
    after = {"state": {"bank": {"account": [{"id": "A", "status": "CLOSED", "_rev": 2}, {"id": "B", "status": "OPEN", "_rev": 2},
                                            {"id": "C", "status": "OPEN"}]}}}
    scenario = {"assertions": [{"type": "state", "app": "bank", "entity": "account", "key": "A", "expect": {"status": "CLOSED"}}]}
    res = validation.unexpected_changes_check(before, after, keys, scenario)
    assert not res["passed"] and res["detail"] == "inserted bank.account C"      # _rev-only change on B is bookkeeping
    assert validation.unexpected_changes_check(before, after, keys, {**scenario, "allowed_changes": [
        {"app": "bank", "entity": "account"}]})["passed"]
