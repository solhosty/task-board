"""Provider-account selection; credentials never leave their assigned runner."""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


def eligible(bindings: Iterable[Dict[str, Any]], provider: Optional[str] = None,
             moment: Optional[datetime] = None) -> list[Dict[str, Any]]:
    moment = moment or datetime.now(timezone.utc)
    result = []
    for binding in bindings:
        if not binding.get("enabled") or (provider and binding.get("provider") != provider):
            continue
        cooldown = binding.get("cooldown_until")
        if cooldown and datetime.fromisoformat(cooldown.replace("Z", "+00:00")) > moment:
            continue
        result.append(binding)
    return sorted(result, key=lambda item: (item.get("priority", 0), item["id"]))


def next_binding(bindings: Iterable[Dict[str, Any]], providers: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Respect provider preference, then account priority within that provider."""
    materialized = list(bindings)
    for provider in providers:
        choices = eligible(materialized, provider)
        if choices:
            return choices[0]
    return None
