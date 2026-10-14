"""Control the H6076 Music DreamView palette through BlueZ D-Bus.

This script runs on the Home Assistant host, where the lamp is reachable
through the host Bluetooth adapter. It shells out to ``gdbus`` (GLib) and has
no third-party Python dependencies.
"""
from __future__ import annotations

import argparse
import functools
import operator
import os
import re
import subprocess
import time
from collections.abc import Sequence

# The lamp's BLE MAC is the last six bytes of its Govee device id.
DEFAULT_ADDRESS = os.environ.get("GOVEE_BLE_ADDRESS", "")
COMMAND_CHARACTERISTIC = "service000e/char0013"

# Exact eight-colour palette reconstructed from the fixed-red iPhone capture.
CAPTURED_RED_PALETTE = (
    (0xFF, 0x47, 0x23),
    (0xFF, 0x3C, 0x1E),
    (0xFF, 0x32, 0x19),
    (0xFF, 0x28, 0x14),
    (0xFF, 0x1E, 0x0F),
    (0xFF, 0x14, 0x0A),
    (0xFF, 0x0A, 0x05),
    (0xFF, 0x00, 0x00),
)


def checked_frame(content: bytes) -> bytes:
    """Pad a Govee command to 19 bytes and append its XOR checksum."""
    if len(content) > 19:
        raise ValueError("Govee frame content cannot exceed 19 bytes")
    body = content.ljust(19, b"\x00")
    checksum = functools.reduce(operator.xor, body, 0)
    return body + bytes((checksum,))


def palette_frames(colors: Sequence[Sequence[int]]) -> tuple[bytes, bytes]:
    """Encode eight RGB values into the captured two-frame A3 palette format."""
    if len(colors) != 8:
        raise ValueError("DreamView palette must contain exactly eight colors")
    flattened = bytearray()
    for color in colors:
        if len(color) != 3:
            raise ValueError("each palette color must contain exactly three values")
        for component in color:
            component = int(component)
            if not 0 <= component <= 255:
                raise ValueError("RGB components must be between 0 and 255")
            flattened.append(component)

    payload = bytes((0x01, 0x02, 0x51, 0x08)) + bytes(flattened)
    return (
        checked_frame(bytes((0xA3, 0x00)) + payload[:17]),
        checked_frame(bytes((0xA3, 0xFF)) + payload[17:]),
    )


def fixed_music_frame(sensitivity: int = 99) -> bytes:
    sensitivity = max(0, min(int(sensitivity), 100))
    return checked_frame(bytes((0x33, 0x60, 0x02, 0x00, 0x00, sensitivity)))


APPLY_PALETTE_FRAME = checked_frame(bytes((0x33, 0x60, 0x08)))


def parse_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip()
    hex_match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value)
    if hex_match:
        packed = int(hex_match.group(1), 16)
        return (packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF)
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("color must be #RRGGBB or R,G,B")
    try:
        rgb = tuple(int(part.strip()) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("RGB components must be integers") from exc
    if any(component < 0 or component > 255 for component in rgb):
        raise argparse.ArgumentTypeError("RGB components must be between 0 and 255")
    return rgb  # type: ignore[return-value]


class BlueZGovee:
    def __init__(self, address: str = DEFAULT_ADDRESS):
        if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address or ""):
            raise ValueError("a BLE address such as AA:BB:CC:DD:EE:FF is required")
        suffix = address.upper().replace(":", "_")
        self.device_path = f"/org/bluez/hci0/dev_{suffix}"
        self.characteristic_path = (
            f"{self.device_path}/{COMMAND_CHARACTERISTIC}"
        )

    @staticmethod
    def _call(object_path: str, method: str, *arguments: str) -> str:
        command = [
            "gdbus",
            "call",
            "--system",
            "--dest",
            "org.bluez",
            "--object-path",
            object_path,
            "--method",
            method,
            *arguments,
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"{method} failed: {detail}")
        return result.stdout.strip()

    def connect(self, attempts: int = 4) -> None:
        """Connect with bounded retries for BlueZ's transient LE abort."""
        for attempt in range(attempts):
            try:
                self._call(self.device_path, "org.bluez.Device1.Connect")
                self.wait_services_resolved()
                return
            except RuntimeError as exc:
                detail = str(exc)
                if "Already Connected" in detail:
                    self.wait_services_resolved()
                    return
                transient = (
                    "le-connection-abort-by-local" in detail
                    or "org.bluez.Error.Failed" in detail
                )
                if not transient or attempt == attempts - 1:
                    raise
                self.disconnect()
                time.sleep(0.6 * (attempt + 1))

    def wait_services_resolved(self, timeout: float = 10.0) -> None:
        """Block until BlueZ has enumerated the GATT tree.

        Connect() returns before service discovery finishes, so writing immediately fails with
        `UnknownObject: Method "WriteValue" ... doesn't exist` - the characteristic path does not
        exist yet. Previously this only worked because a second attempt reused the resolved
        connection.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = self._call(
                    self.device_path,
                    "org.freedesktop.DBus.Properties.Get",
                    "org.bluez.Device1",
                    "ServicesResolved",
                )
            except RuntimeError:
                reply = ""
            if "true" in reply.lower():
                return
            time.sleep(0.25)
        raise RuntimeError(
            f"BlueZ did not resolve services for {self.device_path} within {timeout:.0f}s"
        )

    def disconnect(self) -> None:
        try:
            self._call(self.device_path, "org.bluez.Device1.Disconnect")
        except RuntimeError:
            pass

    def write(self, frame: bytes) -> None:
        if len(frame) != 20:
            raise ValueError("Govee BLE commands must be exactly 20 bytes")
        if functools.reduce(operator.xor, frame, 0) != 0:
            raise ValueError("Govee BLE command has an invalid XOR checksum")
        values = ", ".join(
            [f"byte 0x{frame[0]:02x}", *(f"0x{byte:02x}" for byte in frame[1:])]
        )
        self._call(
            self.characteristic_path,
            "org.bluez.GattCharacteristic1.WriteValue",
            f"[{values}]",
            '{"type": <"command">}',
        )

    def set_music_palette(
        self, colors: Sequence[Sequence[int]], *, sensitivity: int = 99
    ) -> None:
        frames = (*palette_frames(colors), APPLY_PALETTE_FRAME, fixed_music_frame(sensitivity))
        self.connect()
        try:
            for frame in frames:
                self.write(frame)
                time.sleep(0.08)
            time.sleep(0.5)
        finally:
            self.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address",
        default=DEFAULT_ADDRESS or None,
        required=not DEFAULT_ADDRESS,
        help="lamp BLE MAC (the last six bytes of its Govee device id); "
        "defaults to GOVEE_BLE_ADDRESS",
    )
    palette = parser.add_mutually_exclusive_group(required=True)
    palette.add_argument(
        "--captured-red",
        action="store_true",
        help="replay the exact fixed-red palette from the iPhone capture",
    )
    palette.add_argument(
        "--color",
        type=parse_rgb,
        help="lock all eight DreamView palette entries to #RRGGBB or R,G,B",
    )
    parser.add_argument("--sensitivity", type=int, default=99)
    args = parser.parse_args()

    colors = (
        CAPTURED_RED_PALETTE
        if args.captured_red
        else (args.color,) * 8
    )
    BlueZGovee(args.address).set_music_palette(
        colors, sensitivity=args.sensitivity
    )
    label = "captured red" if args.captured_red else args.color
    print(f"Music DreamView palette sent: {label}")


if __name__ == "__main__":
    main()
