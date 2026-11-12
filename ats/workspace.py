"""File-based workspace: reviewed specs, fixtures, scenarios and automations live in Git.

Two roots are read: ``samples/`` (shipped) and ``workspace/`` (local additions
and uploads, git-ignored). Later roots override earlier ones by id.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import spec as specmod


class Workspace:
    def __init__(self, roots: list[Path]):
        self.roots = [Path(r) for r in roots]

    def _files(self, sub: str, pattern: str = "*.yaml") -> list[Path]:
        out: list[Path] = []
        for root in self.roots:
            d = root / sub
            if d.is_dir():
                out += sorted(d.glob(pattern))
        return out

    def specs(self) -> dict[str, dict]:
        specs, self.spec_problems = {}, {}
        for f in self._files("apps"):
            try:
                s = specmod.load_file(f)
                s["_file"] = str(f)
                specs[s["application_id"]] = s
            except specmod.SpecError as exc:
                self.spec_problems[f.name] = exc.errors
            except yaml.YAMLError as exc:
                self.spec_problems[f.name] = [f"invalid YAML: {exc}"]
        return specs

    def _docs(self, sub: str) -> dict[str, dict]:
        out = {}
        for f in self._files(sub):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if doc.get("id"):
                out[doc["id"]] = doc
        return out

    def fixtures(self) -> dict[str, dict]:
        return self._docs("fixtures")

    def scenarios(self) -> dict[str, dict]:
        return self._docs("scenarios")

    def mutations(self) -> dict[str, dict]:
        return self._docs("mutations")

    def automations(self) -> dict[str, dict]:
        out = {}
        for f in self._files("automations", "*/automation.yaml"):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if doc.get("id"):
                doc["_dir"] = str(f.parent)
                out[doc["id"]] = doc
        return out
