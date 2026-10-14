import json
import unittest
from unittest.mock import Mock, patch

import daemon
from daemon import Bridge, LampController
from lamps import Lamp


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def controller(name="Left lamp", entity_id="light.left_lamp", *, ip=None):
        platform = Mock()
        platform.set_music.return_value = {"code": 200}
        lamp = Lamp(name, entity_id, "AA:BB:CC:DD:EE:FF:00:11", ip=ip)
        controller = LampController(lamp, platform)
        return controller, platform

    async def test_platform_music_uses_remembered_colour_and_sensitivity(self):
        ws = FakeWebSocket()
        controller, platform = self.controller()
        controller.is_on = True
        controller.last_solid = (200, 10, 20)
        bridge = Bridge(ws, [controller])
        bridge.applied_mode = "Rhythm"
        bridge.sensitivity = 73

        await bridge.apply("Rhythm", "test")

        platform.set_music.assert_called_once_with(
            "Dynamic",
            (200, 10, 20),
            sensitivity=73,
            auto_color=False,
        )
        self.assertEqual(ws.sent[-1]["domain"], "input_boolean")
        self.assertEqual(ws.sent[-1]["service"], "turn_on")

    async def test_app_labels_map_to_platform_enum_names(self):
        ws = FakeWebSocket()
        controller, platform = self.controller()
        controller.is_on = True
        bridge = Bridge(ws, [controller])
        bridge.applied_mode = "Candy Crush"

        await bridge.apply("Candy Crush", "test")

        platform.set_music.assert_called_once_with(
            "CandyCrush",
            controller.last_solid,
            sensitivity=bridge.sensitivity,
            auto_color=False,
        )

    async def test_off_restores_solid_colour_and_clears_guard(self):
        ws = FakeWebSocket()
        controller, _ = self.controller(ip="192.0.2.1")
        controller.is_on = True
        controller.last_solid = (1, 2, 3)
        bridge = Bridge(ws, [controller])
        bridge.applied_mode = "Off"

        with patch("daemon.set_colour") as set_colour, patch(
                "daemon.COLOUR_REPEAT_GAP", 0):
            await bridge.apply("Off", "test")

        self.assertEqual(set_colour.call_count, 2)
        set_colour.assert_called_with((1, 2, 3), ip="192.0.2.1")
        self.assertEqual(ws.sent[-1]["service"], "turn_off")

    async def test_no_blink_shutdown_clears_group_guard(self):
        ws = FakeWebSocket()
        owner, _ = self.controller()
        owner.is_on = False
        bridge = Bridge(ws, [owner])
        bridge.applied_mode = "Rhythm Dynamic"
        bridge.desired_mode = "Rhythm Dynamic"
        bridge.mode = "Rhythm Dynamic"

        with patch("daemon.OWNER_OFF_WAIT", 0), patch("daemon.DREAMVIEW_SETTLE", 0):
            await bridge._off_via_dreamview("test shutdown")

        self.assertEqual(bridge.desired_mode, "Off")
        self.assertEqual(
            [(message["domain"], message["service"], message["service_data"])
             for message in ws.sent[-2:]],
            [
                ("input_select", "select_option", {
                    "entity_id": daemon.SELECT,
                    "option": "Off",
                }),
                ("input_boolean", "turn_off", {
                    "entity_id": daemon.GUARD,
                }),
            ],
        )

    async def test_power_intent_sync_is_skipped_when_not_configured(self):
        ws = FakeWebSocket()
        controller, _ = self.controller()
        bridge = Bridge(ws, [controller])

        with patch("daemon.POWER_INTENT", ""):
            await bridge.set_power_intent(False)
        self.assertEqual(ws.sent, [])

        with patch("daemon.POWER_INTENT", "input_boolean.lamps_power"):
            await bridge.set_power_intent(False)
        self.assertEqual(ws.sent[-1]["service_data"],
                         {"entity_id": "input_boolean.lamps_power"})


if __name__ == "__main__":
    unittest.main()
