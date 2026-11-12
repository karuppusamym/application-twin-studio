"""API-only replacement for the UI triage bot."""
from ats.sdk import Twin

twin = Twin.from_env()
api = twin.http("qaqc")
results = {}
for case in api.get("/cases?state=NEW").json()["items"]:
    r = api.post(f"/cases/{case['case_id']}/evaluate")
    results[case["case_id"]] = r.json()["state"] if r.ok else "ERROR"
    print(case["case_id"], "->", results[case["case_id"]])
twin.finish({"processed": sum(1 for s in results.values() if s != "ERROR"),
             "failed": sum(1 for s in results.values() if s == "ERROR"), "trace": {"results": results}})
