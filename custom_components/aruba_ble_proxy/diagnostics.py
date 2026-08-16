from __future__ import annotations

from typing import Any

from .const import CONF_ACCESS_TOKEN, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_AP_SOURCE


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    redacted_data = _redact_credentials(entry.data)

    payload: dict[str, Any] = {
        "domain": DOMAIN,
        "entry": redacted_data,
        "options": _redact_credentials(entry.options),
    }
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_AP_SOURCE:
        payload["runtime"] = None
        return payload

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        payload["runtime"] = None
        return payload

    runtime_payload = dict(runtime.diagnostic_attributes())
    runtime_payload["receiver_connected_sources"] = sorted(
        runtime_payload.get("receiver_connected_sources", [])
    )
    payload["runtime"] = runtime_payload
    return payload


def _redact_credentials(data) -> dict[str, Any]:
    redacted = dict(data)
    if CONF_ACCESS_TOKEN in redacted:
        redacted[CONF_ACCESS_TOKEN] = "***redacted***"
    return redacted
