"""Twin engine: one state model behind API, web UI, terminal and SQL."""
import sqlite3
from pathlib import Path

import pytest
import yaml

from ats import spec as specmod
from ats.runtime.environment import EnvironmentError_, EnvironmentManager
from ats.runtime.tn3270 import Tn3270Client

ROOT = Path(__file__).resolve().parent.parent
APPS = ["qaqc", "mortgage", "kyc", "documents", "corebank"]


def load_specs(apps=APPS):
    return {a: specmod.load_file(ROOT / "samples" / "apps" / f"{a}.yaml") for a in apps}


def fixture():
    return yaml.safe_load((ROOT / "samples" / "fixtures" / "loan-review-base.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def mgr(tmp_path):
    m = EnvironmentManager(tmp_path / "envs", "http://test")
    yield m
    for env_id in list(m.envs):
        m.destroy(env_id)


def test_specs_validate():
    for spec in load_specs().values():
        assert spec["_digest"].startswith("sha256:")


def test_invalid_spec_rejected():
    bad = yaml.safe_load((ROOT / "samples" / "apps" / "kyc.yaml").read_text(encoding="utf-8"))
    bad["operations"][0]["steps"].insert(0, {"exec": {"code": "import os"}})
    bad["operations"][0]["steps"].append({"load": {"entity": "nope", "key": "x"}})
    errors = specmod.validate(bad)
    assert any("not on the allowlist" in e for e in errors)
    assert any("unknown entity nope" in e for e in errors)


def test_evaluate_rules(mgr):
    env = mgr.provision(load_specs(), fixture())
    q = env.apps["qaqc"]
    expect = {"QC-1001": ("COMPLETED", "ALL_CHECKS_PASSED"), "QC-1002": ("PENDING_EVIDENCE", "DOCUMENT_MISSING"),
              "QC-1003": ("EXCEPTION", "KYC_NOT_VERIFIED"), "QC-1004": ("EXCEPTION", "CUSTOMER_MISMATCH")}
    for case_id, (state, reason) in expect.items():
        res = q.invoke("evaluate-case", path={"case_id": case_id})
        assert res.status == 200, res.body
        assert (res.body["state"], res.body["reason"]) == (state, reason)
    again = q.invoke("evaluate-case", path={"case_id": "QC-1001"})
    assert again.status == 409 and again.body["code"] == "STATE_CONFLICT"
    assert q.invoke("evaluate-case", path={"case_id": "nope"}).status == 404
    assert len([e for e in q.store.events() if e["event_type"] == "case.evaluated"]) == 4


def test_idempotency_and_timeout_after_commit(mgr):
    scenario = {"faults": [{"app": "qaqc", "operation": "add-note", "occurrence": 1, "type": "timeout_after_commit"}]}
    env = mgr.provision(load_specs(), fixture(), scenario)
    q = env.apps["qaqc"]
    hdr = {"Idempotency-Key": "k1"}
    first = q.invoke("add-note", path={"case_id": "QC-1001"}, body={"text": "hello"}, headers=hdr)
    assert first.status == 504  # committed, answer lost
    retry = q.invoke("add-note", path={"case_id": "QC-1001"}, body={"text": "hello"}, headers=hdr)
    assert retry.status == 201 and retry.headers.get("Idempotent-Replay") == "true"
    assert len(q.store.query("note")) == 1
    clash = q.invoke("add-note", path={"case_id": "QC-1001"}, body={"text": "different"}, headers=hdr)
    assert clash.status == 409 and clash.body["code"] == "IDEMPOTENCY_KEY_REUSED"
    bad = q.invoke("add-note", path={"case_id": "QC-1001"}, body={})
    assert bad.status == 422 and bad.body["field_errors"]


def test_dependency_fault_leaves_state_untouched(mgr):
    scenario = {"faults": [{"app": "kyc", "operation": "get-identity", "occurrence": "all", "type": "unavailable"}]}
    env = mgr.provision(load_specs(), fixture(), scenario)
    res = env.apps["qaqc"].invoke("evaluate-case", path={"case_id": "QC-1001"})
    assert res.status == 503 and res.body["retryable"] is True
    assert env.apps["qaqc"].store.get("case", "QC-1001")["state"] == "NEW"
    assert env.apps["qaqc"].store.events() == []
    assert sum(1 for c in env.calls if c["app"] == "kyc") == 2  # one call + one retry


def test_environments_are_isolated(mgr):
    a = mgr.provision(load_specs(), fixture())
    b = mgr.provision(load_specs(), fixture())
    a.apps["qaqc"].invoke("evaluate-case", path={"case_id": "QC-1001"})
    assert a.apps["qaqc"].store.get("case", "QC-1001")["state"] == "COMPLETED"
    assert b.apps["qaqc"].store.get("case", "QC-1001")["state"] == "NEW"
    assert a.token != b.token and a.dir != b.dir


def test_seed_reference_check_blocks_partial_environment(mgr):
    fx = fixture()
    fx["data"]["qaqc"]["case"][0]["loan_id"] = "L-MISSING"
    with pytest.raises(EnvironmentError_):
        mgr.provision(load_specs(), fx)
    assert mgr.envs == {}


def test_missing_required_dependency_blocks(mgr):
    with pytest.raises(EnvironmentError_):
        mgr.provision(load_specs(["qaqc", "mortgage"]), fixture())


def test_web_ui_uses_same_state(mgr):
    env = mgr.provision(load_specs(), fixture())
    ui = env.web["qaqc"]
    assert ui.handle("GET", "/ui/cases", {}, {}, {})["status"] == 303  # login required
    bad = ui.handle("POST", "/ui/login", {}, {"username": "qa.bot", "password": "wrong"}, {})
    assert "_err=" in bad["location"]
    ok = ui.handle("POST", "/ui/login", {}, {"username": "qa.bot", "password": "Twin#2026"}, {})
    cookies = dict([ok["cookie"]])
    page = ui.handle("GET", "/ui/cases/QC-1003", {}, {}, cookies)["html"]
    assert "id='field-state' data-field='state'>NEW<" in page and "id='btn-evaluate'" in page
    ui.handle("POST", "/ui/cases/QC-1003/_action/btn-evaluate", {}, {}, cookies)
    assert env.apps["qaqc"].invoke("get-case", path={"case_id": "QC-1003"}).body["state"] == "EXCEPTION"
    page = ui.handle("GET", "/ui/cases/QC-1003", {}, {}, cookies)["html"]
    assert ">EXCEPTION<" in page


def test_tn3270_terminal_and_sql_see_same_state(mgr):
    env = mgr.provision(load_specs(), fixture())
    b = env.bindings()["corebank"]
    t = Tn3270Client(b["tn3270"]["host"], b["tn3270"]["port"])
    try:
        assert "SIGN ON" in t.text()
        t.set_field(10, 35, "QCBOT01")
        t.set_field(12, 35, "WRONG")
        t.send("ENTER")
        assert "INVALID USERID OR PASSWORD" in t.get(23, 2, 78)
        t.set_field(10, 35, "qcbot01")
        t.set_field(12, 35, "TWIN2026")
        t.send("ENTER")
        assert "MAIN MENU" in t.text() and "OPER: QCBOT01" in t.text()
        with pytest.raises(ValueError):
            t.set_field(8, 20, "X")  # protected text
        t.set_field(20, 15, "1")
        t.send("ENTER")
        t.set_field(5, 25, "4000001003")
        t.send("ENTER")
        assert t.get(10, 16, 8) == "ACTIVE" and t.get(11, 16, 15) == "98100.00"
        t.send("PF3")
        t.set_field(20, 15, "2")
        t.send("ENTER")
        t.set_field(5, 25, "4000001003")
        t.set_field(7, 25, "QCHOLD")
        t.set_field(9, 25, "KYC MISMATCH")
        t.send("ENTER")
        assert "MEMO M000001 POSTED" in t.get(23, 2, 78)
        t.send("PF9")
        assert "INVALID KEY PF9" in t.get(23, 2, 78)
    finally:
        t.close()
    # the mainframe API and a direct SQL read observe the terminal's write
    assert env.apps["corebank"].invoke("get-account", path={"account_id": "4000001003"}).body["status"] == "HOLD"
    db = sqlite3.connect(b["sql"]["path"])
    assert db.execute("SELECT code, operator FROM memo").fetchall() == [("QCHOLD", "QCBOT01")]
    db.close()
