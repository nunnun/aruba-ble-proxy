import importlib
import asyncio
import sys
import types

import pytest


def _install_config_flow_stubs():
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda value: value
    voluptuous.Required = lambda key, default=None: key
    voluptuous.Optional = lambda key, default=None: key
    voluptuous.Invalid = ValueError

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class OptionsFlow:
        pass

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow

    core = types.ModuleType("homeassistant.core")
    core.callback = lambda func: func

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.config_entries = config_entries
    homeassistant.core = core

    sys.modules.setdefault("voluptuous", voluptuous)
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.config_entries", config_entries)
    sys.modules.setdefault("homeassistant.core", core)


_install_config_flow_stubs()
config_flow = importlib.import_module("custom_components.aruba_ble_proxy.config_flow")

from custom_components.aruba_ble_proxy.const import (  # noqa: E402
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_CONNECTION_SLOTS,
    CONF_ENABLE_ACTIVE_BLE,
    CONF_ENDPOINT_PATH,
    CONF_LISTEN_PORT,
    CONF_PUBLIC_HOST,
    CONF_PUBLIC_SCHEME,
)


def test_listener_schema_uses_frontend_serializable_value_types():
    defaults = config_flow._data_with_defaults({CONF_PUBLIC_HOST: "ha.local"})
    schema = config_flow._listener_data_schema(defaults)

    assert schema[CONF_LISTEN_PORT] is int
    assert schema[CONF_PUBLIC_HOST] is str
    assert schema[CONF_ENDPOINT_PATH] is str
    assert schema[CONF_ACTIVE_CONNECTION_SLOTS] is int


def test_validate_listener_data_applies_non_schema_security_validation():
    data = config_flow._validate_listener_data(
        {
            CONF_PUBLIC_HOST: "ha.local",
            CONF_ENDPOINT_PATH: "aruba",
        }
    )

    assert data[CONF_PUBLIC_HOST] == "ha.local"
    assert data[CONF_ENDPOINT_PATH] == "/aruba"

    with pytest.raises(ValueError):
        config_flow._validate_listener_data(
            {
                CONF_PUBLIC_HOST: "ha.local/path",
                CONF_ENDPOINT_PATH: "/aruba",
            }
        )


def test_data_defaults_enable_active_ble_for_1_0():
    data = config_flow._data_with_defaults({})

    assert data[CONF_ENABLE_ACTIVE_BLE] is True
    assert data[CONF_ACTIVE_CONNECTION_SLOTS] == 3


def test_data_defaults_reject_invalid_active_connection_slots():
    with pytest.raises(ValueError, match="between 1 and"):
        config_flow._data_with_defaults({CONF_ACTIVE_CONNECTION_SLOTS: 0})


def test_data_defaults_preserve_disabled_active_ble():
    data = config_flow._data_with_defaults({CONF_ENABLE_ACTIVE_BLE: False})

    assert data[CONF_ENABLE_ACTIVE_BLE] is False


def test_data_defaults_clean_endpoint_path():
    data = config_flow._data_with_defaults({CONF_ENDPOINT_PATH: "aruba"})

    assert data[CONF_ENDPOINT_PATH] == "/aruba"


def test_endpoint_url_forces_websocket_scheme_and_port():
    data = config_flow._data_with_defaults(
        {
            CONF_PUBLIC_HOST: "https://ha.example.local",
            CONF_LISTEN_PORT: 7443,
            CONF_ENDPOINT_PATH: "aruba-ble-proxy",
        }
    )

    assert (
        config_flow._endpoint_url(data)
        == "ws://ha.example.local:7443/aruba-ble-proxy"
    )


def test_endpoint_url_keeps_explicit_port():
    data = config_flow._data_with_defaults(
        {
            CONF_PUBLIC_HOST: "ws://ha.example.local:8123",
            CONF_LISTEN_PORT: 7443,
            CONF_ENDPOINT_PATH: "/aruba-ble-proxy",
        }
    )

    assert (
        config_flow._endpoint_url(data)
        == "ws://ha.example.local:8123/aruba-ble-proxy"
    )


def test_default_public_host_brackets_ipv6_address():
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(internal_url="http://[2001:db8::1]:8123")
    )

    assert config_flow._default_public_host(hass) == "[2001:db8::1]"


def test_endpoint_url_adds_port_to_bracketed_ipv6_address():
    data = config_flow._data_with_defaults(
        {
            CONF_PUBLIC_HOST: "[2001:db8::1]",
            CONF_LISTEN_PORT: 7443,
        }
    )

    assert (
        config_flow._endpoint_url(data)
        == "ws://[2001:db8::1]:7443/aruba-ble-proxy"
    )


def test_data_defaults_preserve_existing_token():
    data = config_flow._data_with_defaults({CONF_ACCESS_TOKEN: "secret"})

    assert data[CONF_ACCESS_TOKEN] == "secret"
    assert data[CONF_PUBLIC_SCHEME] == "ws"


@pytest.mark.parametrize("port", [0, 65536, True, "invalid"])
def test_data_defaults_reject_invalid_port(port):
    with pytest.raises(ValueError):
        config_flow._data_with_defaults({CONF_LISTEN_PORT: port})


@pytest.mark.parametrize(
    ("validator", "value"),
    [
        (config_flow._validate_public_host, "ha.local/path"),
        (config_flow._validate_public_host, "ha.local\nmalicious"),
        (config_flow._validate_public_host, "[2001:db8::1"),
        (config_flow._validate_public_host, "user@ha.local"),
        (config_flow._validate_public_host, "ha local"),
        (config_flow._validate_endpoint_path, "/aruba?token=value"),
        (config_flow._validate_cli_name, "profile\nconfigure terminal"),
        (config_flow._validate_access_token, "secret\nvalue"),
        (config_flow._validate_access_token, "secret value"),
    ],
)
def test_config_validators_reject_unsafe_values(validator, value):
    with pytest.raises(ValueError):
        validator(value)


def test_cli_placeholders_use_executor_when_available(monkeypatch):
    calls = []

    class Hass:
        async def async_add_executor_job(self, func, *args):
            calls.append((func, args))
            return func(*args)

    monkeypatch.setattr(config_flow, "_cli_profile_count", lambda: 3)
    monkeypatch.setattr(config_flow, "_cli_filter_count", lambda: 21)
    monkeypatch.setattr(config_flow, "_generate_cli", lambda data: "configure terminal")
    monkeypatch.setattr(config_flow, "_generate_cleanup_cli", lambda data: "cleanup")

    data = config_flow._data_with_defaults({CONF_PUBLIC_HOST: "ha.local"})
    placeholders = asyncio.run(config_flow._async_build_cli_placeholders(Hass(), data))

    assert calls
    assert calls[0][0] is config_flow._build_cli_placeholders
    assert placeholders["profile_count"] == "3"
    assert placeholders["filter_count"] == "21"
    assert placeholders["cli_config"] == "configure terminal"
    assert placeholders["cleanup_cli_config"] == "cleanup"


def test_bind_probe_closes_temporary_server(monkeypatch):
    calls = []

    class Server:
        def close(self):
            calls.append("close")

        async def wait_closed(self):
            calls.append("wait_closed")

    async def start_server(callback, host, port):
        calls.append((callback, host, port))
        return Server()

    monkeypatch.setattr(config_flow.asyncio, "start_server", start_server)

    asyncio.run(config_flow._async_test_bind_port("0.0.0.0", 7443))

    assert calls == [
        (config_flow._close_probe_connection, "0.0.0.0", 7443),
        "close",
        "wait_closed",
    ]
