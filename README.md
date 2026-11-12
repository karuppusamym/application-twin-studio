# Application Twin Studio

Build **digital twins of the applications your automations touch** — web UI, REST API, database and
3270 mainframe screen sharing one state — and use them to test an automation before and after it is
converted: UiPath original vs. agentic replacement, or a backend-only job.

```
onboard automation ─► map steps to twin operations ─► gaps / readiness
                                                          │
fixture + faults ─► environment A ◄─ original automation  │
                 └► environment B ◄─ replacement          ▼
                    independent assertions + A/B diff ─► scoped certificate
```

> **Status: working first slice, not a finished product.** Sample twins use *demonstration rules* and
> synthetic data. A passing run proves consistency with the model, never equivalence with a real
> application. Read [Limits](#limits-read-this) before relying on anything here.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python -m ats serve
```

Open http://127.0.0.1:8700 — portal. http://127.0.0.1:8700/demo — playable browser-only demo
(also works by opening `demo/index.html` directly, no server needed).

Command line:

```bash
python -m ats suite uipath-loan-qc-review agentic-loan-qc-review
```

```bash
python -m ats compare kyc-mismatch uipath-loan-qc-review agentic-loan-qc-review --mutation qaqc-kyc-rule-wrong
```

```bash
python -m pytest -q
```

## What is in the box

| Piece | Where | What it does |
|---|---|---|
| ATS-Spec | `ats/spec.py`, `samples/apps/*.yaml` | A twin is a reviewed YAML file: entities, state transitions, operations made of allowlisted steps, web pages, 3270 screens. No embedded code. |
| Behaviour engine | `ats/runtime/engine.py` | Runs an operation in one transaction with its idempotency receipt and outbox events. Fault injection by call occurrence. |
| Web adapter | `ats/runtime/web.py` | Server-rendered pages with stable element ids, login session. Buttons call the same handlers as the API. |
| TN3270 adapter | `ats/runtime/tn3270.py`, `screens.py` | Host-side Telnet 3270 server (EBCDIC, SBA/SF/IC orders, AID keys) over a screen state machine, plus a small client. |
| SQL | `ats/runtime/store.py` | Every entity is a real SQLite table a batch job can query. |
| Environments | `ats/runtime/environment.py` | One isolated environment per run: own databases, token, clock, fault plan, TN3270 port. |
| UiPath importer | `ats/importers/uipath.py` | Static, safe XAML parsing. Unsupported activities become gaps. |
| Onboarding | `ats/onboarding.py` | Maps each node to a twin operation; produces gaps and READY / BLOCKED. |
| Runners | `ats/runners/` | `xaml-replay` (simulated UiPath subset), `command` (any agent / script), `uipath-robot` (hand-off to a licensed robot, untested). |
| Validation | `ats/validation.py` | Hand-written assertions, A/B business diff, scoped certificate. |
| Control plane | `ats/control/` | `/v1` API, twin gateway at `/env/{env}/{app}/...`, portal. |

### Sample scenario set

Five twins — QA/QC workbench (UI + API + SQL), Mortgage, KYC, Documents (API), Core Banking (3270 +
mainframe API + SQL) — and three automations of a synthetic "loan QC review" process:

* `uipath-loan-qc-review` — XAML: web UI login, evaluate, 3270 memo, API note with retry.
* `agentic-loan-qc-review` — tool-using agent loop: API-first plus the same 3270 host.
* `backend-batch-evaluate` — no UI: SQL read, API writes.
* `uipath-legacy-branch-report` — IE, desktop app, Excel, custom code: **correctly BLOCKED**.

13 scenarios: success, missing data, mismatch, dependency timeout with retry, outage, duplicate
(timeout after commit), conflict, not found, host rejection, resume, batch.

## Bring your own automation

Any tool works if it honours the bindings contract:

```
env ATS_BINDINGS -> JSON file {"apps": {app: {http, web, tn3270, sql, auth, credentials}}, "inputs": {...}, "output_path": "..."}
write a JSON result to output_path, exit 0
```

`ats/sdk.py` is a dependency-free helper (`Twin.from_env()`). To plug in LangGraph, CrewAI, the
Claude Agent SDK, Playwright, Selenium or a Java batch job, add a folder under `workspace/automations/`
with an `automation.yaml` (`runner: command`, `command: [...]`, `requires: [...]`).

## Bring your own application

Add `workspace/apps/<app>.yaml`. Run `python -m ats validate`. Start from `samples/apps/kyc.yaml`
(smallest) or `corebank.yaml` (terminal). Steps available: `load query require call decide insert
update delete emit respond if set`. Mark each operation's `verification` honestly:
`hypothesis | demonstration | owner_approved | reference_verified`.

## Limits (read this)

Verified here (27 automated tests, Windows, Python 3.13): everything in the table above, against the
sample twins and hand-authored sample automations.

**Not verified / not built:**

* **No real UiPath robot was used.** `xaml-replay` is a simulator for a small activity subset. The
  sample XAML is hand-authored to follow Studio's layout; a real Studio export has not been imported.
  The `uipath-robot` runner is an unexercised hand-off.
* **TN3270 was verified only with the bundled client.** No wc3270/x3270 or UiPath Terminal session has
  connected to it. TN3270E, structured fields, colour/highlighting and printer sessions are not implemented.
* **No real bank application, rule or data** was available. All rules are demonstration rules, so every
  certificate is `exploratory`.
* **SQL is SQLite.** Oracle / SQL Server dialects, stored procedures and locking are not emulated.
* **Not built:** SOAP, MQ/Kafka, file batch, IE mode, Windows desktop, AI-assisted spec authoring,
  OIDC/RBAC, release signing, PostgreSQL job leases, container/OpenShift deployment, React portal.
* **Local profile only:** binds to loopback with no authentication; jobs run on an in-process thread
  pool and a restart marks unfinished runs as ERROR.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and the gate-by-gate status.
