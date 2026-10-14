import subprocess
import unittest
from unittest.mock import Mock, patch

from govee_ble_ssh import GoveeBLESSHClient


class GoveeBLESSHClientTests(unittest.TestCase):
    @patch("govee_ble_ssh.time.sleep")
    def test_starts_dreamview_and_sends_fixed_palette_to_pi(self, sleep):
        platform = Mock()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        client = GoveeBLESSHClient(
            platform,
            host="homeassistant.local",
            username="tester",
            password="secret",
            host_key="SHA256:test",
            plink="plink",
            runner=runner,
        )

        result = client.set_music(
            "Dynamic", (0, 0, 255), sensitivity=87, auto_color=False
        )

        platform.set_dreamview.assert_called_once_with(True)
        sleep.assert_called_once_with(1.5)
        command = runner.call_args.args[0]
        self.assertEqual(command[0], "plink")
        self.assertIn("SHA256:test", command)
        self.assertEqual(
            command[-1],
            "python3 /config/govee-music/govee_ble.py "
            "--color '#0000FF' --sensitivity 87",
        )
        self.assertEqual(result["transport"], "pi-ble")

    @patch("govee_ble_ssh.time.sleep")
    def test_reuses_active_dreamview_for_color_changes(self, sleep):
        platform = Mock()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        client = GoveeBLESSHClient(
            platform,
            host="homeassistant.local",
            username="tester",
            password="secret",
            host_key="SHA256:test",
            plink="plink",
            runner=runner,
        )

        client.set_music("Dynamic", (255, 0, 0))
        client.set_music("Dynamic", (0, 255, 0))

        platform.set_dreamview.assert_called_once_with(True)
        self.assertEqual(runner.call_count, 2)

    def test_requires_a_pinned_host_key(self):
        with self.assertRaises(ValueError):
            GoveeBLESSHClient(Mock(), host="pi.local", username="u", password="p",
                              host_key="", plink="plink")


if __name__ == "__main__":
    unittest.main()
