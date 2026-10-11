"""robots.txt parsing and matching (RFC 9309, with Google-style '*' and '$' wildcards).

If a robots.txt has groups addressed to the crawler's product token, those groups apply (matched case-insensitively,
ignoring any version suffix); otherwise the groups for "*" apply. Groups written for other bots never apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str


def parse_robots(text: str, product_token: str = "") -> list[Rule]:
    """Return the Allow/Disallow rules that apply to the crawler named by product_token."""
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
            agents.append(_agent_token(value))
        elif key in ("allow", "disallow") and agents:
            rules.append(Rule(allow=key == "allow", pattern=value))
    if agents:
        groups.append((agents, rules))

    token = _agent_token(product_token)
    own = [group_rules for group_agents, group_rules in groups if token and token in group_agents]
    if own:
        return [rule for group_rules in own for rule in group_rules]
    return [rule for group_agents, group_rules in groups if "*" in group_agents for rule in group_rules]


def _agent_token(value: str) -> str:
    """'IphonePriceRadar/0.1' and 'iphonepriceradar' name the same product token."""
    return value.split("/", 1)[0].strip().lower()


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
