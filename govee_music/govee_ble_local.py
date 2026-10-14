"""In-process BLE Music DreamView client, for when the daemon runs ON the Home Assistant Pi.

`govee_ble_ssh.GoveeBLESSHClient` exists only because the daemon used to run on a Windows desktop and
had to reach the Pi's Bluetooth adapter over SSH. Running on the Pi itself, that hop is pure overhead
and an extra failure mode (plink, host keys, stored passwords), so this talks to BlueZ directly.

Same interface as the SSH client, so daemon.py can use either:
    set_music(mode, rgb, sensitivity=..., auto_color=False)
    deactivate()
    description
"""
from __future__ import annotations

import time
from typing import Any

from govee_ble import BlueZGovee
from govee_platform import GoveePlatformClient


class GoveeBLELocalError(RuntimeError):
    """A local Bluetooth palette command failed."""


class GoveeBLELocalClient:
    """Power DreamView through the cloud, then set its beat palette over local BLE."""

    description = "local BLE Music DreamView (beat-aware fixed palette)"

    def __init__(
        self,
        platform: GoveePlatformClient,
        *,
        address: str,
        settle: float = 1.5,
    ):
        if not address:
            raise ValueError("a BLE address is required")
        self.platform = platform
        self.address = address
        self.settle = settle
        self._active = False

    def deactivate(self) -> None:
        """Mark DreamView inactive after another command exits music mode."""
        self._active = False

    def stop(self) -> None:
        """Actually switch the DreamView session off in the cloud.

        Necessary, not cosmetic: a session left on re-asserts itself and powers the lamps back on
        roughly half a minute after they are switched off. HA records that as a state change with no
        service and no user context, because the device did it on its own.
        """
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
            raise ValueError("the BLE backend expects a fixed RGB palette")
        if len(rgb) != 3:
            raise ValueError("rgb must contain exactly three values")
        components = tuple(max(0, min(int(value), 255)) for value in rgb)
        sensitivity = max(0, min(int(sensitivity), 100))

        if not self._active:
            self.platform.set_dreamview(True)
            # The H6076 briefly rejects LE connections while entering DreamView.
            time.sleep(self.settle)
            self._active = True

        try:
            BlueZGovee(self.address).set_music_palette(
                (components,) * 8, sensitivity=sensitivity
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise GoveeBLELocalError(f"local BLE palette write failed: {exc}") from exc

        return {
            "code": 200,
            "mode": mode,
            "rgb": components,
            "sensitivity": sensitivity,
            "transport": "local-ble",
        }
