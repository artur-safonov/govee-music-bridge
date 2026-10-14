import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings

LAMP = {"name": "Left", "entity_id": "light.left_lamp",
        "device_id": "aa:bb:cc:dd:ee:ff:00:11"}


class SettingsTests(unittest.TestCase):
    def test_defaults_when_only_lamps_are_given(self):
        parsed = settings.parse({"lamps": [LAMP]})
        self.assertEqual(parsed.lamps[0].device_id, "AA:BB:CC:DD:EE:FF:00:11")
        self.assertEqual(parsed.mode_select, "input_select.govee_music_mode")
        self.assertEqual(parsed.music_guard, "input_boolean.govee_music_active")
        self.assertEqual(parsed.sensitivity_entity, "input_number.govee_music_sensitivity")
        for optional in settings.OPTIONAL_HELPERS:
            self.assertEqual(getattr(parsed, optional), "")

    def test_blank_helper_falls_back_to_default(self):
        parsed = settings.parse({"lamps": [LAMP], "mode_select": "  "})
        self.assertEqual(parsed.mode_select, settings.DEFAULT_HELPERS["mode_select"])

    def test_scripts_accept_entity_or_bare_name(self):
        parsed = settings.parse({
            "lamps": [LAMP],
            "toggle_script": "script.lamps_toggle",
            "group_color_script": "set_group_color",
        })
        self.assertEqual(parsed.toggle_script, "lamps_toggle")
        self.assertEqual(parsed.group_color_script, "set_group_color")

    def test_rejects_invalid_lamps_and_helpers(self):
        bad = [
            {"lamps": {"entity_id": "light.x"}},
            {"lamps": [{**LAMP, "entity_id": "switch.left"}]},
            {"lamps": [{**LAMP, "device_id": "AA:BB:CC:DD:EE:FF"}]},
            {"lamps": [LAMP, LAMP]},
            {"lamps": [LAMP], "music_guard": "input_select.wrong_domain"},
            {"lamps": [LAMP], "power_intent": "not an entity"},
            {"lamps": [LAMP], "toggle_script": "script.Bad Name"},
        ]
        for options in bad:
            with self.subTest(options=options):
                with self.assertRaises(settings.SettingsError):
                    settings.parse(options)

    def test_no_lamps_is_reported_before_running(self):
        with self.assertRaises(settings.SettingsError):
            settings.parse({}).require_lamps()

    def test_loads_the_file_named_by_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps({"lamps": [LAMP], "power_intent": "input_boolean.p"}),
                            encoding="utf-8")
            with patch.dict(os.environ, {"GOVEE_MUSIC_OPTIONS": str(path)}):
                loaded = settings.load()
        self.assertEqual(loaded.lamps[0].entity_id, "light.left_lamp")
        self.assertEqual(loaded.power_intent, "input_boolean.p")
        self.assertEqual(loaded.source, str(path))


if __name__ == "__main__":
    unittest.main()
