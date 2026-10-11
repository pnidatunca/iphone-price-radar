"""robots.txt parsing and matching with Google-style wildcards.

Only groups that apply to user-agent "*" are used. None of the target sites has a group for this crawler, and a
group written for another bot never applies to us.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str


def parse_robots(text: str) -> list[Rule]:
    """Return the Allow/Disallow rules of every group that applies to user-agent '*'."""
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            # A user-agent line after rules starts a new group; consecutive user-agent lines share one group.
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append(Rule(allow=key == "allow", pattern=value))
    if agents:
        groups.append((agents, rules))
    return [rule for group_agents, group_rules in groups if "*" in group_agents for rule in group_rules]


@lru_cache(maxsize=2048)
def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


def is_allowed(rules: list[Rule], path: str) -> bool:
    """Decide for a path (with query string). The longest matching pattern wins, Allow wins a tie, and a path that
    matches no rule is allowed. An empty Disallow means "allow everything" and is ignored."""
    best_len, best_allow = -1, True
    for rule in rules:
        if not rule.pattern or not _compile(rule.pattern).match(path):
            continue
        if len(rule.pattern) > best_len or (len(rule.pattern) == best_len and rule.allow):
            best_len, best_allow = len(rule.pattern), rule.allow
    return best_allow
