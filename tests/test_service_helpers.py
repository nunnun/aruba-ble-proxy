import asyncio
from pathlib import Path
import sys
import types

import pytest

from custom_components import aruba_ble_proxy as integration_module
from custom_components.aruba_ble_proxy import (
    _endpoint_url,
    _parse_hex_value,
    _runtime_for_ap,
    _service_data_to_config,
    async_remove_entry,
)
from custom_components.aruba_ble_proxy.const import (
    CONF_ACCESS_TOKEN,
    CONF_ENABLE_RADIO_PROFILE,
    CONF_ENDPOINT_PATH,
    CONF_ENTRY_TYPE,
    CONF_LISTEN_PORT,
    CONF_PUBLIC_HOST,
    CONF_PUBLIC_SCHEME,
    CONF_PARENT_ENTRY_ID,
    CONF_RADIO_PROFILE,
    CONF_TRANSPORT_PREFIX,
    DOMAIN,
    ENTRY_TYPE_AP_SOURCE,
)


def test_service_cli_config_forces_ws_scheme_without_public_scheme_field():
    data = _service_data_to_config(
        {
            CONF_PUBLIC_HOST: "wss://ha.example.local",
            CONF_LISTEN_PORT: 7443,
            CONF_ENDPOINT_PATH: "aruba-ble-proxy",
            CONF_ACCESS_TOKEN: "token",
            CONF_TRANSPORT_PREFIX: "ha-ble",
            CONF_ENABLE_RADIO_PROFILE: True,
            CONF_RADIO_PROFILE: "ha-ble-radio",
        }
    )

    assert data[CONF_PUBLIC_SCHEME] == "ws"
    assert _endpoint_url(data) == "ws://ha.example.local:7443/aruba-ble-proxy"


def test_service_endpoint_url_adds_port_to_bracketed_ipv6_address():
    data = _service_data_to_config(
        {
            CONF_PUBLIC_HOST: "[2001:db8::1]",
            CONF_LISTEN_PORT: 7443,
            CONF_ENDPOINT_PATH: "/aruba-ble-proxy",
            CONF_ACCESS_TOKEN: "secret",
            CONF_TRANSPORT_PREFIX: "ha-ble",
            CONF_ENABLE_RADIO_PROFILE: True,
            CONF_RADIO_PROFILE: "ha-ble-radio",
        }
    )

    assert _endpoint_url(data) == "ws://[2001:db8::1]:7443/aruba-ble-proxy"


def test_service_yaml_does_not_offer_unsupported_wss_option():
    services_yaml = Path("custom_components/aruba_ble_proxy/services.yaml").read_text(
        encoding="utf-8"
    )

    assert "public_scheme:" not in services_yaml
    assert "wss" not in services_yaml


def test_service_cli_config_rejects_command_injection():
    data = {
        CONF_PUBLIC_HOST: "ha.example.local",
        CONF_LISTEN_PORT: 7443,
        CONF_ENDPOINT_PATH: "/aruba-ble-proxy",
        CONF_ACCESS_TOKEN: "token",
        CONF_TRANSPORT_PREFIX: "ha-ble\ncommit apply",
        CONF_ENABLE_RADIO_PROFILE: True,
        CONF_RADIO_PROFILE: "ha-ble-radio",
    }

    try:
        _service_data_to_config(data)
    except ValueError as err:
        assert "profile name" in str(err)
    else:
        raise AssertionError("CLI command injection should fail validation")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_PUBLIC_HOST, "user@ha.example.local"),
        (CONF_PUBLIC_HOST, "ha example.local"),
        (CONF_PUBLIC_HOST, "[2001:db8::1"),
        (CONF_ENDPOINT_PATH, "/aruba proxy"),
        (CONF_ACCESS_TOKEN, "secret value"),
    ],
)
def test_service_cli_config_rejects_unsafe_whitespace_and_userinfo(field, value):
    data = {
        CONF_PUBLIC_HOST: "ha.example.local",
        CONF_LISTEN_PORT: 7443,
        CONF_ENDPOINT_PATH: "/aruba-ble-proxy",
        CONF_ACCESS_TOKEN: "token",
        CONF_TRANSPORT_PREFIX: "ha-ble",
        CONF_ENABLE_RADIO_PROFILE: True,
        CONF_RADIO_PROFILE: "ha-ble-radio",
    }
    data[field] = value

    with pytest.raises(ValueError):
        _service_data_to_config(data)


