"""Govee LAN API helpers for the H6076 floor lamp.

The LAN API documents only turn / brightness / colorwc / devStatus, but it also carries `ptReal`,
a passthrough that tunnels raw BLE protocol packets (base64, UDP 4003). Home Assistant's
govee_light_local uses it for scenes (0x33 0x05 0x04) and segments (0x33 0x05 0x15).

Music sub-mode selection was derived empirically against this lamp on 2026-07-28:

    33 05 0F <sub-mode index>  + XOR checksum

This reliably selects music modes that still use auto-colour. It does not encode the app's complete
fixed-colour music configuration. Exact multi-frame commands captured from a Govee Tap-to-Run
action can be replayed with :func:`send_ptreal_frames`.

The lamp reacts using its own built-in microphone, so this is one-shot: no audio needs streaming
to it, and the mode persists until something sends it an explicit colour or power command.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time

# Default target for the command-line helpers only. The daemon always passes each lamp's discovered
# address explicitly (see lamps.resolve_ips).
LAMP_IP = os.environ.get("GOVEE_LAMP_IP", "")
CMD_PORT = 4003

MUSIC_SUB = 0x0F
SEL_SET = 0x00
FLAG_USE_MY_COLOUR = 0x01
FLAG_AUTO_COLOUR = 0x00

# Music sub-modes. `33 05 0F <index>` selects one; index matches the order in the Govee app.
# Verified by sight: 0x01 Rhythm, 0x02 Bounce, 0x04 Strike, 0x06 Skittles, 0x08 Candy Crush.
#
# Works only while the mode is on AUTO-COLOUR. Once a mode is given a fixed colour in the app, the
# lamp refuses this short frame and wants the full config; govee_platform.py sends that configuration
# through the official API.
MUSIC_SUBMODES: dict[str, int] = {
    "Energic": 0x00,
    "Rhythm": 0x01,
    "Bounce": 0x02,
    "Hopping": 0x03,
    "Strike": 0x04,
    "Vibrate": 0x05,
    "Skittles": 0x06,
    "Torch": 0x07,
    "Candy Crush": 0x08,
    "Blend": 0x09,
    "Luminous": 0x0A,
    "Separation": 0x0B,
}


def set_music_submode(name_or_index, ip: str = LAMP_IP) -> bytes:
    """Select a music sub-mode: 33 05 0F <index>. Beat-reactive via the lamp's own mic."""
    idx = (MUSIC_SUBMODES[name_or_index] if isinstance(name_or_index, str)
           else int(name_or_index))
    return send_ptreal((0x05, MUSIC_SUB, idx), ip=ip)


def frame(*payload: int) -> bytes:
    """Build a 20-byte Govee packet: 0x33 header, zero padding, XOR checksum."""
    body = bytes([0x33, *payload])
    if len(body) > 19:
        raise ValueError(f"payload too long: {len(body)} bytes")
    body = body.ljust(19, b"\x00")
    ck = 0
    for b in body:
        ck ^= b
    return body + bytes([ck])


def send_ptreal(payload: tuple[int, ...], ip: str = LAMP_IP) -> bytes:
    pkt = frame(*payload)
    send_ptreal_frames([base64.b64encode(pkt).decode("ascii")], ip=ip)
    return pkt


def validate_ptreal_frame(encoded: str) -> bytes:
    """Decode and validate one base64-encoded 20-byte Govee protocol frame."""
    try:
        packet = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("ptReal frame is not valid base64") from exc
    if len(packet) != 20:
        raise ValueError(f"ptReal frame must decode to 20 bytes, got {len(packet)}")
    checksum = 0
    for value in packet:
        checksum ^= value
    if checksum:
        raise ValueError("ptReal frame has an invalid XOR checksum")
    return packet


def _send(message: bytes, ip: str) -> None:
    if not ip:
        raise ValueError("no lamp IP: pass ip= or set GOVEE_LAMP_IP")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(message, (ip, CMD_PORT))
    finally:
        s.close()


def send_ptreal_frames(frames: list[str], ip: str = LAMP_IP) -> list[bytes]:
    """Replay an exact array of base64 ``ptReal`` frames as one LAN command."""
    if not frames:
        raise ValueError("at least one ptReal frame is required")
    packets = [validate_ptreal_frame(encoded) for encoded in frames]
    msg = json.dumps(
        {"msg": {"cmd": "ptReal", "data": {"command": frames}}},
        separators=(",", ":"),
    ).encode()
    _send(msg, ip)
    return packets


def music_payload(rgb, sensitivity: int = 100, auto_colour: bool = False) -> tuple[int, ...]:
    """Return the historical single-frame experiment; not a fixed-colour protocol."""
    r, g, b = (max(0, min(int(c), 255)) for c in rgb)
    sens = max(0, min(int(sensitivity), 100))
    flag = FLAG_AUTO_COLOUR if auto_colour else FLAG_USE_MY_COLOUR
    return (0x05, MUSIC_SUB, SEL_SET, sens, flag, r, g, b)


ARM_SETTLE = 1.5


def set_music(rgb, sensitivity: int = 100, auto_colour: bool = False,
              ip: str = LAMP_IP, settle: float = ARM_SETTLE) -> bytes:
    """Activate the legacy auto-colour frame.

    The former two-packet "arm then lock" experiment did not reliably lock RGB and is intentionally
    rejected now. Capture and replay a Govee Tap-to-Run action for a fixed palette.
    """
    if not auto_colour:
        raise NotImplementedError(
            "fixed-colour music needs captured Tap-to-Run frames"
        )
    arm = music_payload(rgb, sensitivity, auto_colour=True)
    send_ptreal(arm, ip=ip)
    return frame(*arm)


def set_power(on: bool, ip: str = LAMP_IP) -> None:
    """Documented `turn` command, straight to the lamp over UDP.

    A second, independent way to switch a lamp off: Home Assistant's own turn_off goes through the
    same LAN API, but a lamp acting as a DreamView follower has been seen to ignore it, so it is worth
    pushing the command directly as well when a lamp refuses to go off.
    """
    msg = json.dumps({"msg": {"cmd": "turn", "data": {"value": 1 if on else 0}}},
                     separators=(",", ":")).encode()
    _send(msg, ip)


def set_colour(rgb, ip: str = LAMP_IP) -> None:
    """Documented colorwc command. Also the way to LEAVE music mode."""
    r, g, b = (max(0, min(int(c), 255)) for c in rgb)
    msg = json.dumps(
        {"msg": {"cmd": "colorwc",
                 "data": {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}}},
        separators=(",", ":"),
    ).encode()
    _send(msg, ip)


if __name__ == "__main__":
    import sys

    rgb = tuple(int(a) for a in sys.argv[1:4]) if len(sys.argv) >= 4 else (255, 0, 0)
    sens = int(sys.argv[4]) if len(sys.argv) >= 5 else 100
    pkt = set_music(rgb, sens)
    print(f"sent music rgb={rgb} sens={sens}: {pkt.hex(' ')}")
