"""Agents name themselves in their own on-disk vocabulary; consumers use agent kinds.

The mapping lives here so no other component has to know an agent's self-name.
"""

from __future__ import annotations

from dataclasses import dataclass

#: On-disk self-name -> agent kind. Only kinds this package can actually read
#: belong here; adding a row is a coverage claim.
AGENT_KIND_BY_SOURCE = {
    "claude": "claude-code",
}


def map_source(source: str) -> str | None:
    """Return the agent kind for an on-disk source name, or None if unmapped.

    None is a *coverage statement* — the session is still collected and still
    counted; it simply resolves less. It is never an error and never a guess.
    """
    return AGENT_KIND_BY_SOURCE.get(source)


@dataclass(frozen=True)
class KindSelection:
    """Which agent kinds the caller asked for."""

    kinds: frozenset[str] | None

    def includes(self, kind: str | None) -> bool:
        if self.kinds is None:
            return True
        return kind is not None and kind in self.kinds


def parse_kind_filter(values: list[str] | None) -> KindSelection:
    if not values:
        return KindSelection(kinds=None)
    return KindSelection(kinds=frozenset(values))
