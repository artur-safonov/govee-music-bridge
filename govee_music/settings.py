"""Installation settings: which lamps to drive and which Home Assistant helpers to use.

The same JSON shape is read in both places this bridge runs:

* as a Home Assistant add-on, from `/data/options.json`, written by Supervisor from the add-on's
  Configuration tab;
* on a workstation, from the file named by `GOVEE_MUSIC_OPTIONS`, or `options.json` beside this
  module. That file is ignored by Git because it describes one particular home.

Example::

    {
      "lamps": [
        {"name": "Left lamp", "entity_id": "light.left_lamp",
         "device_id": "AA:BB:CC:DD:EE:FF:00:11"}
      ],
      "mode_select": "input_select.govee_music_mode",
      "music_guard": "input_boolean.govee_music_active",
      "sensitivity_entity": "input_number.govee_music_sensitivity"
    }

Only `lamps` is required. The three helpers above have defaults; the remaining integrations
(group_color_script, group_light, toggle_script, power_intent) are optional and switched off
when left empty.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

ADDON_OPTIONS = pathlib.Path("/data/options.json")
LOCAL_OPTIONS = pathlib.Path(__file__).resolve().parent / "options.json"

DEVICE_ID = re.compile(r"^([0-9A-F]{2}:){7}[0-9A-F]{2}$")
ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

DEFAULT_HELPERS = {
    "mode_select": "input_select.govee_music_mode",
    "music_guard": "input_boolean.govee_music_active",
    "sensitivity_entity": "input_number.govee_music_sensitivity",
}
OPTIONAL_HELPERS = ("group_color_script", "group_light", "toggle_script", "power_intent")
SCRIPT_HELPERS = ("group_color_script", "toggle_script")
HELPER_DOMAINS = {
    "mode_select": "input_select",
    "music_guard": "input_boolean",
    "sensitivity_entity": "input_number",
    "group_light": "light",
    "power_intent": "input_boolean",
}


class SettingsError(ValueError):
    """The options file is missing something the bridge cannot run without."""


@dataclass(frozen=True)
class LampSettings:
    name: str
    entity_id: str
    device_id: str


@dataclass(frozen=True)
class Settings:
    lamps: tuple[LampSettings, ...] = ()
    mode_select: str = DEFAULT_HELPERS["mode_select"]
    music_guard: str = DEFAULT_HELPERS["music_guard"]
    sensitivity_entity: str = DEFAULT_HELPERS["sensitivity_entity"]
    group_color_script: str = ""
    group_light: str = ""
    toggle_script: str = ""
    power_intent: str = ""
    source: str = field(default="defaults", compare=False)

    def require_lamps(self) -> None:
        if not self.lamps:
            raise SettingsError(
                f"no lamps configured ({self.source}). Add each lamp's name, Home Assistant "
                "entity_id and Govee device_id to the 'lamps' option."
            )


def _text(options: dict[str, Any], key: str, default: str = "") -> str:
    value = options.get(key)
    return default if value is None else str(value).strip()


def parse(options: dict[str, Any], source: str = "options") -> Settings:
    """Validate one options mapping. Raises SettingsError with a message meant for a person."""
    raw_lamps = options.get("lamps") or []
    if not isinstance(raw_lamps, list):
        raise SettingsError("'lamps' must be a list")
    lamps = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_lamps, 1):
        if not isinstance(raw, dict):
            raise SettingsError(f"lamp {index} must be an object")
        entity_id = _text(raw, "entity_id")
        device_id = _text(raw, "device_id").upper()
        name = _text(raw, "name") or entity_id
        if not entity_id.startswith("light.") or not ENTITY_ID.match(entity_id):
            raise SettingsError(f"lamp {index}: entity_id must look like light.my_lamp")
        if not DEVICE_ID.match(device_id):
            raise SettingsError(
                f"lamp {index}: device_id must be the 8-byte Govee id, "
                "for example AA:BB:CC:DD:EE:FF:00:11"
            )
        if entity_id in seen:
            raise SettingsError(f"lamp {index}: {entity_id} is listed twice")
        seen.add(entity_id)
        lamps.append(LampSettings(name, entity_id, device_id))

    helpers = {key: _text(options, key) or default for key, default in DEFAULT_HELPERS.items()}
    helpers.update({key: _text(options, key) for key in OPTIONAL_HELPERS})
    for key in SCRIPT_HELPERS:
        # A script may be given as script.name or as the bare name used in its service call.
        name = helpers[key].removeprefix("script.")
        if name and not re.fullmatch(r"[a-z0-9_]+", name):
            raise SettingsError(f"'{key}' must be a script name, got {helpers[key]!r}")
        helpers[key] = name
    for key, domain in HELPER_DOMAINS.items():
        value = helpers[key]
        if value and not (ENTITY_ID.match(value) and value.startswith(domain + ".")):
            raise SettingsError(f"'{key}' must be a {domain}.* entity id, got {value!r}")
    return Settings(lamps=tuple(lamps), source=source, **helpers)


def options_path() -> pathlib.Path | None:
    configured = os.environ.get("GOVEE_MUSIC_OPTIONS")
    if configured:
        return pathlib.Path(configured)
    for candidate in (ADDON_OPTIONS, LOCAL_OPTIONS):
        if candidate.is_file():
            return candidate
    return None


def load() -> Settings:
    """Read the options file, or return defaults with no lamps when there is none."""
    path = options_path()
    if path is None:
        return Settings()
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not isinstance(options, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    return parse(options, str(path))
