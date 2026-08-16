from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from .aruba_cli import (
    chunked,
    default_uuid_seed_path,
    parse_uuid_file,
    render_aruba_cleanup_config,
    render_aruba_config,
)

_uuid_cache: list[str] | None = None
MAX_ACTIVE_CONNECTION_SLOTS = 32
_CLI_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _cached_uuid_list() -> list[str]:
    global _uuid_cache
    if _uuid_cache is None:
        _uuid_cache = parse_uuid_file(default_uuid_seed_path())
    return _uuid_cache

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_CONNECTION_SLOTS,
    CONF_AP_SOURCE,
    CONF_ENABLE_RADIO_PROFILE,
    CONF_ENABLE_ACTIVE_BLE,
    CONF_ENDPOINT_PATH,
    CONF_ENTRY_TYPE,
    CONF_LISTEN_HOST,
    CONF_LISTEN_PORT,
    CONF_PARENT_ENTRY_ID,
    CONF_PUBLIC_HOST,
    CONF_PUBLIC_SCHEME,
    CONF_RADIO_PROFILE,
    CONF_SETUP_COMPLETE,
    CONF_TRANSPORT_PREFIX,
    DEFAULT_ENDPOINT_PATH,
    DEFAULT_ACTIVE_CONNECTION_SLOTS,
    DEFAULT_ENABLE_ACTIVE_BLE,
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    DEFAULT_PUBLIC_SCHEME,
    DEFAULT_RADIO_PROFILE,
    DEFAULT_TRANSPORT_PREFIX,
    DOMAIN,
    ENTRY_TYPE_AP_SOURCE,
    ENTRY_TYPE_LISTENER,
)


def _default_public_host(hass) -> str:
    url = (
        getattr(hass.config, "internal_url", None)
        or getattr(hass.config, "external_url", None)
        or ""
    )
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.hostname or url
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _clean_path(path: str) -> str:
    path = path.strip() or DEFAULT_ENDPOINT_PATH
    return path if path.startswith("/") else f"/{path}"


def _validate_listen_port(value: Any) -> int:
    if isinstance(value, bool):
        raise vol.Invalid("port must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("port must be an integer") from err
    if not 1 <= port <= 65535:
        raise vol.Invalid("port must be between 1 and 65535")
    return port


def _validate_active_connection_slots(value: Any) -> int:
    if isinstance(value, bool):
        raise vol.Invalid("active connection slots must be an integer")
    try:
        slots = int(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("active connection slots must be an integer") from err
    if not 1 <= slots <= MAX_ACTIVE_CONNECTION_SLOTS:
        raise vol.Invalid(
            f"active connection slots must be between 1 and {MAX_ACTIVE_CONNECTION_SLOTS}"
        )
    return slots


def _validate_public_host(value: Any) -> str:
    host = str(value).strip().rstrip("/")
    if not host or _has_unsafe_cli_characters(host):
        raise vol.Invalid("public host is required")
    try:
        parsed = urlparse(host if "://" in host else f"//{host}")
    except ValueError as err:
        raise vol.Invalid("invalid public host") from err
    if parsed.scheme and parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise vol.Invalid("unsupported public host scheme")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise vol.Invalid("public host must not contain a path, query, or fragment")
    try:
        parsed.port
    except ValueError as err:
        raise vol.Invalid("invalid public host port") from err
    return host


def _validate_endpoint_path(value: Any) -> str:
    path = _clean_path(str(value))
    if (
        len(path) > 128
        or _has_unsafe_cli_characters(path)
        or "?" in path
        or "#" in path
    ):
        raise vol.Invalid("endpoint path is invalid")
    return path


def _validate_cli_name(value: Any) -> str:
    name = str(value).strip()
    if not _CLI_NAME_RE.fullmatch(name):
        raise vol.Invalid("profile names may contain only letters, digits, dot, dash, underscore")
    return name


def _validate_access_token(value: Any) -> str:
    token = str(value).strip()
    if len(token) > 512 or _has_unsafe_cli_characters(token):
        raise vol.Invalid("access token is invalid")
    return token


def _has_unsafe_cli_characters(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _listener_data_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_LISTEN_PORT,
                default=defaults[CONF_LISTEN_PORT],
            ): int,
            vol.Required(
                CONF_PUBLIC_HOST,
                default=defaults[CONF_PUBLIC_HOST],
            ): str,
            vol.Required(
                CONF_ENDPOINT_PATH,
                default=defaults[CONF_ENDPOINT_PATH],
            ): str,
            vol.Optional(
                CONF_ACCESS_TOKEN,
                default=defaults[CONF_ACCESS_TOKEN],
            ): str,
            vol.Required(
                CONF_TRANSPORT_PREFIX,
                default=defaults[CONF_TRANSPORT_PREFIX],
            ): str,
            vol.Required(
                CONF_ENABLE_RADIO_PROFILE,
                default=defaults[CONF_ENABLE_RADIO_PROFILE],
            ): bool,
            vol.Required(
                CONF_ENABLE_ACTIVE_BLE,
                default=defaults[CONF_ENABLE_ACTIVE_BLE],
            ): bool,
            vol.Required(
                CONF_ACTIVE_CONNECTION_SLOTS,
                default=defaults[CONF_ACTIVE_CONNECTION_SLOTS],
            ): int,
            vol.Required(
                CONF_RADIO_PROFILE,
                default=defaults[CONF_RADIO_PROFILE],
            ): str,
        }
    )


