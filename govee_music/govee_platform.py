"""Govee Platform API client for music mode and DreamView control.

The H6076's local ``ptReal`` music selector can choose a sub-mode, but the full
music configuration (sensitivity, auto-colour and RGB palette) is exposed by
Govee's Platform API as ``devices.capabilities.music_setting/musicMode``.

The numeric value for a named music mode is device-specific.  This module
therefore discovers the H6076 and its capability schema before sending a
control request; it never guesses or hard-codes the enum values.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://openapi.api.govee.com"
DEFAULT_SKU = "H6076"
MUSIC_TYPE = "devices.capabilities.music_setting"
MUSIC_INSTANCE = "musicMode"
DREAMVIEW_TYPE = "devices.capabilities.toggle"
DREAMVIEW_INSTANCE = "dreamViewToggle"


class GoveeAPIError(RuntimeError):
    """A Govee API request failed or returned an unusable response."""


@dataclass(frozen=True)
class MusicCapability:
    modes: dict[str, int]
    sensitivity_min: int = 0
    sensitivity_max: int = 100
    supports_auto_color: bool = False
    supports_rgb: bool = False

    def mode_value(self, name_or_value: str | int) -> int:
        if isinstance(name_or_value, int):
            if name_or_value not in self.modes.values():
                raise ValueError(
                    f"music mode value {name_or_value} is not advertised by this device"
                )
            return name_or_value
        wanted = name_or_value.casefold()
        for name, value in self.modes.items():
            if name.casefold() == wanted:
                return value
        raise ValueError(
            f"unsupported music mode {name_or_value!r}; "
            f"available: {', '.join(self.modes)}"
        )


def rgb_value(rgb: tuple[int, int, int] | list[int]) -> int:
    if len(rgb) != 3:
        raise ValueError("rgb must contain exactly three values")
    r, g, b = (max(0, min(int(component), 255)) for component in rgb)
    return (r << 16) | (g << 8) | b


class GoveePlatformClient:
    def __init__(
        self,
        api_key: str,
        *,
        sku: str = DEFAULT_SKU,
        device_id: str | None = None,
        timeout: float = 15.0,
        opener: Callable[..., Any] = urlopen,
    ):
        if not api_key:
            raise ValueError("Govee API key is required")
        self.api_key = api_key
        self.sku = sku
        self.device_id = device_id
        self.timeout = timeout
        self._opener = opener
        self._device: dict[str, Any] | None = None
        self._music: MusicCapability | None = None

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{API_BASE}{path}",
            data=payload,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Govee-API-Key": self.api_key,
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GoveeAPIError(f"Govee API HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise GoveeAPIError(f"Govee API request failed: {exc}") from exc
        if result.get("code") != 200:
            # Govee puts the human-readable reason under "msg" on the control endpoint and
            # "message" elsewhere, and repeats it as capability.state.errorMsg. Reading only
            # "message" produced "returned 400: None" and hid "The device does not has
            # DreamView", which callers need in order to fall back.
            state = (result.get("capability") or {}).get("state") or {}
            detail = (
                result.get("msg")
                or result.get("message")
                or state.get("errorMsg")
                or "no detail"
            )
            raise GoveeAPIError(f"Govee API returned {result.get('code')}: {detail}")
        return result

    def devices(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/router/api/v1/user/devices")
        devices = result.get("data")
        if not isinstance(devices, list):
            raise GoveeAPIError("Govee device response did not contain a data list")
        return devices

    def discover(self) -> tuple[dict[str, Any], MusicCapability]:
        matches = [
            device
            for device in self.devices()
            if str(device.get("sku", "")).casefold() == self.sku.casefold()
            and (
                not self.device_id
                or str(device.get("device", "")).casefold()
                == self.device_id.casefold()
            )
        ]
        if not matches:
            target = f"{self.sku}/{self.device_id}" if self.device_id else self.sku
            raise GoveeAPIError(f"device {target} was not returned by Govee")
        if len(matches) > 1 and not self.device_id:
            ids = ", ".join(str(item.get("device")) for item in matches)
            raise GoveeAPIError(
                f"multiple {self.sku} devices found ({ids}); set GOVEE_DEVICE_ID"
            )
        device = matches[0]
        raw_capability = next(
            (
                capability
                for capability in device.get("capabilities", [])
                if capability.get("type") == MUSIC_TYPE
                and capability.get("instance") == MUSIC_INSTANCE
            ),
            None,
        )
        if raw_capability is None:
            raise GoveeAPIError(
                f"{self.sku} does not advertise {MUSIC_TYPE}/{MUSIC_INSTANCE}"
            )

        fields = {
            field.get("fieldName"): field
            for field in raw_capability.get("parameters", {}).get("fields", [])
        }
        mode_field = fields.get("musicMode", {})
        modes = {
            str(option["name"]): int(option["value"])
            for option in mode_field.get("options", [])
            if "name" in option and "value" in option
        }
        if not modes:
            raise GoveeAPIError("musicMode capability has no advertised mode options")
        sensitivity_range = fields.get("sensitivity", {}).get("range", {})
        music = MusicCapability(
            modes=modes,
            sensitivity_min=int(sensitivity_range.get("min", 0)),
            sensitivity_max=int(sensitivity_range.get("max", 100)),
            supports_auto_color="autoColor" in fields,
            supports_rgb="rgb" in fields,
        )
        self._device = device
        self._music = music
        return device, music

    def set_music(
        self,
        mode: str | int,
        rgb: tuple[int, int, int] | list[int],
        *,
        sensitivity: int = 100,
        auto_color: bool = False,
    ) -> dict[str, Any]:
        if self._device is None or self._music is None:
            self.discover()
        assert self._device is not None and self._music is not None

        sensitivity = max(
            self._music.sensitivity_min,
            min(int(sensitivity), self._music.sensitivity_max),
        )
        value: dict[str, Any] = {
            "musicMode": self._music.mode_value(mode),
            "sensitivity": sensitivity,
        }
        if self._music.supports_auto_color:
            value["autoColor"] = 1 if auto_color else 0
        elif not auto_color:
            raise GoveeAPIError("device does not advertise fixed/auto colour selection")
        if self._music.supports_rgb:
            value["rgb"] = rgb_value(rgb)
        elif not auto_color:
            raise GoveeAPIError("device does not advertise an RGB music palette")

        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {
                "sku": self._device["sku"],
                "device": self._device["device"],
                "capability": {
                    "type": MUSIC_TYPE,
                    "instance": MUSIC_INSTANCE,
                    "value": value,
                },
            },
        }
        return self._request("POST", "/router/api/v1/device/control", body)

    def set_dreamview(self, enabled: bool) -> dict[str, Any]:
        """Enable or disable the device's saved DreamView configuration."""
        if self._device is None:
            self.discover()
        assert self._device is not None

        supports_dreamview = any(
            capability.get("type") == DREAMVIEW_TYPE
            and capability.get("instance") == DREAMVIEW_INSTANCE
            for capability in self._device.get("capabilities", [])
        )
        if not supports_dreamview:
            raise GoveeAPIError(
                f"{self.sku} does not advertise "
                f"{DREAMVIEW_TYPE}/{DREAMVIEW_INSTANCE}"
            )

        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {
                "sku": self._device["sku"],
                "device": self._device["device"],
                "capability": {
                    "type": DREAMVIEW_TYPE,
                    "instance": DREAMVIEW_INSTANCE,
                    "value": 1 if enabled else 0,
                },
            },
        }
        return self._request("POST", "/router/api/v1/device/control", body)


