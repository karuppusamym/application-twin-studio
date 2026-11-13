# Instructions for AI coding agents

This repository builds digital twins of applications so automations can be tested against them.
Full playbook: [docs/devin-playbook.md](docs/devin-playbook.md).

## Before opening a pull request, run

```bash
python -m ats validate
python -m pytest -q
python -m ats all --repeat 3
```

Report the results as they are. Never loosen or delete an assertion to make a run pass.

## Hard rules

* Twin specs (`samples/apps/*.yaml`, `workspace/apps/*.yaml`) use only the allowlisted steps and
  functions; no code in specs.
* Every operation you write is `verification: hypothesis`. Never set `owner_approved` or
  `reference_verified`; only an application owner may.
* Do not change a twin's behaviour and scenario expectations (`samples/scenarios/`) in the same pull request.
* Do not add `allowed_changes` to a scenario to hide a change your automation made.
* Unknown behaviour stays unmodelled (the twin answers 501). Do not guess rules.
* Synthetic data only. No real customer data, account numbers or production screens.
* Converted automations go in `workspace/automations/<id>/` with `runner: command`, `role: replacement`
  and `replaces: <original id>`, and must pass `python -m ats suite <original> <id> --repeat 3`.

CI enforces the first three rules with `tools/guard_ai_changes.py`.
