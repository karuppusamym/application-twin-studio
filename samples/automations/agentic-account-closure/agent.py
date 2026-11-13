"""Replacement for the green-screen closure bot: uses the mainframe REST API instead of 3270.

This is interface substitution - the strongest reason to keep one state model
behind the terminal and the API: the test can prove both leave the same state.
"""
import os

from ats.sdk import Twin

twin = Twin.from_env()
r = twin.http("corebank").post(f"/accounts/{twin.inputs['account_id']}/close")
print("close ->", r.status, r.text[:120])
if r.ok:
    if os.environ.get("AGENT_BUG") == "also_hold_neighbour":
        # simulated collateral damage: right answer, plus an unrequested change to another account
        twin.http("corebank").post("/accounts/4000002002/memos", {"code": "QCHOLD", "text": "CLEANUP"})
    twin.finish({"status": "CLOSED", "message": "ACCOUNT CLOSED"})
else:
    twin.finish({"status": "REJECTED", "message": r.json()["message"]})
