"""Backend reconciliation across two databases plus an API. No UI.

Reads ACTIVE loans from the Mortgage database and account status from the core
banking database (read-only SQL), and opens a QA/QC case through the API for
every active loan whose servicing account is not ACTIVE.
"""
from ats.sdk import Twin

twin = Twin.from_env()
loans_db, host_db = twin.sql("mortgage"), twin.sql("corebank")
loans = loans_db.execute("SELECT loan_id, customer_id, account_id FROM loan WHERE status = ? ORDER BY loan_id", ("ACTIVE",)).fetchall()
status = dict(host_db.execute("SELECT account_id, status FROM account").fetchall())
loans_db.close()
host_db.close()

api = twin.http("qaqc")
opened = []
for loan_id, customer_id, account_id in loans:
    if status.get(account_id) != "ACTIVE":
        reason = "RECON_ACCOUNT_" + status.get(account_id, "MISSING")
        r = api.post("/cases", {"customer_id": customer_id, "loan_id": loan_id, "account_id": account_id, "reason": reason},
                     headers={"Idempotency-Key": f"recon-{loan_id}"})
        print(loan_id, account_id, reason, "->", r.status)
        if r.status == 201:
            opened.append(r.json()["case_id"])
twin.finish({"checked": len(loans), "opened": len(opened), "case_ids": opened})
