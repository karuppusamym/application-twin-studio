"""Agentic replacement for the address-change automation: CRM API + KYC API + 3270 host."""
from ats.sdk import Twin

twin = Twin.from_env()
crm, kyc = twin.http("crm"), twin.http("kyc")
request_id = twin.inputs["request_id"]
out = {"status": None, "outcome": "NONE"}


def finish(status):
    out["status"] = status
    twin.finish(out)
    raise SystemExit(0)


def reject(reason):
    crm.post(f"/requests/{request_id}/reject", {"reason": reason})
    out["outcome"] = "REJECTED"
    finish("DONE")


def update_host(account, line, city, postal):
    cred = twin.credentials("corebank")
    t = twin.terminal("corebank")
    try:
        t.set_field(10, 35, cred["userid"])
        t.set_field(12, 35, cred["password"])
        t.send("ENTER")
        t.set_field(20, 15, "3")
        t.send("ENTER")
        t.set_field(5, 25, account)
        t.set_field(7, 25, line)
        t.set_field(9, 25, city)
        t.set_field(11, 25, postal)
        t.send("ENTER")
        message = t.get(23, 2, 78)
        t.send("PF3")
        t.set_field(20, 15, "X")
        t.send("ENTER")
        return "ADDRESS UPDATED" in message
    finally:
        t.close()


r = crm.get(f"/requests/{request_id}")
if r.status == 404:
    finish("NOT_FOUND")
req = r.json()
if req["state"] != "PENDING":
    finish("SKIPPED")
identity = kyc.get(f"/identities/{req['customer_id']}")
print("kyc ->", identity.status)
if identity.status != 200:
    finish("FAILED")
if identity.json()["status"] != "VERIFIED":
    reject("KYC_NOT_VERIFIED")
if not update_host(req["account_id"], req["new_address_line"], req["new_city"], req["new_postal_code"]):
    reject("HOST_REJECTED")
crm.post(f"/requests/{request_id}/complete")
out["outcome"] = "COMPLETED"
finish("DONE")
