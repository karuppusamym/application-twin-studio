# Playbook: building digital twins and converted automations with an AI coding agent

Written for Devin (paste it as a Devin playbook), but it works for any coding agent that opens pull
requests: Claude Code, Copilot agent, Cursor, and so on. The short form lives in [`AGENTS.md`](../AGENTS.md).

## How it fits together

```
evidence (OpenAPI / WSDL / screen captures / UiPath project / DB schema)
        │
        ▼
 AI agent session ──► branch ──► pull request ──► CI: validate · tests · all processes ×3 · guard
                                        │
                                        ▼
                      people review: app owner approves rules, tester owns expected results
```

The agent writes files; the studio and CI decide whether they are trustworthy. Nothing an agent
writes can raise confidence on its own.

## Setup (once, by a person)

1. In Devin, connect the GitHub repository `application-twin-studio`.
2. Add this file as a playbook (or point Devin's knowledge at `AGENTS.md`).
3. Enable CI: copy `tools/github-actions-ci.yml` to `.github/workflows/ci.yml`. Pushing it needs a
   GitHub token with the `workflow` scope, or add it in the GitHub web editor.
4. Protect `main`: require the `ci` check and one human review before merging.
5. Create two labels: `owner-approved` and `oracle-reviewed` (see "Trust rules").

## Task types and what to give the agent

| Task | Give the agent | Agent produces | Done when |
|---|---|---|---|
| New application twin | API spec (OpenAPI/WSDL), screen captures, DB schema, a description of the rules | `samples/apps/<app>.yaml` (+ fixture rows) | `python -m ats validate` OK; every operation `verification: hypothesis` |
| Extend a twin | The missing operation from an onboarding gap (`python -m ats onboard <automation>`) | Edited spec | Gap closed; existing scenarios still pass |
| Convert a UiPath bot to an agent | The UiPath project folder in `samples/automations/` or `workspace/automations/` | `workspace/automations/<id>/` with `automation.yaml` (`runner: command`, `role: replacement`, `replaces:`) and the agent code using `ats/sdk.py` | `python -m ats suite <original> <new> --repeat 3` all EQUIVALENT_IN_SCOPE |
| Onboard a real UiPath project | The exported project | `workspace/automations/<id>/` + `automation.yaml` arguments mapped to bindings | `python -m ats onboard <id>` READY, or every gap listed honestly |
| New scenarios | The process and the cases to cover (boundary, timeout, outage, duplicate...) | `samples/scenarios/*.yaml` | Reviewed by a person, in a pull request that does NOT change twins |

## Rules for writing a twin spec

* Only the allowlisted steps: `load query require call decide insert update delete emit respond if set`.
  Only allowlisted functions: `$concat $add $sub $mul $coalesce $count $upper $trim $str`. No code in specs.
* Protocols available: `http`, `web`, `tn3270`, `sql`, `soap`. Anything else (MQ, Kafka, desktop, 5250)
  is declared but not executable; say so instead of faking it.
* Every operation the agent writes gets `verification: hypothesis`. Never `owner_approved` or
  `reference_verified`.
* Copy names from the evidence: real field names, error codes, screen titles and positions, element
  ids. A twin with invented names tests nothing about the real application.
* Unknown behaviour stays unmodelled. The twin returns `501 UNMODELED_OPERATION` and the run fails
  visibly. Guessing a rule is worse than leaving the gap.
* Start from `samples/apps/kyc.yaml` (smallest), `crm.yaml` (web UI), `corebank.yaml` (3270 + API + web),
  `payments.yaml` (SOAP).

## Trust rules (enforced by `tools/guard_ai_changes.py` in CI)

1. **No self-approval.** A pull request that raises an operation to `owner_approved` or
   `reference_verified` fails unless an application owner adds the `owner-approved` label.
2. **Independent oracle.** A pull request may change twin behaviour **or** scenario expectations, not
   both. Otherwise the same author can make both wrong in the same way and all tests still pass.
   Override only with `oracle-reviewed`, after a person has checked the assertions.
3. **Agents are tested repeatedly.** CI runs every process with `--repeat 3`. A converted agent must
   pass every attempt, not most of them.
4. **Nothing else may change.** Every run fails if the automation changed a row the scenario did not
   expect (`no-unexpected-changes`). If a change is legitimate, the scenario declares it under
   `allowed_changes` — written by the tester, not by the agent that made the change.

## Data rules

* Synthetic data only in fixtures and prompts. Do not paste real customer data, account numbers or
  production screen captures into an agent session unless your organisation has approved that tool
  for that data.
* Test credentials live only in fixtures (`credentials:`) and are valid only inside a twin environment.

## Commands the agent must run before opening a pull request

```bash
python -m ats validate
python -m pytest -q
python -m ats all --repeat 3
```

Paste the last lines of each into the pull-request description. A failing run is reported as
failing; it is never "fixed" by loosening an assertion.

## Example prompts

* "Using the OpenAPI file in `evidence/limits-api.yaml`, create a twin `samples/apps/limits.yaml` for the
  credit-limit service. Model only `GET /limits/{customer}` and `POST /limits/{customer}/reserve`.
  All operations `verification: hypothesis`. Follow `docs/devin-playbook.md`."
* "Convert `samples/automations/uipath-address-change` into an agent in
  `workspace/automations/agentic-address-change-v2` that uses REST for KYC and the mainframe web front
  end instead of the 3270 terminal. Run the suite with `--repeat 3` and report every result."
* "Onboarding of `uipath-legacy-branch-report` shows gaps. List which ones a twin could close and
  which need a new protocol adapter. Do not change any files."

## Calling Devin from the studio (later)

Devin also has an API for starting sessions from code. A future step is for the studio to open a
Devin session automatically when onboarding reports a missing operation, and attach the resulting pull
request to the gap. It is not built: start with the manual flow above and add automation once its pull
requests are reliably good.
