"""Agent registry — who exists and how the orchestrator reaches them.

Mac/orchestrator-side (uses PyYAML for the human-edited registry.yaml). The
transport modules (message.py, mailbox.py) stay stdlib-only; a bare remote
runner gets its own identity from env/args, not by parsing this file.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = "Worker/agents/registry.yaml"

KIND_HUMAN = "human"
KIND_AI_LOCAL = "ai-local"
KIND_AI_REMOTE = "ai-remote"

# Capability a remote advertises to receive heavy tasks (gradle builds,
# emulator runs, long jobs) — see Orchestrator._route_decision.
CAP_HEAVY = "heavy"
# Capability a heavy remote adds when it can run the post-verify device check
# (screen check / emulator) itself. Without it a remote never gets a task that may
# need one — see orchestrator.needs_device.
CAP_DEVICE = "device"


@dataclass
class Agent:
    id: str
    kind: str
    projects: list[str] = field(default_factory=list)
    host: str | None = None
    mailbox: str = "git://Worker/agents"
    repo: str | None = None            # project git repo an AI agent works in
    display_name: str | None = None
    notify: list[str] = field(default_factory=list)
    reply_via: list[str] = field(default_factory=list)
    sla: str | None = None
    renudge_backoff: list[str] = field(default_factory=list)
    wake: str | None = None
    concurrency: int = 1
    capabilities: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_human(self) -> bool:
        return self.kind == KIND_HUMAN

    @property
    def is_remote(self) -> bool:
        return self.kind == KIND_AI_REMOTE

    def handles(self, project_slug: str) -> bool:
        """True if this agent owns `project_slug` (exact or glob, e.g. withdrawal-*)."""
        return any(fnmatch.fnmatch(project_slug, pat) for pat in self.projects)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Agent":
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        kw = {k: v for k, v in d.items() if k in known}
        return cls(**kw, raw=d)


class Registry:
    def __init__(self, agents: list[Agent]):
        self.agents = agents
        self._by_id = {a.id: a for a in agents}
        if len(self._by_id) != len(agents):
            dupes = [a.id for a in agents if list(a.id for a in agents).count(a.id) > 1]
            raise ValueError(f"duplicate agent id(s) in registry: {sorted(set(dupes))}")

    def get(self, agent_id: str) -> Agent:
        try:
            return self._by_id[agent_id]
        except KeyError:
            raise KeyError(f"no agent '{agent_id}' in registry") from None

    def for_project(self, project_slug: str) -> Agent | None:
        """Resolve the owning agent for a task/project slug.

        Exact project match wins over a glob match; among globs, the most
        specific (longest non-wildcard prefix) wins so `withdrawal-a2` prefers
        an agent listing `withdrawal-a2` over one listing `withdrawal-*`.
        """
        exact = [a for a in self.agents if project_slug in a.projects]
        if exact:
            return exact[0]
        globbers = [a for a in self.agents if a.handles(project_slug)]
        if not globbers:
            return None
        return max(globbers, key=lambda a: self._match_specificity(a, project_slug))

    @staticmethod
    def _match_specificity(agent: Agent, slug: str) -> int:
        best = 0
        for pat in agent.projects:
            if fnmatch.fnmatch(slug, pat):
                best = max(best, len(pat.replace("*", "")))
        return best

    def humans(self) -> list[Agent]:
        return [a for a in self.agents if a.is_human]

    def heavy_remotes(self) -> list[Agent]:
        """Remote agents advertising the `heavy` capability, in registry order."""
        return [a for a in self.agents
                if a.is_remote and CAP_HEAVY in (a.capabilities or [])]


def load_registry(path: str | Path | None = None, base: Path | None = None) -> Registry:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PyYAML required to read the agent registry — `uv add pyyaml`"
        ) from exc
    p = Path(path or DEFAULT_REGISTRY)
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Registry([Agent.from_dict(a) for a in data.get("agents", [])])
