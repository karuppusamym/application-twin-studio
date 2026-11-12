"""UI-only automation driven by a REAL browser (Playwright + Chromium).

Signs in to the QA/QC workbench, filters NEW cases and clicks Evaluate on each.
If no browser is installed it falls back to the form-level HTML driver and says
so in the trace, so the run is never silently a different kind of evidence.
"""
from ats.sdk import Twin

twin = Twin.from_env()
ui = twin.apps["qaqc"]["web"]
cred = twin.credentials("qaqc")
results = {}


def with_playwright():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{ui}/login")
        page.fill("#username", cred["username"])
        page.fill("#password", cred["password"])
        page.click("#btn-login")
        page.select_option("#filter-state", "NEW")
        page.click("#btn-filter")
        case_ids = [a.inner_text() for a in page.query_selector_all("#table-cases a")]
        for case_id in case_ids:
            page.goto(f"{ui}/cases/{case_id}")
            page.click("#btn-evaluate")
            failed = page.query_selector("#error-message") is not None
            results[case_id] = "ERROR" if failed else page.inner_text("#field-state")
        browser.close()


def with_html_driver():
    from ats.runners.htmldriver import HtmlDriver
    d = HtmlDriver([ui])
    d.goto(f"{ui}/login")
    d.type_into({"id": "username"}, cred["username"])
    d.type_into({"id": "password"}, cred["password"])
    d.click({"id": "btn-login"})
    d.goto(f"{ui}/cases?state=NEW")
    case_ids = [n.text() for n in d.dom.walk() if n.tag == "a" and n.attrs.get("id", "").startswith("link-")]
    for case_id in case_ids:
        d.goto(f"{ui}/cases/{case_id}")
        d.click({"id": "btn-evaluate"})
        results[case_id] = "ERROR" if d.exists({"id": "error-message"}) else d.get_text({"id": "field-state"})
    d.close()


try:
    with_playwright()
    driver = "playwright-chromium"
except Exception as exc:  # browser not installed on this worker
    print("playwright unavailable, using form-level driver:", type(exc).__name__)
    results.clear()
    with_html_driver()
    driver = "html-driver-fallback"

for case_id, state in results.items():
    print(case_id, "->", state)
print("driver:", driver)
twin.finish({"processed": sum(1 for s in results.values() if s != "ERROR"),
             "failed": sum(1 for s in results.values() if s == "ERROR"), "trace": {"driver": driver, "results": results}})
