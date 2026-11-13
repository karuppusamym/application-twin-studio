"""Pull-request guard for AI-authored (or any) changes to twins and scenarios.

Two rules keep the evidence trustworthy when an AI agent such as Devin writes the code:

1. No self-approval. A pull request may not raise an operation's `verification` to
   `owner_approved` or `reference_verified`. Only an application owner may do that, signalled
   by the pull-request label `owner-approved` (passed in ATS_PR_LABELS).
2. Independent oracle. One pull request may not change a twin's behaviour (samples/apps,
   workspace/apps) AND the expected results of scenarios (samples/scenarios, workspace/scenarios).
   Otherwise the same author can make both wrong in the same way and every test still passes.
   Override only with the label `oracle-reviewed`, after a person has checked the assertions.

Usage:  python tools/guard_ai_changes.py <base-ref>      (e.g. origin/main)
Exit 0 = OK, 1 = rule broken, 2 = usage error.
"""
from __future__ import annotations

import os
import subprocess
import sys

import yaml

APPROVED = {"owner_approved", "reference_verified"}
APP_DIRS = ("samples/apps/", "workspace/apps/")
SCENARIO_DIRS = ("samples/scenarios/", "workspace/scenarios/")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def levels(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    try:
        spec = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    return {op.get("id"): op.get("verification", "hypothesis") for op in spec.get("operations") or [] if isinstance(op, dict)}


def show(ref: str, path: str) -> str | None:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    base = git("merge-base", argv[1], "HEAD").strip()
    changed = [p for p in git("diff", "--name-only", base, "HEAD").splitlines() if p.strip()]
    labels = {x.strip() for x in os.environ.get("ATS_PR_LABELS", "").split(",") if x.strip()}
    problems = []

    for path in changed:
        if path.startswith(APP_DIRS) and path.endswith((".yaml", ".yml")):
            old, new = levels(show(base, path)), levels(show("HEAD", path))
            for op, level in new.items():
                if level in APPROVED and old.get(op) != level and "owner-approved" not in labels:
                    problems.append(f"{path}: operation '{op}' raised to {level}. Only an application owner may approve "
                                    "rules (add the 'owner-approved' label after their review).")

    twins = sorted(p for p in changed if p.startswith(APP_DIRS))
    oracles = sorted(p for p in changed if p.startswith(SCENARIO_DIRS))
    if twins and oracles and "oracle-reviewed" not in labels:
        problems.append("This change edits twin behaviour AND scenario expectations together: "
                        f"{', '.join(twins[:3])} + {', '.join(oracles[:3])}. Split it into two pull requests, "
                        "or have a person check the assertions and add the 'oracle-reviewed' label.")

    print(f"guard: {len(changed)} changed file(s) since {base[:10]}; labels: {sorted(labels) or '-'}")
    for p in problems:
        print("FAIL", p)
    if not problems:
        print("OK   no self-approved rules; twin and expected results not changed together")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