def _client_from_env() -> GoveePlatformClient:
    config = dict(os.environ)
    env = pathlib.Path(__file__).resolve().parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                config.setdefault(name.strip(), value.strip())
    return GoveePlatformClient(
        config.get("GOVEE_API_KEY", ""),
        sku=config.get("GOVEE_SKU", DEFAULT_SKU),
        device_id=config.get("GOVEE_DEVICE_ID") or None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="list every device on the account with its device id")
    sub.add_parser("discover", help="show this device's advertised music modes")
    music = sub.add_parser("music", help="activate a music mode with a chosen colour")
    music.add_argument("mode")
    music.add_argument("red", type=int)
    music.add_argument("green", type=int)
    music.add_argument("blue", type=int)
    music.add_argument("--sensitivity", type=int, default=100)
    music.add_argument("--auto-color", action="store_true")
    dreamview = sub.add_parser(
        "dreamview", help="enable or disable the saved DreamView configuration"
    )
    dreamview.add_argument("state", choices=("on", "off"))
    args = parser.parse_args()

    client = _client_from_env()
    if args.command == "devices":
        print(json.dumps([
            {"sku": device.get("sku"), "device": device.get("device"),
             "deviceName": device.get("deviceName")}
            for device in client.devices()
        ], indent=2))
    elif args.command == "discover":
        device, capability = client.discover()
        print(
            json.dumps(
                {
                    "sku": device.get("sku"),
                    "device": device.get("device"),
                    "deviceName": device.get("deviceName"),
                    "modes": capability.modes,
                    "supportsAutoColor": capability.supports_auto_color,
                    "supportsRgb": capability.supports_rgb,
                },
                indent=2,
            )
        )
    elif args.command == "music":
        result = client.set_music(
            args.mode,
            (args.red, args.green, args.blue),
            sensitivity=args.sensitivity,
            auto_color=args.auto_color,
        )
        print(json.dumps(result, indent=2))
    else:
        result = client.set_dreamview(args.state == "on")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
