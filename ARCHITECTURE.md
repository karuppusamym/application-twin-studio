# Architecture

## Principles carried into code

1. **One state model behind every interface.** HTTP routes, web buttons and terminal keys all end in
   `TwinApp.invoke`. The UI and the terminal hold no rules of their own.
2. **No silent success.** An unmatched request returns `501 UNMODELED_OPERATION` and is recorded; an
   unsupported activity blocks readiness; an unresolved binding blocks the run.
3. **Isolation per run.** Original and replacement never share databases, tokens, sessions or ports.
4. **The oracle is independent.** Expected outcomes live in the scenario file, written by hand. Two
   runners agreeing against a wrong twin still fail (`tests/test_e2e.py::test_wrong_twin_rule...`).
5. **Confidence is stated, not implied.** Certificates carry `exploratory | model_validated |
   reference_validated` and the reasons they cannot go higher.
6. **Agents are judged on every attempt and on everything they touched.** `--repeat K` passes only if all K
   attempts pass and match the original; `no-unexpected-changes` fails any changed row the scenario did not
   expect. AI-authored pull requests cannot approve their own rules or rewrite their own oracle
   (`tools/guard_ai_changes.py`).
7. **Uploaded content is data.** XAML is parsed, never executed; DTDs are rejected; archives are
   size-limited and checked for path traversal; specs can only use allowlisted steps and functions.

## Request path

```
automation ──HTTP──► /env/{env}/{app}/cases/QC-1/evaluate ─┐
automation ──HTTP──► /env/{env}/{app}/ui/cases/QC-1  (form)├─► TwinApp.invoke(op) ─► steps in 1 txn
automation ──TN3270► 127.0.0.1:{port} ─► ScreenSession ────┘        │   ├─ state rows
                                                                    │   ├─ idempotency receipt
                    env.call(dep, op)  ◄── call step ───────────────┘   └─ outbox event
```

The control plane is not in the business-call path except as the process hosting the gateway in the
local profile.

## Run pipeline

`GATE → PROVISION+SEED → PREFLIGHT → EXECUTE → COLLECT → ASSERT → CLEANUP`

* GATE re-runs onboarding; a BLOCKED automation never gets an environment.
* SEED is all-or-nothing and validates cross-application references declared in the fixture.
* CLEANUP always runs and reports its own status.
* Run status separates `FAILED` (assertions) from `ERROR` (infrastructure) and `BLOCKED` (gate/preflight).

## Local profile vs. blueprint target

| Concern | Blueprint target | Built here | Why / consequence |
|---|---|---|---|
| Metadata + jobs | PostgreSQL, leases, fencing tokens | SQLite documents, thread pool | Single process. Restart → unfinished runs become ERROR. No multi-worker recovery. |
| Twin state | Database per app per environment | SQLite file per app per environment | Same isolation shape; no vendor SQL semantics. |
| Environment boundary | Namespace per run, NetworkPolicy | Directory + path prefix + per-env token + per-env TN3270 port | Logical isolation only; not a security boundary. |
| Portal | React + TypeScript | Single static HTML file | No build step; fine for a slice, not for a large UI. |
| Evidence store | Object storage with hashes | Run documents with spec/fixture/package digests | No evidence artefact store or retention policy. |
| Identity | OIDC + workspace RBAC | None (loopback only) | Must not be exposed on a network. |
| Releases | Signed immutable bundles, SBOM | Digest lock recorded per run | No signing, no rollback tooling. |
| AI authoring | LLM proposes spec diffs from evidence | Not built | Specs are hand-authored YAML. |

## Acceptance gates (blueprint v3 §12) — measured status

| Gate | Target | Status |
|---|---|---|
| Scenario coverage | ≥ 20 versioned cases | **Met: 46 checks (41 scenarios)** across 7 processes and 8 twins (success, missing data, mismatch, boundary, timeout, retry, duplicate, conflict, host rejection). Cleanup-failure is not covered. |
| Determinism | 10 repeats identical | **Met** for one scenario/runner (`test_repeated_runs_are_deterministic`); not run for every case |
| Reuse | Second automation reuses twin | **Met**: 6 automations share the QA/QC twin; 9 share the core banking twin through three different interfaces |
| Isolation | 20 concurrent, zero cross-run access | **Met** at logical level (`test_concurrent_runs_do_not_interfere`, cross-environment token rejected) |
| Recovery | Kill workers at each stage | **Not met**: restart marks runs ERROR; no resume |
| Readiness integrity | Unsupported/unbound/missing blocks | **Met** (`test_unsupported_automation_is_blocked_and_cannot_run`) |
| Rollback | Previous bundle provisions | **Not built** |
| Negative: wrong rule detected | — | **Met** |
| Negative: defective replacement detected | — | **Met** |
| Agent consistency (pass every attempt) | — | **Met** as a mechanism (`--repeat K`); flaky sample agent caught at 4/5. Sample agents are not LLM-driven |
| Collateral changes detected | — | **Met** (`test_collateral_change_is_caught...`) |
| AI-authored change guard | — | **Built** (`tools/guard_ai_changes.py`); runs in CI once the workflow is enabled |
| Real UiPath package on licensed robot | — | **Not done** |
| Real 3270 client conformance | — | **Not done** |
| Real browser against the web twin | — | **Met** for one process (Playwright + Chromium) |
| Interface substitution (3270 original vs API replacement) | — | **Met** (`test_interface_substitution...`) |
| Interface substitution (3270 original vs host web front end) | — | **Met** (`test_host_web_front_end_replaces_the_terminal`) |

## Extending

* **New protocol adapter**: add the protocol to `spec.PROTOCOLS`, implement it under `ats/runtime/`,
  bind it in `Environment.provision` / `bindings`, and add a conformance test with the real client.
* **New UiPath activity**: add it to `importers.uipath.SUPPORTED` *and* implement `_a_<Name>` in
  `runners/xaml_replay.py`. One without the other is caught by the replay raising `UnsupportedActivity`.
* **New step type**: add to `spec.STEP_TYPES` and implement `_step_<name>` in the engine. Keep it
  declarative; nothing may evaluate text from a spec.