async def _async_test_bind_port(host: str, port: int) -> None:
    server = await asyncio.start_server(_close_probe_connection, host, port)
    server.close()
    await server.wait_closed()


def _close_probe_connection(reader, writer) -> None:
    writer.close()


def _endpoint_url(data: dict[str, Any]) -> str:
    host = data[CONF_PUBLIC_HOST].strip().rstrip("/")
    scheme = data[CONF_PUBLIC_SCHEME]
    path = _clean_path(data[CONF_ENDPOINT_PATH])
    if host.startswith("http://"):
        host = host.removeprefix("http://")
    if host.startswith("https://"):
        host = host.removeprefix("https://")
    if host.startswith("ws://"):
        host = host.removeprefix("ws://")
    if host.startswith("wss://"):
        host = host.removeprefix("wss://")
    # Detect whether the host part already includes a port.
    # Bracketed IPv6 like [::1] contains colons but no port; an IPv6
    # with port looks like [::1]:7443 — the ] is followed by :.
    host_part = host.split("/", 1)[0]
    if host_part.startswith("[") and "]" in host_part:
        has_port = host_part.rindex("]") < len(host_part) - 1
    else:
        has_port = ":" in host_part
    if not has_port:
        host = f"{host}:{data[CONF_LISTEN_PORT]}"
    return f"{scheme}://{host}{path}"


def _generate_cli(data: dict[str, Any]) -> str:
    uuids = _cached_uuid_list()
    return render_aruba_config(
        uuids=uuids,
        name_prefix=data[CONF_TRANSPORT_PREFIX],
        endpoint_url=_endpoint_url(data),
        token=data[CONF_ACCESS_TOKEN],
        radio_profile=data[CONF_RADIO_PROFILE] if data[CONF_ENABLE_RADIO_PROFILE] else None,
    )


def _generate_cleanup_cli(data: dict[str, Any]) -> str:
    uuids = _cached_uuid_list()
    return render_aruba_cleanup_config(
        name_prefix=data[CONF_TRANSPORT_PREFIX],
        profile_count=len(chunked(uuids, 10)),
        radio_profile=data[CONF_RADIO_PROFILE] if data[CONF_ENABLE_RADIO_PROFILE] else None,
    )


def _cli_profile_count() -> int:
    uuids = _cached_uuid_list()
    return len(chunked(uuids, 10))


def _cli_filter_count() -> int:
    return len(_cached_uuid_list())


def _build_cli_placeholders(data: dict[str, Any]) -> dict[str, str]:
    return {
        "endpoint_url": _endpoint_url(data),
        "profile_count": str(_cli_profile_count()),
        "filter_count": str(_cli_filter_count()),
        "cli_config": _generate_cli(data),
        "cleanup_cli_config": _generate_cleanup_cli(data),
    }


async def _async_build_cli_placeholders(hass, data: dict[str, Any]) -> dict[str, str]:
    executor = getattr(hass, "async_add_executor_job", None)
    if executor is None:
        return _build_cli_placeholders(data)
    return await executor(_build_cli_placeholders, data)


def _data_with_defaults(data: dict[str, Any]) -> dict[str, Any]:
    merged = {
        CONF_LISTEN_HOST: DEFAULT_LISTEN_HOST,
        CONF_LISTEN_PORT: DEFAULT_LISTEN_PORT,
        CONF_PUBLIC_SCHEME: DEFAULT_PUBLIC_SCHEME,
        CONF_PUBLIC_HOST: "",
        CONF_ENDPOINT_PATH: DEFAULT_ENDPOINT_PATH,
        CONF_ACCESS_TOKEN: "",
        CONF_TRANSPORT_PREFIX: DEFAULT_TRANSPORT_PREFIX,
        CONF_ENABLE_RADIO_PROFILE: True,
        CONF_ENABLE_ACTIVE_BLE: DEFAULT_ENABLE_ACTIVE_BLE,
        CONF_ACTIVE_CONNECTION_SLOTS: DEFAULT_ACTIVE_CONNECTION_SLOTS,
        CONF_RADIO_PROFILE: DEFAULT_RADIO_PROFILE,
        CONF_ENTRY_TYPE: ENTRY_TYPE_LISTENER,
        CONF_SETUP_COMPLETE: False,
    }
    merged.update(data)
    merged[CONF_LISTEN_PORT] = _validate_listen_port(merged[CONF_LISTEN_PORT])
    merged[CONF_ENDPOINT_PATH] = _validate_endpoint_path(merged[CONF_ENDPOINT_PATH])
    merged[CONF_ACTIVE_CONNECTION_SLOTS] = _validate_active_connection_slots(
        merged[CONF_ACTIVE_CONNECTION_SLOTS]
    )
    return merged


