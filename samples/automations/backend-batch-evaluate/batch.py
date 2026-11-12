"""Backend-only automation: no UI at all.

Reads NEW cases straight from the case table (read-only SQL) and evaluates
each one through the QA/QC API. The twin needs no frontend for this.
"""
from ats.sdk import Twin

twin = Twin.from_env()
db = twin.sql("qaqc")
new_cases = [row[0] for row in db.execute('SELECT case_id FROM "case" WHERE state = ? ORDER BY case_id', ("NEW",))]
db.close()

api = twin.http("qaqc")
results = {}
for case_id in new_cases:
    r = api.post(f"/cases/{case_id}/evaluate")
    results[case_id] = r.json()["state"] if r.ok else f"ERROR {r.status}"
    print(case_id, "->", results[case_id])

twin.finish({"processed": len(new_cases), "results": results})
