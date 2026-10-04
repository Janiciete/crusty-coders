"""Automatic condition detection (darkness, ice/snow).

Stub per CLAUDE.md §5: real weather (NWS) and sunrise/sunset (astral) wiring
lands in Prompt 7. Until then, `get_conditions` only interprets explicit
overrides ("on"/"off"); "auto" (or anything unrecognized) resolves to False
since there is no live data source wired up yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Conditions:
    darkness: bool = False
    ice: bool = False
    source: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def get_conditions(overrides: dict) -> Conditions:
    """Resolve darkness/ice flags from user overrides.

    overrides: e.g. {"darkness": "auto"|"on"|"off", "ice": "auto"|"on"|"off"}.
    Stub behavior: "on" -> True, "off" and "auto" -> False (no live weather/
    sunrise data until Prompt 7 wires in NWS + astral).
    """
    overrides = overrides or {}
    source = {}
    warnings = []

    def resolve(key: str) -> bool:
        raw = overrides.get(key, "auto")
        if raw == "on":
            source[key] = "override_on"
            return True
        if raw == "off":
            source[key] = "override_off"
            return False
        # "auto" or unrecognized value: stub has no live data source.
        source[key] = "auto_stub"
        if raw != "auto":
            warnings.append(f"unrecognized {key} override {raw!r}; treated as auto")
        return False

    darkness = resolve("darkness")
    ice = resolve("ice")
    return Conditions(darkness=darkness, ice=ice, source=source, warnings=warnings)
