import json
import unittest
from unittest.mock import Mock

from govee_platform import (
    DREAMVIEW_INSTANCE,
    DREAMVIEW_TYPE,
    GoveeAPIError,
    GoveePlatformClient,
    MUSIC_INSTANCE,
    MUSIC_TYPE,
    rgb_value,
)


DEVICE_RESPONSE = {
    "code": 200,
    "message": "success",
    "data": [
        {
            "sku": "H6076",
            "device": "AA:BB:CC:DD",
            "deviceName": "Floor Lamp",
            "capabilities": [
                {
                    "type": MUSIC_TYPE,
                    "instance": MUSIC_INSTANCE,
                    "parameters": {
                        "dataType": "STRUCT",
                        "fields": [
                            {
                                "fieldName": "musicMode",
                                "dataType": "ENUM",
                                "options": [
                                    {"name": "Rhythm", "value": 42},
                                    {"name": "Bounce", "value": 7},
                                ],
                            },
                            {
                                "fieldName": "sensitivity",
                                "dataType": "INTEGER",
                                "range": {"min": 0, "max": 100},
                            },
                            {
                                "fieldName": "autoColor",
                                "dataType": "ENUM",
                                "options": [
                                    {"name": "on", "value": 1},
                                    {"name": "off", "value": 0},
                                ],
                            },
                            {
                                "fieldName": "rgb",
                                "dataType": "INTEGER",
                                "range": {"min": 0, "max": 16777215},
                            },
                        ],
                    },
                },
                {
                    "type": DREAMVIEW_TYPE,
                    "instance": DREAMVIEW_INSTANCE,
                    "parameters": {"dataType": "ENUM"},
                },
            ],
        }
    ],
}


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.value).encode()


class PlatformClientTests(unittest.TestCase):
    def test_rgb_value_clamps_components(self):
        self.assertEqual(rgb_value((300, -1, 16)), 0xFF0010)

    def test_discovers_device_specific_mode_values(self):
        opener = Mock(return_value=FakeResponse(DEVICE_RESPONSE))
        client = GoveePlatformClient("key", opener=opener)
        device, music = client.discover()
        self.assertEqual(device["device"], "AA:BB:CC:DD")
        self.assertEqual(music.mode_value("rhythm"), 42)
        self.assertEqual(music.mode_value("Bounce"), 7)

    def test_sends_locked_colour_music_struct(self):
        opener = Mock(
            side_effect=[
                FakeResponse(DEVICE_RESPONSE),
                FakeResponse({"code": 200, "message": "success", "data": {}}),
            ]
        )
        client = GoveePlatformClient("key", opener=opener)
        client.set_music("Rhythm", (255, 0, 16), sensitivity=110)

        request = opener.call_args_list[1].args[0]
        sent = json.loads(request.data)
        self.assertEqual(request.full_url.endswith("/router/api/v1/device/control"), True)
        self.assertEqual(
            sent["payload"]["capability"],
            {
                "type": MUSIC_TYPE,
                "instance": MUSIC_INSTANCE,
                "value": {
                    "musicMode": 42,
                    "sensitivity": 100,
                    "autoColor": 0,
                    "rgb": 0xFF0010,
                },
            },
        )

    def test_rejects_device_without_music_capability(self):
        response = json.loads(json.dumps(DEVICE_RESPONSE))
        response["data"][0]["capabilities"] = []
        client = GoveePlatformClient(
            "key", opener=Mock(return_value=FakeResponse(response))
        )
        with self.assertRaisesRegex(GoveeAPIError, "does not advertise"):
            client.discover()

    def test_toggles_saved_dreamview_configuration(self):
        opener = Mock(
            side_effect=[
                FakeResponse(DEVICE_RESPONSE),
                FakeResponse({"code": 200, "message": "success", "data": {}}),
            ]
        )
        client = GoveePlatformClient("key", opener=opener)
        client.set_dreamview(True)

        request = opener.call_args_list[1].args[0]
        sent = json.loads(request.data)
        self.assertEqual(
            sent["payload"]["capability"],
            {
                "type": DREAMVIEW_TYPE,
                "instance": DREAMVIEW_INSTANCE,
                "value": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
