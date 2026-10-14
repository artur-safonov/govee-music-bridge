"""Development transport: write the Music DreamView palette from a Windows PC through SSH.

The add-on talks to BlueZ directly (govee_ble_local.py) and never uses this. It exists for running
the daemon on a PC while the Bluetooth adapter sits on the Home Assistant host. It needs PuTTY's
plink, an SSH account on that host, and the host's key fingerprint, which is pinned on every call.
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess
import time
from collections.abc import Callable
from typing import Any

from govee_platform import GoveePlatformClient

DEFAULT_PLINK = pathlib.Path(r"C:\Program Files\PuTTY\plink.exe")
DEFAULT_REMOTE_SCRIPT = "/config/govee-music/govee_ble.py"


class GoveeBLESSHError(RuntimeError):
    """A Pi-side Bluetooth palette command failed."""


class GoveeBLESSHClient:
    """Power DreamView through the cloud, then set its beat palette over Pi BLE."""

    description = "Pi BLE Music DreamView (beat-aware fixed palette)"

    def __init__(
        self,
        platform: GoveePlatformClient,
        *,
        host: str,
        username: str,
        password: str,
        host_key: str,
        remote_script: str = DEFAULT_REMOTE_SCRIPT,
        plink: str | pathlib.Path = DEFAULT_PLINK,
        runner: Callable[..., Any] = subprocess.run,
        address: str | None = None,
    ):
        if not host or not username or not password:
            raise ValueError("HA SSH host, username, and password are required")
        if not host_key:
            raise ValueError("the SSH host key fingerprint is required (HA_SSH_HOST_KEY)")
        self.platform = platform
        # BLE MAC of the lamp this client drives; None keeps the Pi script's own default,
        # which is only correct when there is exactly one lamp.
        self.address = address
        self.host = host
        self.username = username
        self.password = password
        self.host_key = host_key
        self.remote_script = remote_script
        self.plink = str(plink)
        self._runner = runner
        self._active = False

    def deactivate(self) -> None:
        """Mark DreamView inactive after another command exits music mode."""
        self._active = False

    def stop(self) -> None:
        """Switch the DreamView session off in the cloud (see GoveeBLELocalClient.stop)."""
        self._active = False
        self.platform.set_dreamview(False)

    def set_music(
        self,
        mode: str | int,
        rgb: tuple[int, int, int] | list[int],
        *,
        sensitivity: int = 100,
        auto_color: bool = False,
    ) -> dict[str, Any]:
        if auto_color:
            raise ValueError("Pi BLE backend expects a fixed RGB palette")
        if len(rgb) != 3:
            raise ValueError("rgb must contain exactly three values")
        components = tuple(max(0, min(int(value), 255)) for value in rgb)
        sensitivity = max(0, min(int(sensitivity), 100))

        if not self._active:
            self.platform.set_dreamview(True)
            # The H6076 briefly rejects LE connections while entering DreamView.
            time.sleep(1.5)
            self._active = True

        color = "#" + "".join(f"{component:02X}" for component in components)
        remote_command = (
            f"python3 {shlex.quote(self.remote_script)} "
            f"--color {shlex.quote(color)} "
            f"--sensitivity {sensitivity}"
        )
        if self.address:
            remote_command += f" --address {shlex.quote(self.address)}"
        command = [
            self.plink,
            "-batch",
            "-ssh",
            "-hostkey",
            self.host_key,
            "-P",
            "22",
            "-l",
            self.username,
            "-pw",
            self.password,
            self.host,
            remote_command,
        ]
        try:
            result = self._runner(
                command,
                text=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GoveeBLESSHError(f"could not run Pi BLE command: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown SSH error").strip()
            raise GoveeBLESSHError(f"Pi BLE command failed: {detail}")
        return {
            "code": 200,
            "mode": mode,
            "rgb": components,
            "sensitivity": sensitivity,
            "transport": "pi-ble",
        }