def _validate_listener_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize submitted data with validators kept outside the UI schema.

    Home Assistant must serialize config-flow schemas for the frontend. Plain
    Python validator functions aren't serializable by voluptuous-serialize.
    """
    validated = _data_with_defaults(data)
    validated[CONF_PUBLIC_HOST] = _validate_public_host(
        validated[CONF_PUBLIC_HOST]
    )
    validated[CONF_ENDPOINT_PATH] = _validate_endpoint_path(
        validated[CONF_ENDPOINT_PATH]
    )
    validated[CONF_ACCESS_TOKEN] = _validate_access_token(
        validated[CONF_ACCESS_TOKEN]
    )
    validated[CONF_TRANSPORT_PREFIX] = _validate_cli_name(
        validated[CONF_TRANSPORT_PREFIX]
    )
    validated[CONF_RADIO_PROFILE] = _validate_cli_name(
        validated[CONF_RADIO_PROFILE]
    )
    return validated


class ArubaBleProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]):
        if discovery_info.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AP_SOURCE:
            return self.async_abort(reason="unknown_discovery")

        source = str(discovery_info[CONF_AP_SOURCE])
        parent_entry_id = str(discovery_info[CONF_PARENT_ENTRY_ID])
        data = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_AP_SOURCE,
            CONF_AP_SOURCE: source,
            CONF_PARENT_ENTRY_ID: parent_entry_id,
        }
        await self.async_set_unique_id(f"{ENTRY_TYPE_AP_SOURCE}:{parent_entry_id}:{source}")
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(
            title=f"Aruba AP {source}",
            data=data,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            try:
                data = _validate_listener_data(dict(user_input))
            except vol.Invalid:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_listener_data_schema(
                        {**_data_with_defaults({}), **dict(user_input)}
                    ),
                    errors={"base": "invalid_input"},
                )
            data[CONF_LISTEN_HOST] = DEFAULT_LISTEN_HOST
            data[CONF_PUBLIC_SCHEME] = DEFAULT_PUBLIC_SCHEME
            data[CONF_ACCESS_TOKEN] = data[CONF_ACCESS_TOKEN].strip() or secrets.token_urlsafe(32)
            data[CONF_ENTRY_TYPE] = ENTRY_TYPE_LISTENER
            data[CONF_SETUP_COMPLETE] = True
            try:
                await _async_test_bind_port(data[CONF_LISTEN_HOST], data[CONF_LISTEN_PORT])
            except OSError:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_listener_data_schema(data),
                    errors={"base": "cannot_bind"},
                )
            self._data = data
            await self.async_set_unique_id(f"{data[CONF_LISTEN_HOST]}:{data[CONF_LISTEN_PORT]}")
            self._abort_if_unique_id_configured()
            return await self.async_step_cli()

        return self.async_show_form(
            step_id="user",
            data_schema=_listener_data_schema(
                _data_with_defaults(
                    {CONF_PUBLIC_HOST: _default_public_host(self.hass)}
                )
            ),
        )

    async def async_step_cli(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="Aruba BLE Proxy",
                data=self._data,
            )

        return self.async_show_form(
            step_id="cli",
            data_schema=vol.Schema({}),
            description_placeholders=await _async_build_cli_placeholders(
                self.hass,
                self._data,
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_AP_SOURCE:
            return ArubaBleProxyApSourceOptionsFlow()
        return ArubaBleProxyOptionsFlow()


class ArubaBleProxyOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            data = _data_with_defaults(dict(self.config_entry.data))
            data.update(dict(user_input))
            try:
                data = _validate_listener_data(data)
            except vol.Invalid:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_listener_data_schema(data),
                    errors={"base": "invalid_input"},
                )
            data[CONF_LISTEN_HOST] = DEFAULT_LISTEN_HOST
            data[CONF_PUBLIC_SCHEME] = DEFAULT_PUBLIC_SCHEME
            data[CONF_ENDPOINT_PATH] = _clean_path(str(data[CONF_ENDPOINT_PATH]))
            data[CONF_ACCESS_TOKEN] = (
                str(data[CONF_ACCESS_TOKEN]).strip() or secrets.token_urlsafe(32)
            )
            data[CONF_ENTRY_TYPE] = ENTRY_TYPE_LISTENER
            data[CONF_SETUP_COMPLETE] = True
            if data[CONF_LISTEN_PORT] != self.config_entry.data.get(CONF_LISTEN_PORT):
                try:
                    await _async_test_bind_port(
                        data[CONF_LISTEN_HOST],
                        data[CONF_LISTEN_PORT],
                    )
                except OSError:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_listener_data_schema(data),
                        errors={"base": "cannot_bind"},
                    )
            self._data = data
            return await self.async_step_cli()

        data = _data_with_defaults(dict(self.config_entry.data))
        return self.async_show_form(
            step_id="init",
            data_schema=_listener_data_schema(data),
        )

    async def async_step_cli(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="cli",
            data_schema=vol.Schema({}),
            description_placeholders=await _async_build_cli_placeholders(
                self.hass,
                self._data,
            ),
        )


class ArubaBleProxyApSourceOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_abort(reason="ap_source_options_not_supported")