def test_removing_listener_removes_its_ap_child_entries_only():
    class Entry:
        def __init__(self, entry_id, data):
            self.entry_id = entry_id
            self.data = data

    parent = Entry("parent", {})
    own_child = Entry(
        "own-child",
        {
            CONF_ENTRY_TYPE: ENTRY_TYPE_AP_SOURCE,
            CONF_PARENT_ENTRY_ID: "parent",
        },
    )
    other_child = Entry(
        "other-child",
        {
            CONF_ENTRY_TYPE: ENTRY_TYPE_AP_SOURCE,
            CONF_PARENT_ENTRY_ID: "other-parent",
        },
    )

    class ConfigEntries:
        removed = []

        def async_entries(self, domain):
            assert domain == DOMAIN
            return [parent, own_child, other_child]

        async def async_remove(self, entry_id):
            self.removed.append(entry_id)
            return True

    class Hass:
        config_entries = ConfigEntries()

    asyncio.run(async_remove_entry(Hass(), parent))

    assert Hass.config_entries.removed == ["own-child"]


def test_services_are_registered_as_admin_only(monkeypatch):
    registrations = []

    class SupportsResponse:
        ONLY = "only"

    core = types.ModuleType("homeassistant.core")
    core.SupportsResponse = SupportsResponse
    service_helpers = types.ModuleType("homeassistant.helpers.service")
    service_helpers.async_register_admin_service = (
        lambda *args, **kwargs: registrations.append((args, kwargs))
    )
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.service", service_helpers)
    monkeypatch.setattr(integration_module, "_cli_service_schema", lambda: object())
    monkeypatch.setattr(
        integration_module,
        "_ble_action_service_schema",
        lambda: object(),
    )
    monkeypatch.setattr(
        integration_module,
        "_gatt_action_service_schema",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        integration_module,
        "_gatt_notify_service_schema",
        lambda: object(),
    )

    asyncio.run(integration_module.async_setup(types.SimpleNamespace(), {}))

    assert len(registrations) == 7
    assert all(call[0][0].__class__ is types.SimpleNamespace for call in registrations)
    assert all(call[1]["supports_response"] == SupportsResponse.ONLY for call in registrations)


def test_runtime_for_ap_normalizes_cli_service_source_mac():
    class Runtime:
        def __init__(self, sources):
            self.sources = sources

        def diagnostic_attributes(self):
            return {"receiver_connected_sources": self.sources}

    class Hass:
        data = {
            "aruba_ble_proxy": {
                "first": Runtime(["AA:BB:CC:DD:EE:FF"]),
                "second": Runtime(["02:00:00:00:00:01"]),
            }
        }

    runtime = _runtime_for_ap(Hass(), "020000000001")

    assert runtime.sources == ["02:00:00:00:00:01"]


def test_parse_hex_value_accepts_common_byte_separators():
    assert _parse_hex_value("57 0f:4e-01") == bytes.fromhex("570f4e01")
    assert _parse_hex_value("0x570f") == bytes.fromhex("570f")
    assert _parse_hex_value("") == b""


def test_parse_hex_value_rejects_odd_or_non_hex_input():
    try:
        _parse_hex_value("abc")
    except ValueError as err:
        assert "even number" in str(err)
    else:
        raise AssertionError("odd hex input should fail")

    try:
        _parse_hex_value("57 zz")
    except ValueError as err:
        assert "hexadecimal bytes" in str(err)
    else:
        raise AssertionError("non-hex input should fail")
