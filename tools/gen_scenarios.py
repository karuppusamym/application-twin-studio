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
print(len(list(OUT.glob("*.yaml"))), "scenario files in", OUT)
