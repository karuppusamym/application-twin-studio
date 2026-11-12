"""Generates the sample scenario files in samples/scenarios.

Expected outcomes are written here by hand from the process description, not
derived from the twin's handlers - that independence is what makes them an
oracle. Run: python tools/gen_scenarios.py
"""
import pathlib

import yaml

OUT = pathlib.Path(__file__).resolve().parent.parent / "samples" / "scenarios"
APPS = ["qaqc", "mortgage", "kyc", "documents", "corebank"]
ACCOUNTS = {f"QC-100{n}": f"400000100{n}" for n in range(1, 6)}


def scenario(sid, title, case, expect_state, memo, status, **extra):
    acct = ACCOUNTS.get(case)
    did_work = status in ("DONE", "HOST_REJECTED")
    a = []
    if expect_state:
        a.append({"id": "case-state", "type": "state", "app": "qaqc", "entity": "case", "key": case,
                  "expect": {"state": expect_state}, "critical": True})
    a.append({"id": "runner-status", "type": "output", "path": "status", "equals": status, "critical": True})
    if acct:
        a.append({"id": "host-memo-count", "type": "count", "app": "corebank", "entity": "memo",
                  "where": {"account_id": acct}, "equals": 1 if memo else 0, "critical": True})
        if memo:
            a.append({"id": "host-memo-code", "type": "rows", "app": "corebank", "entity": "memo",
                      "where": {"account_id": acct}, "each": {"code": memo}, "critical": True})
        a.append({"id": "account-status", "type": "state", "app": "corebank", "entity": "account", "key": acct,
                  "expect": {"status": extra.pop("account_status", "HOLD" if memo == "QCHOLD" else "ACTIVE")},
                  "critical": True})
    a.append({"id": "note-count", "type": "count", "app": "qaqc", "entity": "note", "where": {"case_id": case},
              "equals": 1 if did_work else 0, "critical": True})
    a.append({"id": "evaluated-events", "type": "event", "app": "qaqc", "event": "case.evaluated",
              "count": 1 if did_work else 0})
    doc = {"id": sid, "title": title, "description": extra.pop("description", ""), "fixture": "loan-review-base",
           "apps": APPS, "process": "loan-qc-review", "inputs": {"case_id": case}, **extra, "assertions": a}
    (OUT / f"{sid}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, width=120), encoding="utf-8")


scenario("matched-case", "All checks pass", "QC-1001", "COMPLETED", "QCPASS", "DONE",
         description="Loan, identity and document all agree. Case completes and a QCPASS memo is posted on the host.")
scenario("missing-document", "Document still requested", "QC-1002", "PENDING_EVIDENCE", None, "DONE",
         description="The income proof was requested but not received. Case waits for evidence; no host memo.")
scenario("kyc-mismatch", "KYC identity mismatch", "QC-1003", "EXCEPTION", "QCHOLD", "DONE",
         description="KYC reports a mismatch. Case becomes an exception and the servicing account is put on hold.")
scenario("customer-mismatch", "Loan belongs to another customer", "QC-1004", "EXCEPTION", "QCHOLD", "DONE",
         description="The loan record names a different customer than the case.")
scenario("kyc-timeout-recovers", "KYC times out once, then answers", "QC-1001", "COMPLETED", "QCPASS", "DONE",
         description="First KYC call times out; the twin retries once (demo gap G17) and the case still completes.",
         faults=[{"app": "kyc", "operation": "get-identity", "occurrence": 1, "type": "timeout"}])
scenario("kyc-outage", "KYC is down", "QC-1001", "NEW", None, "FAILED",
         description="KYC never answers. The case must stay NEW and the automation must not post a memo or note.",
         faults=[{"app": "kyc", "operation": "get-identity", "occurrence": "all", "type": "unavailable"}])
scenario("already-completed", "Case was already completed", "QC-1005", "COMPLETED", None, "SKIPPED",
         description="Re-processing a completed case must be a no-op.")
scenario("case-not-found", "Unknown case", "QC-9999", None, None, "NOT_FOUND",
         description="The requested case does not exist.")
scenario("note-timeout-after-commit", "Note call times out after it was saved", "QC-1001", "COMPLETED", "QCPASS", "DONE",
         description="The add-note call commits but the response is lost. A retry with the same idempotency key "
                     "must not create a second note.",
         faults=[{"app": "qaqc", "operation": "add-note", "occurrence": 1, "type": "timeout_after_commit"}])
scenario("document-record-missing", "Document record absent", "QC-1001", "PENDING_EVIDENCE", None, "DONE",
         description="Intentional anomaly: the document id on the case has no record in the repository.",
         remove=[{"app": "documents", "entity": "document", "key_field": "document_id", "key": "D300"}],
         intentional_anomalies=["qaqc.case.document_id"])
scenario("host-account-closed", "Host rejects memo on closed account", "QC-1001", "COMPLETED", None, "HOST_REJECTED",
         description="The servicing account is closed on the host, so the memo is rejected. The case result still stands.",
         patch=[{"app": "corebank", "entity": "account", "key_field": "account_id", "key": "4000001001",
                 "set": {"status": "CLOSED"}}],
         account_status="CLOSED")
scenario("resume-pending-case", "Pending case completes once the document arrives", "QC-1002", "COMPLETED", "QCPASS", "DONE",
         description="Case starts in PENDING_EVIDENCE and the document has now been received.",
         patch=[{"app": "qaqc", "entity": "case", "key_field": "case_id", "key": "QC-1002",
                 "set": {"state": "PENDING_EVIDENCE", "reason": "DOCUMENT_MISSING"}},
                {"app": "documents", "entity": "document", "key_field": "document_id", "key": "D301",
                 "set": {"status": "RECEIVED"}}])

EXPECTED = [("QC-1001", "COMPLETED"), ("QC-1002", "PENDING_EVIDENCE"), ("QC-1003", "EXCEPTION"),
            ("QC-1004", "EXCEPTION"), ("QC-1005", "COMPLETED")]
batch = {
    "id": "batch-evaluate-new", "title": "Backend batch evaluates every NEW case",
    "description": "Backend-only automation: reads the case table with SQL, evaluates each NEW case through the API.",
    "fixture": "loan-review-base", "apps": APPS, "process": "batch-evaluate", "inputs": {},
    "assertions": [{"id": f"state-{c}", "type": "state", "app": "qaqc", "entity": "case", "key": c,
                    "expect": {"state": s}, "critical": True} for c, s in EXPECTED] + [
        {"id": "no-new-left", "type": "count", "app": "qaqc", "entity": "case", "where": {"state": "NEW"},
         "equals": 0, "critical": True},
        {"id": "evaluated-events", "type": "event", "app": "qaqc", "event": "case.evaluated", "count": 4},
        {"id": "processed", "type": "output", "path": "processed", "equals": 4, "critical": True}],
}
(OUT / "batch-evaluate-new.yaml").write_text(yaml.safe_dump(batch, sort_keys=False, width=120), encoding="utf-8")


# =====================================================================================
# Further processes. Same rule: expected outcomes are written from the process
# description, never read back from the twin.
# =====================================================================================
def write(sid, title, process, apps, inputs, assertions, description="", **extra):
    doc = {"id": sid, "title": title, "description": description, "fixture": "loan-review-base", "apps": apps,
           "process": process, "inputs": inputs, **extra, "assertions": assertions}
    (OUT / f"{sid}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, width=120), encoding="utf-8")


def state(aid, app, entity, key, **expect):
    return {"id": aid, "type": "state", "app": app, "entity": entity, "key": key, "expect": expect, "critical": True}


def count(aid, app, entity, equals, **where):
    return {"id": aid, "type": "count", "app": app, "entity": entity, "where": where, "equals": equals, "critical": True}


def output(path, equals):
    return {"id": f"output-{path}", "type": "output", "path": path, "equals": equals, "critical": True}


def event(app, name, n):
    return {"id": f"event-{name}", "type": "event", "app": app, "event": name, "count": n}


def patch(app, entity, key_field, key, **values):
    return {"app": app, "entity": entity, "key_field": key_field, "key": key, "set": values}


# ---- wire release: SOAP + REST + mainframe API --------------------------------------
WIRE_APPS = ["payments", "risk", "corebank"]


def wire(sid, title, wire_id, status, action, decision, wire_status=None, reason=None, acct=None, acct_status="ACTIVE",
         memos=0, screenings=1, description="", **extra):
    a = [output("status", status), output("action", action), output("decision", decision),
         count("screening-count", "risk", "screening", screenings, wire_id=wire_id)]
    if wire_status:
        expect = {"status": wire_status, **({"reason": reason} if reason else {})}
        a.append(state("wire-state", "payments", "wire", wire_id, **expect))
    if acct:
        a += [state("account-status", "corebank", "account", acct, status=acct_status),
              count("host-memo-count", "corebank", "memo", memos, account_id=acct)]
    a.append(event("payments", "wire.released", 1 if action == "RELEASE" else 0))
    write(sid, title, "wire-release", WIRE_APPS, {"wire_id": wire_id}, a, description, **extra)


wire("wire-clear", "Wire passes screening and is released", "W-5001", "DONE", "RELEASE", "CLEAR", "RELEASED", "RISK_CLEAR",
     "4000001001", description="Small wire to an unlisted beneficiary from an active account.")
wire("wire-review-threshold", "Large wire is held for review", "W-5002", "DONE", "HOLD", "REVIEW", "HELD", "AMOUNT_THRESHOLD",
     "4000001002", description="Amount above 100,000 must be held, not released.")
wire("wire-watchlist-block", "Watch-listed beneficiary is rejected", "W-5003", "DONE", "REJECT", "BLOCK", "REJECTED", "WATCHLIST",
     "4000001003", "HOLD", memos=1,
     description="Beneficiary is on the watch list: reject the wire and put the debit account on hold via the mainframe API.")
wire("wire-already-released", "Wire was already released", "W-5004", "SKIPPED", "NONE", "NONE", "RELEASED", None,
     "4000001004", screenings=0, description="Re-processing must not screen or change anything.")
wire("wire-not-found", "Unknown wire", "W-9999", "NOT_FOUND", "NONE", "NONE", screenings=0,
     description="Payments answers with a SOAP fault.")
wire("wire-risk-outage", "Risk service is down", "W-5001", "FAILED", "NONE", "NONE", "PENDING", None, "4000001001", screenings=0,
     description="No screening, no release: the wire must stay PENDING.",
     faults=[{"app": "risk", "operation": "screen-wire", "occurrence": "all", "type": "unavailable"}])
wire("wire-account-on-hold", "Release refused because the account is on hold", "W-5001", "DONE", "HOLD", "CLEAR", "HELD",
     "ACCOUNT_NOT_ACTIVE", "4000001001", "HOLD",
     description="Screening is clear but Payments refuses the release; the automation must hold the wire.",
     patch=[patch("corebank", "account", "account_id", "4000001001", status="HOLD")])
wire("wire-amount-at-threshold", "Boundary: exactly 100,000.00 is released", "W-5001", "DONE", "RELEASE", "CLEAR", "RELEASED",
     None, "4000001001", description="The rule is strictly greater than 100,000.",
     patch=[patch("payments", "wire", "wire_id", "W-5001", amount="100000.00")])
wire("wire-amount-over-threshold", "Boundary: 100,000.01 is held", "W-5001", "DONE", "HOLD", "REVIEW", "HELD", "AMOUNT_THRESHOLD",
     "4000001001", description="One cent over the threshold. Decimal arithmetic, no float rounding.",
     patch=[patch("payments", "wire", "wire_id", "W-5001", amount="100000.01")])

# ---- address change: web UI + REST + 3270 -------------------------------------------
ADDR_APPS = ["crm", "kyc", "corebank"]


def address(sid, title, request_id, status, outcome, req_state=None, reason=None, customer=None, postal=None,
            acct=None, acct_postal=None, description="", **extra):
    a = [output("status", status), output("outcome", outcome)]
    if req_state:
        a.append(state("request-state", "crm", "request", request_id, state=req_state, **({"reason": reason} if reason else {})))
    if customer:
        a.append(state("crm-customer-address", "crm", "customer", customer, postal_code=postal))
    if acct:
        a.append(state("host-account-address", "corebank", "account", acct, postal_code=acct_postal))
    a += [event("crm", "request.completed", 1 if outcome == "COMPLETED" else 0),
          event("corebank", "address.updated", 1 if outcome == "COMPLETED" else 0)]
    write(sid, title, "address-change", ADDR_APPS, {"request_id": request_id}, a, description, **extra)


address("address-ok", "Verified customer, address updated everywhere", "AR-7001", "DONE", "COMPLETED", "COMPLETED",
        "ADDRESS_UPDATED", "C100", "03801", "4000001001", "03801",
        description="KYC verified; host and CRM both end with the new address.")
address("address-kyc-mismatch", "KYC mismatch: request rejected", "AR-7002", "DONE", "REJECTED", "REJECTED", "KYC_NOT_VERIFIED",
        "C102", "03820", "4000001003", None, description="Nothing may change on the host or on the customer record.")
address("address-invalid-postal", "Host rejects an invalid postal code", "AR-7003", "DONE", "REJECTED", "REJECTED",
        "HOST_REJECTED", "C101", "03060", "4000001002", None,
        description="The requested postal code is not five digits; the host refuses it and the CRM request is rejected.")
address("address-account-closed", "Host rejects update on a closed account", "AR-7001", "DONE", "REJECTED", "REJECTED",
        "HOST_REJECTED", "C100", "03301", "4000001001", None,
        patch=[patch("corebank", "account", "account_id", "4000001001", status="CLOSED")])
address("address-already-completed", "Request was already completed", "AR-7004", "SKIPPED", "NONE", "COMPLETED", None,
        "C103", "03431")
address("address-not-found", "Unknown request", "AR-9999", "NOT_FOUND", "NONE")
address("address-kyc-outage", "KYC is down", "AR-7001", "FAILED", "NONE", "PENDING", None, "C100", "03301", "4000001001", None,
        description="Without an identity answer the request must stay PENDING and nothing is updated.",
        faults=[{"app": "kyc", "operation": "get-identity", "occurrence": "all", "type": "unavailable"}])
address("address-kyc-record-missing", "Customer has no KYC record", "AR-7001", "FAILED", "NONE", "PENDING", None,
        "C100", "03301", "4000001001", None,
        remove=[{"app": "kyc", "entity": "identity", "key_field": "customer_id", "key": "C100"}])

# ---- account closure: 3270 only vs mainframe API ------------------------------------
def closure(sid, title, account, status, message, acct_status=None, description="", **extra):
    a = [output("status", status), output("message", message)]
    if acct_status:
        a.append(state("account-status", "corebank", "account", account, status=acct_status))
    a.append(event("corebank", "account.closed", 1 if status == "CLOSED" else 0))
    write(sid, title, "account-closure", ["corebank"], {"account_id": account}, a, description, **extra)


closure("closure-zero-balance", "Zero-balance account is closed", "4000002001", "CLOSED", "ACCOUNT CLOSED", "CLOSED",
        description="Green-screen original vs mainframe-API replacement must leave the same host state.")
closure("closure-balance-not-zero", "Balance not zero", "4000002002", "REJECTED", "BALANCE NOT ZERO - CLOSE REJECTED", "ACTIVE")
closure("closure-on-hold", "Account on hold", "4000002003", "REJECTED", "ACCOUNT ON HOLD - CLOSE REJECTED", "HOLD")
closure("closure-not-found", "Unknown account", "4999999999", "REJECTED", "ACCOUNT NOT FOUND")
closure("closure-already-closed", "Account already closed", "4000002001", "REJECTED", "ACCOUNT ALREADY CLOSED", "CLOSED",
        patch=[patch("corebank", "account", "account_id", "4000002001", status="CLOSED")])

# ---- case triage: real-browser UI vs API --------------------------------------------
def triage(sid, title, states, processed, failed, events, description="", **extra):
    a = [state(f"state-{c}", "qaqc", "case", c, state=s) for c, s in states]
    a += [output("processed", processed), output("failed", failed), event("qaqc", "case.evaluated", events)]
    write(sid, title, "case-triage", APPS, {}, a, description, **extra)


triage("triage-all-new", "Evaluate every NEW case", EXPECTED, 4, 0, 4,
       description="UI-only original (Chromium) vs API replacement.")
triage("triage-kyc-outage", "KYC down during triage", [(c, "NEW") for c, _ in EXPECTED[:4]], 0, 4, 0,
       description="Every evaluation fails; no case may change state.",
       faults=[{"app": "kyc", "operation": "get-identity", "occurrence": "all", "type": "unavailable"}])
triage("triage-nothing-new", "No NEW cases", [(c, "COMPLETED") for c, _ in EXPECTED], 0, 0, 0,
       patch=[patch("qaqc", "case", "case_id", c, state="COMPLETED") for c, _ in EXPECTED[:4]])

# ---- loan / account reconciliation: SQL + SQL + REST --------------------------------
def recon(sid, title, opened, hold, closed, description="", **extra):
    a = [output("checked", extra.pop("checked", 5)), output("opened", opened),
         count("cases-for-hold", "qaqc", "case", hold, reason="RECON_ACCOUNT_HOLD"),
         count("cases-for-closed", "qaqc", "case", closed, reason="RECON_ACCOUNT_CLOSED"),
         count("total-cases", "qaqc", "case", 5 + opened), event("qaqc", "case.created", opened)]
    write(sid, title, "loan-account-recon", APPS, {}, a, description, **extra)


recon("recon-clean", "Loans and accounts agree", 0, 0, 0, description="Nothing to open.")
recon("recon-two-mismatches", "Two active loans on inactive accounts", 2, 1, 1,
      description="One account on hold, one closed: exactly one case each.",
      patch=[patch("corebank", "account", "account_id", "4000001002", status="HOLD"),
             patch("corebank", "account", "account_id", "4000001004", status="CLOSED")])
recon("recon-closed-loan-ignored", "Closed loan on a closed account is not an exception", 0, 0, 0, checked=4,
      patch=[patch("mortgage", "loan", "loan_id", "L201", status="CLOSED"),
             patch("corebank", "account", "account_id", "4000001002", status="CLOSED")])

print(len(list(OUT.glob("*.yaml"))), "scenario files in", OUT)
