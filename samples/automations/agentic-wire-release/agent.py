"""Agentic replacement for the wire-release automation: SOAP + REST + mainframe API.

AGENT_BUG=release_without_screening simulates a dangerous conversion defect.
"""
import os

from ats.sdk import Twin

twin = Twin.from_env()
pay, risk, host = twin.soap("payments"), twin.http("risk"), twin.http("corebank")
wire_id = twin.inputs["wire_id"]
out = {"status": None, "action": "NONE", "decision": "NONE"}


def finish(status):
    out["status"] = status
    twin.finish(out)
    raise SystemExit(0)


wire = pay.call("GetWire", {"wireId": wire_id})
print("GetWire ->", wire)
if not wire["ok"]:
    finish("NOT_FOUND")
if wire["fields"]["status"] != "PENDING":
    finish("SKIPPED")

# flaky_screening simulates an inconsistent agent: right on most attempts, skips screening on attempt 3.
bug = os.environ.get("AGENT_BUG")
if bug == "release_without_screening" or (bug == "flaky_screening" and os.environ.get("ATS_ATTEMPT") == "3"):
    pay.call("ReleaseWire", {"wireId": wire_id})
    out.update(decision="CLEAR", action="RELEASE")
    finish("DONE")

screening = risk.post("/screenings", {"wire_id": wire_id}, headers={"Idempotency-Key": f"screen-{wire_id}"})
print("screening ->", screening.status, screening.text[:120])
if screening.status != 201:
    finish("FAILED")
out["decision"] = decision = screening.json()["decision"]

if decision == "CLEAR":
    released = pay.call("ReleaseWire", {"wireId": wire_id})
    if released["ok"]:
        out["action"] = "RELEASE"
    else:
        pay.call("HoldWire", {"wireId": wire_id, "reason": "ACCOUNT_NOT_ACTIVE"})
        out["action"] = "HOLD"
elif decision == "REVIEW":
    pay.call("HoldWire", {"wireId": wire_id, "reason": "AMOUNT_THRESHOLD"})
    out["action"] = "HOLD"
elif decision == "BLOCK":
    pay.call("RejectWire", {"wireId": wire_id, "reason": "WATCHLIST"})
    host.post(f"/accounts/{wire['fields']['account_id']}/memos", {"code": "QCHOLD", "text": "WIRE BLOCKED"})
    out["action"] = "REJECT"
finish("DONE")
