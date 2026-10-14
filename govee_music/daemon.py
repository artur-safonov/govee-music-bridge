"""Bridge Home Assistant -> colour-locked Govee music mode, for every configured lamp.

HA cannot emit a raw UDP packet and cannot reach the lamps over BLE: shell_command and
python_script are not configured on that box and neither can be added over the REST API. So this
process watches HA over the WebSocket API and does the sending.

STATE MODEL (what the user asked for)

    mode_select (input_select)           one shared state for all lamps
        "Off"   -> lamps behave as ordinary lights; a colour change just sets the colour
        <mode>  -> lamps are beat-reactive; a colour change re-paints the music palette and
                   the lamps STAY in music mode

The four rules the behaviour must satisfy:

    1. Lamps off, press ON -> both come on in whatever mode is currently selected. The selected mode is
       remembered as intent even while the lamps are off.
    2. Press the MUSIC switch -> both lamps move between music and plain colour. It must never switch a
       lamp off, so leaving music sends a solid colour rather than any power command.
    3. Lamps off, press the MUSIC switch -> nothing visible happens; the mode is only remembered. This
       needs care, because arming DreamView would switch the lamps ON - so music is never applied to a
       lamp that is off.
    4. Press OFF -> both lamps go off and STAY off, whatever mode they were in. That requires tearing
       down the DreamView session, which otherwise re-powers them about 30s later.

    music_guard (input_boolean)          guard consumed by the Stream Deck group-light scripts.
                                         While on, those scripts skip every lamp, so a deck colour
                                         or brightness press cannot knock them out of music mode.
                                         This daemon then re-applies the palette instead, which is
                                         why the colour changes without the music dropping.

The entity ids are installation settings; see settings.py and the add-on's Configuration tab.

A colour change is picked up two ways:
  * the preferred path - a call to the group colour script is observed directly, so the palette
    is repainted without the lamp ever leaving music mode (no flicker);
  * a fallback - if a lamp's own rgb_color changes while music is active (someone used the HA UI or
    the lamp entity directly), the palette is re-applied for that lamp.
Re-applying cannot feed back on itself: a music/palette write does not move the lamp's colour
register, so HA's polled state does not change when we send it.

TRANSPORT (per lamp, see README)
  1. Govee Platform API toggles the saved Music DreamView session on.
  2. The Home Assistant Raspberry Pi writes the chosen palette over BLE to that lamp's own MAC.
Both are per-device, so each lamp gets its own client pair.

A lamp only works this way once a Music DreamView session exists on it, which is created by
configuring music mode with Auto-colour off in the Govee app. Without it the cloud replies
"The device does not has DreamView" and that lamp falls back to the LAN auto-colour selector.

Run:  python daemon.py   (lamps and helpers from options.json, credentials from the environment)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import time
from datetime import datetime

import websockets

import settings
from govee_ble_local import GoveeBLELocalClient, GoveeBLELocalError
from govee_ble_ssh import GoveeBLESSHClient, GoveeBLESSHError
from govee_lan import MUSIC_SUBMODES, set_colour, set_music_submode, set_power
from govee_platform import GoveeAPIError, GoveePlatformClient
from lamps import DEFAULT_LOCAL_IP, LAMPS, SETTINGS, Lamp, resolve_ips

SELECT = SETTINGS.mode_select
GUARD = SETTINGS.music_guard
SENSITIVITY = SETTINGS.sensitivity_entity
# Optional: a script that sets one colour on a group of lights. Watching its calls lets a colour
# change repaint the music palette without the lamps ever leaving music mode. Empty disables it.
GROUP_COLOR_SCRIPT = SETTINGS.group_color_script
# Optional: a light-group helper containing the lamps, which a wall/deck button may target.
GROUP_ENTITY = SETTINGS.group_light
# Preferred deck target: this script does NOT cut power itself, so the off sequence can make the lamps
# static before switching them off - no blink. See its description in Home Assistant.
TOGGLE_SCRIPT = SETTINGS.toggle_script
TOGGLE_SCRIPT_ENTITY = f"script.{TOGGLE_SCRIPT}" if TOGGLE_SCRIPT else ""
# Best deck target: an intent switch. Lamp state briefly reads "off" while DreamView activates, and a
# script's state lingers for the length of its safety delay - both made the button misreport and drop
# presses. A boolean flips instantly and never lags.
POWER_INTENT = SETTINGS.power_intent

# --- timings -------------------------------------------------------------------------------------
# The OFF sequence was ~15s, which is too slow to use. These are the knobs. Each is a trade-off
# between speed and the lamps actually obeying, so they are named rather than sprinkled inline.
CYCLE_OFF_WAIT = 1.4      # power-cycle: how long the lamps stay off before being switched back on
CYCLE_ON_WAIT = 1.0       # power-cycle: gap between the two power-on rounds
STATIC_HOLD = 1.2         # how long the static colour is visible before the power is cut
OFF_SETTLE = 0.2          # after leaving music, before the first turn_off
OFF_VERIFY_WAIT = 0.8     # after a turn_off, before checking whether it took
COLOUR_REPEAT_GAP = 0.25  # gap between the two colorwc datagrams (UDP, unacknowledged)
DREAMVIEW_SETTLE = 0.8    # after stopping DreamView, before switching the follower lamp off
# Kept short on purpose. The owner reporting itself off is a nice confirmation but Home Assistant polls
# these lamps slowly, so it usually still says "on" even once the lamp is dark - waiting 3s for it was
# pure delay. If the follower is switched off a touch early the retry loop in _force_off covers it.
OWNER_OFF_WAIT = 0.8
OFF = "Off"
FALLBACK_COLOUR = (255, 255, 255)

# Govee's Platform API names differ slightly from the labels in the phone app.
# In particular, the app's Rhythm mode has a Dynamic/Calm selector which the API
# exposes as two separate musicMode enum values.
MODE_OPTIONS = [
    OFF,
    "Energic",
    "Rhythm Dynamic",
    "Rhythm Calm",
    "Bounce",
    "Hopping",
    "Strike",
    "Vibrate",
    "Skittles",
    "Torch",
    "Candy Crush",
    "Blend",
    "Luminous",
    "Separation",
]
PLATFORM_MODE_ALIASES = {
    "Rhythm": "Dynamic",  # legacy helper option
    "Rhythm Dynamic": "Dynamic",
    "Rhythm Calm": "Calm",
    "Candy Crush": "CandyCrush",
    "Blend": "Fusion",
}
LAN_MODE_ALIASES = {
    "Rhythm Dynamic": "Rhythm",
    "Rhythm Calm": "Rhythm",
}


def load_config() -> dict[str, str]:
    """Credentials and transport overrides: the environment, then a local .env for development."""
    config = dict(os.environ)
    env = pathlib.Path(__file__).resolve().parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in {'"', "'"}
                ):
                    value = value[1:-1]
                config.setdefault(name.strip(), value)
    return config


CONFIG = load_config()


def load_ha_config() -> tuple[str, str, str]:
    """Returns (rest_base, ws_url, token).

    Running as a Home Assistant add-on on the Pi, Supervisor injects SUPERVISOR_TOKEN and proxies
    Core - so no long-lived token has to be stored anywhere. Note the websocket lives at
    /core/websocket, NOT /core/api/websocket, so the usual suffix rule does not apply here.
    """
    supervisor = CONFIG.get("SUPERVISOR_TOKEN")
    if supervisor and not CONFIG.get("HA_TOKEN"):
        return "http://supervisor/core/api", "ws://supervisor/core/websocket", supervisor

    url, token = CONFIG.get("HA_URL"), CONFIG.get("HA_TOKEN")
    if not url or not token:
        sys.exit("need HA_URL and HA_TOKEN (env or .env), or run as an add-on with "
                 "homeassistant_api: true")
    base = url.rstrip("/")
    ws = (base.replace("https://", "wss://").replace("http://", "ws://")
          + "/api/websocket")
    return f"{base}/api", ws, token


class _Tee:
    def __init__(self, console, log):
        self.console = console
        self.log = log

    def write(self, value):
        self.log.write(value)
        self.log.flush()
        if self.console is not None:
            self.console.write(value)
            self.console.flush()

    def flush(self):
        self.log.flush()
        if self.console is not None:
            self.console.flush()


def enable_daemon_log() -> None:
    """Mirror stdout/stderr to a log file for background diagnostics.

    Skipped when running as an add-on: Supervisor already captures stdout and shows it in the add-on
    log, and the container filesystem is ephemeral so a file there would just be lost.
    """
    if CONFIG.get("SUPERVISOR_TOKEN"):
        print(f"--- daemon start {datetime.now().isoformat()} (add-on) ---", flush=True)
        return
    project = pathlib.Path(__file__).resolve().parent
    log_path = project / "logs" / "daemon.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"(file logging disabled: {exc})", flush=True)
        return
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)
    print(f"\n--- daemon start {datetime.now().isoformat()} ---", flush=True)


class LampController:
    """One lamp: its colour memory, its cloud client and its BLE writer."""

    def __init__(self, lamp: Lamp, music_client):
        self.lamp = lamp
        self.music_client = music_client
        self.last_solid: tuple[int, int, int] = FALLBACK_COLOUR
        self.last_brightness = 200
        self._last_call = 0.0
        # set once a lamp is known to lack a DreamView session, so we stop retrying the cloud
        self.no_dreamview = False
        # Our own writes make the lamp report power changes back to HA. Ignore its transitions for a
        # moment after we act, otherwise arming music (which powers the lamp on) looks like the user
        # switching it on and we re-arm forever.
        self.suppress_until = 0.0
        # Whether the lamp is powered on. Music is never applied to a lamp that is off, because
        # arming DreamView would switch it on - see rule 3 in the module docstring.
        self.is_on = False

    SUPPRESS_SECONDS = 10.0

    def suppress(self) -> None:
        self.suppress_until = time.monotonic() + self.SUPPRESS_SECONDS

    def suppressed(self) -> bool:
        return time.monotonic() < self.suppress_until

    @property
    def entity_id(self) -> str:
        return self.lamp.entity_id

    def note_colour(self, state: dict | None) -> bool:
        attributes = (state or {}).get("attributes") or {}
        brightness = attributes.get("brightness")
        if isinstance(brightness, (int, float)) and brightness > 0:
            self.last_brightness = int(brightness)
        rgb = attributes.get("rgb_color")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            new = tuple(int(component) for component in rgb)
            if new != self.last_solid:
                self.last_solid = new
                return True
        return False

    def note_power(self, state: dict | None) -> None:
        value = (state or {}).get("state")
        if value in ("on", "off"):
            self.is_on = value == "on"

    async def off(self) -> None:
        """Leave music and hold a solid colour.

        Deliberately does NOT stop the DreamView session: `set_dreamview(False)` switches the session
        OWNER lamp off, which is why the left lamp died on every music switch while the right one kept
        shining. Leaving music is done by power-cycling the lamps instead (Bridge.power_cycle), which
        the user found reliable and which exits music without killing anything.
        """
        if self.lamp.ip:
            # colorwc is fire-and-forget UDP with no acknowledgement, so a single datagram can simply
            # be lost - which showed up as a lamp occasionally not going static. Send it twice.
            set_colour(self.last_solid, ip=self.lamp.ip)
            await asyncio.sleep(COLOUR_REPEAT_GAP)
            set_colour(self.last_solid, ip=self.lamp.ip)
        deactivate = getattr(self.music_client, "deactivate", None)
        if callable(deactivate):
            deactivate()
        self.suppress()

    async def stop_session(self) -> str:
        """Switch this lamp's DreamView session off, so it cannot re-power itself."""
        self.suppress()
        stop = getattr(self.music_client, "stop", None)
        if not callable(stop) or self.no_dreamview:
            deactivate = getattr(self.music_client, "deactivate", None)
            if callable(deactivate):
                deactivate()
            return "no session of its own"
        try:
            await asyncio.to_thread(stop)
            return "DreamView session stopped"
        except (GoveeAPIError, GoveeBLESSHError, GoveeBLELocalError,
                OSError, ValueError) as exc:
            return f"could not stop the session: {exc}"

    async def music(self, mode: str, sensitivity: int) -> tuple[bool, str]:
        """Apply a music mode with this lamp's palette. Returns (ok, detail)."""
        if mode == OFF:
            # Arming calls set_dreamview(True), which switches the lamp ON, so asking for "Off" here
            # powers the lamps back up - the opposite of what the caller wanted. Callers must handle
            # OFF themselves; refuse it rather than silently re-powering the lamps.
            return False, "refusing to arm music for mode Off"
        platform_mode = PLATFORM_MODE_ALIASES.get(mode, mode)
        lan_mode = LAN_MODE_ALIASES.get(mode, mode)

        if self.no_dreamview:
            # DreamView is a multi-device SYNC GROUP, not a per-lamp effect. A lamp without its own
            # session follows the lamp that has one: it arms, recolours and beats in step. Sending
            # it anything here is worse than doing nothing - the LAN selector only offers
            # auto-colour, which would drop this lamp out of the group's fixed palette.
            return True, "following the DreamView group (no command sent)"

        if self.music_client is not None:
            try:
                # The official API allows two control requests per second per device.
                delay = 0.55 - (time.monotonic() - self._last_call)
                if delay > 0:
                    await asyncio.sleep(delay)
                await asyncio.to_thread(
                    self.music_client.set_music,
                    platform_mode,
                    self.last_solid,
                    sensitivity=sensitivity,
                    auto_color=False,
                )
                self._last_call = time.monotonic()
                self.suppress()
            except (GoveeAPIError, GoveeBLESSHError, GoveeBLELocalError,
                    OSError, ValueError) as exc:
                if "does not has DreamView" in str(exc):
                    self.no_dreamview = True
                    return True, (
                        "no DreamView session of its own - treating it as a member of another "
                        "lamp's DreamView group, which Govee syncs; sending nothing"
                    )
                return False, str(exc)
            transport = getattr(self.music_client, "description", None)
            if not isinstance(transport, str):
                transport = "Platform API"
            return True, (f"rgb={self.last_solid} sensitivity={sensitivity} "
                          f"via {transport}")

        return True, self._lan_fallback(lan_mode)

    def _lan_fallback(self, lan_mode: str) -> str:
        if lan_mode not in MUSIC_SUBMODES:
            return f"cannot map {lan_mode!r} to a LAN sub-mode"
        if not self.lamp.ip:
            return f"no LAN IP known for {self.lamp.name}"
        set_music_submode(lan_mode, ip=self.lamp.ip)
        return f"0x{MUSIC_SUBMODES[lan_mode]:02x} via LAN (auto-colour)"


class Bridge:
    def __init__(self, ws, controllers: list[LampController]):
        self.ws = ws
        self.controllers = controllers
        self._id = 10
        self.sensitivity = 100
        self.mode = OFF
        # Applying a mode takes 5-10s per lamp (cloud DreamView toggle, settle, BLE connect, four
        # frame writes). Awaiting that inline meant presses a few seconds apart queued up and the
        # lamps visibly replayed stale commands - it looked like the switch did nothing. Instead the
        # handlers only record what is WANTED and wake a worker, which always applies the latest
        # value and skips everything in between.
        self.desired_mode = OFF
        self._wake = asyncio.Event()
        self._reconciling = False
        # what we last actually pushed to the lamps, so a mode CHANGE can be told apart from a
        # palette repaint within the same mode (only the former needs a power cycle)
        self.applied_mode = OFF
        # ONE global lock for a whole operation. Per-lamp suppression was not enough: a cycle plus two
        # BLE arms takes far longer than the window, so our own off/on leaked back in as user intent
        # and re-triggered everything - the churn that showed up as delay and lamps dropping out.
        self.busy_until = 0.0
        # Long lamp operations must NOT be awaited from the websocket loop: while one sleeps, no events
        # are read, so is_on goes stale and decisions are made on data minutes old. That is why an
        # off-retry loop reported "still on" for lamps that had already gone off. They run as tasks
        # instead, serialised on this lock so two of them cannot drive the lamps at once.
        self.lamp_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._off_watch: asyncio.Task | None = None
        # One press of the deck's off button emits BOTH light.toggle on the group and light.turn_off on
        # the members, so two sequences used to start. The second saw applied_mode already Off, skipped
        # the go-static step and switched off immediately - which is why static was applied only
        # sometimes. Set synchronously when a press is seen, before anything is spawned.
        self._off_pending = False
        # Stays set from an OFF press until the user asks for the lamps ON again. While set, nothing
        # re-arms music: a lamp that re-powers itself must not drag the whole group back into music.
        self.off_intent = False
        # When WE update the power-intent switch (to keep the deck button honest after the script or the
        # group was used), the change comes back as an event. Ignore our own echo briefly, otherwise it
        # would start a fresh sequence.
        self._intent_echo = 0.0

    BUSY_TAIL = 4.0

    def spawn(self, coro) -> None:
        """Run a lamp operation off the event loop so events keep being processed."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception():
                print(f"[error] background task failed: {finished.exception()!r}",
                      flush=True)

        task.add_done_callback(done)

    async def set_power_intent(self, on: bool) -> None:
        """Keep the deck's power switch showing reality, without triggering a new sequence."""
        if not POWER_INTENT:
            return
        self._intent_echo = time.monotonic() + 3.0
        await self.call("input_boolean", "turn_on" if on else "turn_off",
                        {"entity_id": POWER_INTENT})

    def hold(self, seconds: float = 0.0) -> None:
        self.busy_until = max(self.busy_until, time.monotonic() + seconds)

    def busy(self) -> bool:
        return time.monotonic() < self.busy_until

    async def call(self, domain: str, service: str, data: dict) -> None:
        self._id += 1
        await self.ws.send(json.dumps({
            "id": self._id, "type": "call_service",
            "domain": domain, "service": service, "service_data": data,
        }))

    async def set_guard(self, on: bool) -> None:
        await self.call("input_boolean", "turn_on" if on else "turn_off",
                        {"entity_id": GUARD})

    async def set_mode_options(self) -> None:
        await self.call("input_select", "set_options",
                        {"entity_id": SELECT, "options": MODE_OPTIONS})

    async def notify(self, title: str, message: str) -> None:
        await self.call("persistent_notification", "create",
                        {"title": title, "message": message})

    def controller_for(self, entity_id: str) -> LampController | None:
        for controller in self.controllers:
            if controller.entity_id == entity_id:
                return controller
        return None

    async def power_cycle(self, why: str) -> bool:
        """Switch both lamps off and back on, then restore their solid colour.

        The user's own finding, and it is the most reliable transition available: the lamps always come
        back in a clean static state, from which music can be armed. It replaces stopping the DreamView
        session, which had the side effect of switching the owner lamp off.

        Returns False when no lamp was on, so callers can skip it.
        """
        live = [c for c in self.controllers if c.is_on]
        if not live:
            return False
        self.hold(60.0)

        entity_ids = [c.entity_id for c in self.controllers]
        for controller in self.controllers:
            # our own off/on must not be read as the user power-cycling the lamps
            controller.suppress()

        print(f"[cycle] off/on both lamps ({why})", flush=True)
        await self.call("light", "turn_off", {"entity_id": entity_ids})
        await asyncio.sleep(CYCLE_OFF_WAIT)

        # Send the power-on UNCONDITIONALLY twice, rather than skipping lamps that HA claims are
        # already on. Home Assistant marks a light on optimistically as soon as the command is
        # accepted, so a lamp that physically missed it still reads as "on" - and then it never got
        # its static colour, which is the "left lamp just switches off without going static" case.
        # Each round goes over Home Assistant AND straight to the lamp over the LAN.
        for attempt in (1, 2):
            for controller in self.controllers:
                await self.call("light", "turn_on", {
                    "entity_id": controller.entity_id,
                    "rgb_color": list(controller.last_solid),
                    "brightness": controller.last_brightness,
                })
                if controller.lamp.ip:
                    set_power(True, ip=controller.lamp.ip)
            await asyncio.sleep(CYCLE_ON_WAIT)
            print(f"[cycle] power-on round {attempt} sent to both lamps", flush=True)

        # We have just commanded both lamps on twice over two independent channels, so treat them as
        # on: the following static colour write must reach BOTH, not only the ones HA happens to
        # report as on.
        for controller in self.controllers:
            controller.is_on = True
            controller.suppress()
        print("[cycle] both lamps commanded on", flush=True)
        return True

    async def ensure_off(self, why: str, sync_intent: bool = True) -> None:
        """Make BOTH lamps actually go off, in music mode or not.

        The long-standing complaint: with music running, pressing OFF switched only the left lamp off
        while the right kept shining. The right lamp is the DreamView FOLLOWER, and a follower has been
        seen to ignore a plain turn_off while the group session is live. Reacting to lamp state did not
        help either, because a lamp that ignores the command produces no state change to react to.

        So, in the order the user suggested: leave music first, then switch off, then VERIFY and retry -
        pushing the command over Home Assistant and directly over the LAN, which are separate paths.
        """
        try:
            # Music running: use the short path the user proposed. Stopping DreamView switches the
            # SESSION OWNER off by itself and drops the FOLLOWER into static, so there is no power
            # cycle and nothing has to be brought back on first - which is what made this slow.
            unwound = False
            if self.applied_mode != OFF and any(c.is_on for c in self.controllers):
                await self._off_via_dreamview(why)
                unwound = True
            else:
                print(f"[offcmd] step 1: music not running, nothing to unwind ({why})",
                      flush=True)

            # STEP 3 - switch off whatever is still on. After the DreamView path that is normally just
            # the follower lamp, the owner having switched itself off already.
            remaining = [c.lamp.name for c in self.controllers if c.is_on]
            print(f"[offcmd] step 3: switching off "
                  f"{', '.join(remaining) if remaining else 'nothing left'} ({why})",
                  flush=True)
            async with self.lamp_lock:
                self.hold(45.0)
                try:
                    # sessions are already stopped when we came through the DreamView path, so do not
                    # pay for those cloud round-trips twice
                    await self._force_off(why, stop_sessions=not unwound)
                finally:
                    self.busy_until = time.monotonic() + self.BUSY_TAIL
        finally:
            self._off_pending = False

        # Keep the deck's switch honest when the sequence came from the script or the group. NOT when
        # the intent switch itself triggered us: it is already off, and setting it again opens an echo
        # window that swallowed a genuine ON press landing in the same moment.
        if sync_intent:
            await self.set_power_intent(False)

        # A lamp can still re-power itself after the command sequence finishes, so keep watching.
        self.start_off_watch()

    def start_off_watch(self) -> None:
        self.cancel_off_watch()
        self._off_watch = asyncio.create_task(self._watch_off())

    def cancel_off_watch(self) -> None:
        task = getattr(self, "_off_watch", None)
        if task and not task.done():
            task.cancel()
        self._off_watch = None

    async def _watch_off(self) -> None:
        """Hold the lamps off for a while after an OFF press.

        The follower lamp has been seen to come back on in static mode after the off sequence has
        already finished and reported success. Cancelled as soon as the user asks for them ON again.
        """
        try:
            # Keep watching for as long as the user's OFF intent stands, not just 60s: a lamp came back
            # on after the old window expired and nothing was left watching.
            for _ in range(75):  # up to ~10 minutes
                await asyncio.sleep(8.0)
                if not self.off_intent:
                    return
                strays = [c for c in self.controllers if c.is_on]
                if not strays:
                    continue
                names = ", ".join(c.lamp.name for c in strays)
                print(f"[offwatch] {names} came back on - switching off again", flush=True)
                async with self.lamp_lock:
                    self.hold(30.0)
                    try:
                        await self._force_off("re-powered itself after off")
                    finally:
                        self.busy_until = time.monotonic() + self.BUSY_TAIL
        except asyncio.CancelledError:
            pass

    async def _off_via_dreamview(self, why: str) -> None:
        """Stop DreamView, wait for the follower to fall back to static, then leave it to be switched off.

        The user's flow, and the fastest one available:
          1. stop DreamView  -> the session OWNER switches itself off, the FOLLOWER drops to static
          2. wait for the owner to report itself off - the one observable proof it took effect
          3. brief settle so the follower is visibly static, then the caller switches it off

        No power cycle and nothing is brought back on, which is where the seconds were going.
        """
        # Set these BEFORE touching the select, so the worker sees no mode change and does not
        # power-cycle: that cycle is exactly the cost being removed here.
        self.applied_mode = OFF
        self.desired_mode = OFF
        self.mode = OFF

        owners = [c for c in self.controllers if not c.no_dreamview]
        followers = [c for c in self.controllers if c.no_dreamview]
        print(f"[offcmd] step 1: stopping DreamView ({why}) - owner "
              f"{', '.join(c.lamp.name for c in owners) or 'none'} switches off, follower "
              f"{', '.join(c.lamp.name for c in followers) or 'none'} goes static",
              flush=True)

        async with self.lamp_lock:
            self.hold(45.0)
            try:
                if owners:
                    await asyncio.gather(*(c.stop_session() for c in owners))
            finally:
                self.busy_until = time.monotonic() + self.BUSY_TAIL

        # keep the HA select and the deck's music switch in step with reality
        await self.call("input_select", "select_option",
                        {"entity_id": SELECT, "option": OFF})
        # _off_via_dreamview sets desired_mode to Off before changing the select above.  The
        # resulting state_changed event is therefore (correctly) ignored by event_loop as an echo,
        # but that also means apply(Off) does not run and cannot clear the group-script guard for us.
        # Clear it explicitly so ordinary room colour/brightness commands include both TV lamps.
        await self.set_guard(False)

        # step 2 - the owner reporting itself off is our only observable confirmation
        deadline = time.monotonic() + OWNER_OFF_WAIT
        while owners and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if not any(c.is_on for c in owners):
                break
        state = ", ".join(f"{c.lamp.name}={'on' if c.is_on else 'off'}"
                          for c in self.controllers)
        print(f"[offcmd] step 2: {state} - letting the follower settle into static",
              flush=True)
        await asyncio.sleep(DREAMVIEW_SETTLE)

    async def _force_off(self, why: str, stop_sessions: bool = True) -> None:
        if stop_sessions:
            for controller in self.controllers:
                detail = await controller.stop_session()
                print(f"[offcmd] {controller.lamp.name}: leaving music - {detail} ({why})",
                      flush=True)
        self.applied_mode = OFF
        await asyncio.sleep(OFF_SETTLE)

        for attempt in range(1, 5):
            stragglers = [c for c in self.controllers if c.is_on]
            if not stragglers:
                break
            names = ", ".join(c.lamp.name for c in stragglers)
            print(f"[offcmd] attempt {attempt}: switching off {names}", flush=True)
            await self.call("light", "turn_off",
                            {"entity_id": [c.entity_id for c in stragglers]})
            for controller in stragglers:
                if controller.lamp.ip:
                    # independent channel, in case the integration's command is ignored
                    set_power(False, ip=controller.lamp.ip)
            await asyncio.sleep(OFF_VERIFY_WAIT)

        remaining = [c.lamp.name for c in self.controllers if c.is_on]
        print(f"[offcmd] finished ({why}); still on: "
              f"{', '.join(remaining) if remaining else 'none'}", flush=True)

    def request(self, mode: str, why: str) -> None:
        """Record the wanted mode and wake the worker. Never blocks the event loop."""
        self.desired_mode = mode
        self._why = why
        self._wake.set()

    async def reconcile_worker(self) -> None:
        """Apply the latest desired state, coalescing anything that arrived while we were busy."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                target = self.desired_mode
                why = getattr(self, "_why", "reconcile")
                self._reconciling = True
                # hold for the whole operation, so nothing we do to the lamps is mistaken for the user
                self.hold(90.0)
                try:
                    self.mode = target
                    async with self.lamp_lock:
                        await self.apply(target, why)
                finally:
                    self._reconciling = False
                    self.busy_until = time.monotonic() + self.BUSY_TAIL
                if self.desired_mode == target:
                    break
                # something changed mid-apply: drop it and go straight to the newest value
                print(f"[coalesce] {target!r} superseded by {self.desired_mode!r}",
                      flush=True)
                self._wake.clear()

    async def handle_power(self, controller: LampController, state: str) -> None:
        """A lamp was switched on or off by someone while a music mode is selected.

        Switching a lamp off must stop its DreamView session, or the session re-asserts and powers the
        lamp back on about half a minute later - visible in the HA logbook as a state change with no
        service and no user context. Switching it on again re-arms music, which is why the mode select
        is deliberately left alone: the lamps resume the mode they were in.
        """
        if self.busy() or controller.suppressed():
            # our own writes, not the user
            return
        if state == "on":
            if self.mode == OFF:
                # THE bug behind "the right lamp turns back on a second later": this used to arm music
                # regardless of mode, and arming calls set_dreamview(True), which SWITCHES THE LAMP ON.
                # So any stray power-on event re-powered the lamps via "re-arming Off". With no music
                # mode selected there is nothing to arm - a lamp coming on is just a light.
                print(f"[power] {controller.lamp.name}: switched on, mode is Off - "
                      f"nothing to arm", flush=True)
                return
            if self.off_intent:
                # The user asked for OFF and has not asked for ON since. A lamp that re-powers itself
                # must NOT drag the group back into music - that produced
                # "[power] switched on - re-arming Rhythm Dynamic" right after a successful off.
                print(f"[power] {controller.lamp.name}: came on while held off - "
                      f"not re-arming, the off-watch will deal with it", flush=True)
                return

        async with self.lamp_lock:
            if state == "off":
                detail = await controller.stop_session()
                print(f"[power] {controller.lamp.name}: switched off - {detail}",
                      flush=True)
            else:
                # let the lamp finish powering up before writing a palette to it
                await asyncio.sleep(1.5)
                if self.off_intent or self.mode == OFF:
                    # An OFF press arrived while we were waiting. Abandon the re-arm rather than
                    # spending 6s on a DreamView activation and BLE write that the user has already
                    # cancelled - that wait was what made "off shortly after on" feel slow.
                    print(f"[power] {controller.lamp.name}: re-arm abandoned, off was "
                          f"requested", flush=True)
                    return
                ok, detail = await controller.music(self.mode, self.sensitivity)
                if ok:
                    # Record that music is now running. Without this, applied_mode stayed "Off" after a
                    # power-on re-arm, so the NEXT off press decided "music already off, nothing to
                    # unwind" and skipped the go-static step. That is exactly the intermittent
                    # "sometimes it just switches off without going static".
                    self.applied_mode = self.mode
                tag = "power" if ok else "error"
                print(f"[{tag}] {controller.lamp.name}: switched on - re-arming "
                      f"{self.mode}: {detail}", flush=True)

    async def apply(self, mode: str, why: str,
                    only: LampController | None = None) -> None:
        targets = [only] if only else self.controllers

        # Switching between music and static: power-cycle both lamps first. This is the transition the
        # user found reliable, and it avoids stopping the DreamView session, which switched the owner
        # lamp off. Only a genuine mode CHANGE cycles - repainting a palette in the same mode does not.
        if only is None and mode != self.applied_mode:
            await self.power_cycle(f"{self.applied_mode!r} -> {mode!r}")
        self.applied_mode = mode

        if mode == OFF:
            # both lamps in parallel: they are independent, and doing them in sequence added a needless
            # second to a sequence the user already finds slow
            live_targets = [c for c in targets if c.is_on]
            if live_targets:
                # keep them on, just solid - switching music off must never switch a lamp off
                await asyncio.gather(*(c.off() for c in live_targets))
                for controller in live_targets:
                    print(f"[off  ] {controller.lamp.name}: solid "
                          f"{controller.last_solid} ({why})", flush=True)
            for controller in targets:
                if controller.is_on:
                    continue
                # already off: never send a colour, that would switch the lamp back on
                controller.suppress()
                deactivate = getattr(controller.music_client, "deactivate", None)
                if callable(deactivate):
                    deactivate()
                print(f"[off  ] {controller.lamp.name}: stays off ({why})", flush=True)
            if not only:
                await self.set_guard(False)
            return

        if mode not in MODE_OPTIONS and mode not in PLATFORM_MODE_ALIASES:
            print(f"[warn ] unknown mode {mode!r} - ignoring", flush=True)
            return

        live = [controller for controller in targets if controller.is_on]
        if not live:
            # Rule 3: with the lamps off, selecting a music mode must do nothing visible. The mode is
            # remembered as intent, and gets applied when the lamps are switched on (rule 1).
            print(f"[skip ] all lamps are off - {mode} remembered, not applied ({why})",
                  flush=True)
            if not only:
                await self.set_guard(True)
            return

        failures = []
        for controller in live:
            ok, detail = await controller.music(mode, self.sensitivity)
            tag = "music" if ok else "error"
            print(f"[{tag}] {controller.lamp.name}: {mode} {detail} ({why})", flush=True)
            if not ok:
                failures.append(f"{controller.lamp.name}: {detail}")
        for controller in targets:
            if not controller.is_on:
                print(f"[skip ] {controller.lamp.name}: off, not armed ({why})", flush=True)

        if failures and not only:
            await self.set_guard(False)
            await self.notify("Govee music mode failed", "\n".join(failures))
        elif not failures:
            await self.set_guard(True)


def ble_is_local() -> bool:
    """Can this host write BLE itself, or must it hop to the Pi over SSH?

    Set GOVEE_BLE_MODE=local|ssh to override. Otherwise: a Bluetooth adapter plus gdbus means we are
    on the Pi and should talk to BlueZ directly, which removes plink, host keys and a stored password
    from the path entirely.
    """
    mode = CONFIG.get("GOVEE_BLE_MODE", "").strip().lower()
    if mode in {"local", "ssh"}:
        return mode == "local"
    return (
        pathlib.Path("/sys/class/bluetooth").is_dir()
        and shutil.which("gdbus") is not None
    )


async def build_controllers() -> list[LampController]:
    """One cloud+BLE client pair per lamp, or None for a LAN-only fallback."""
    resolve_ips(CONFIG.get("GOVEE_LOCAL_IP") or DEFAULT_LOCAL_IP)
    api_key = CONFIG.get("GOVEE_API_KEY", "")
    ssh_user = CONFIG.get("HA_SSH_USER", "")
    ssh_password = CONFIG.get("HA_SSH_PASSWORD", "")
    local_ble = ble_is_local()
    print(f"  BLE transport: {'local BlueZ' if local_ble else 'SSH to the Pi'}", flush=True)

    controllers: list[LampController] = []
    for lamp in LAMPS:
        client = None
        if api_key:
            platform = GoveePlatformClient(
                api_key,
                sku=CONFIG.get("GOVEE_SKU", "H6076"),
                device_id=lamp.device_id,
            )
            client = platform
            if local_ble:
                client = GoveeBLELocalClient(platform, address=lamp.ble_mac)
            elif ssh_user and ssh_password and not (
                    CONFIG.get("HA_SSH_HOST") and CONFIG.get("HA_SSH_HOST_KEY")):
                print("  SSH BLE transport needs HA_SSH_HOST and HA_SSH_HOST_KEY as well; "
                      "using the Platform API alone", flush=True)
            elif ssh_user and ssh_password:
                client = GoveeBLESSHClient(
                    platform,
                    host=CONFIG.get("HA_SSH_HOST", ""),
                    username=ssh_user,
                    password=ssh_password,
                    host_key=CONFIG.get("HA_SSH_HOST_KEY", ""),
                    remote_script=CONFIG.get(
                        "GOVEE_BLE_REMOTE_SCRIPT",
                        "/config/govee-music/govee_ble.py",
                    ),
                    plink=CONFIG.get("PUTTY_PLINK",
                                     r"C:\Program Files\PuTTY\plink.exe"),
                    address=lamp.ble_mac,
                )
        controllers.append(LampController(lamp, client))
        transport = getattr(client, "description", None) or (
            "Platform API" if client else "LAN fallback (auto-colour)")
        print(f"  {lamp.name:<14} {lamp.entity_id:<16} ip={lamp.ip or '?':<15} "
              f"ble={lamp.ble_mac}  {transport}", flush=True)
    return controllers


async def main() -> None:
    _, ws_url, token = load_ha_config()
    print(f"connecting to {ws_url}", flush=True)
    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"auth failed: {auth}")
        print(f"authenticated to HA {auth.get('ha_version')}", flush=True)

        print("lamps:", flush=True)
        controllers = await build_controllers()
        bridge = Bridge(ws, controllers)

        await ws.send(json.dumps({"id": 1, "type": "get_states"}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1 and message.get("type") == "result":
                for state in message.get("result") or []:
                    eid = state["entity_id"]
                    if eid == SELECT:
                        bridge.mode = state["state"]
                    elif eid == SENSITIVITY:
                        try:
                            bridge.sensitivity = int(float(state["state"]))
                        except (TypeError, ValueError):
                            pass
                    else:
                        controller = bridge.controller_for(eid)
                        if controller:
                            controller.note_colour(state)
                            controller.note_power(state)
                break
        print(f"primed: mode={bridge.mode!r} sensitivity={bridge.sensitivity} "
              + " ".join(f"{c.lamp.name}={c.last_solid}" for c in controllers),
              flush=True)

        await ws.send(json.dumps({"id": 2, "type": "subscribe_events",
                                  "event_type": "state_changed"}))
        await ws.send(json.dumps({"id": 3, "type": "subscribe_events",
                                  "event_type": "call_service"}))
        await bridge.set_mode_options()
        bridge.desired_mode = bridge.mode
        worker = asyncio.create_task(bridge.reconcile_worker())
        if bridge.mode == OFF:
            # Reconcile a stale guard left by an interrupted previous session.
            await bridge.set_guard(False)
        else:
            bridge.request(bridge.mode, "daemon startup")
        print(f"watching {SELECT}, {SENSITIVITY}, script.{GROUP_COLOR_SCRIPT}, "
              + ", ".join(c.entity_id for c in controllers), flush=True)

        try:
            await event_loop(ws, bridge, controllers)
        finally:
            # Without this, every reconnect would leave another worker behind holding a dead
            # websocket, and they would all fight over the lamps.
            worker.cancel()


async def event_loop(ws, bridge: Bridge, controllers: list[LampController]) -> None:
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") != "event":
                continue
            data = message["event"]["data"]

            # --- colour intent, taken from actual service calls -----------------
            #
            # Deliberately driven by call_service rather than state_changed. A lamp reports a
            # colour back when DreamView switches it on, and treating that echo as user intent
            # repainted the palette white immediately after arming.
            if message.get("id") == 3:
                service_data = data.get("service_data") or {}
                domain = data.get("domain")
                service = data.get("service")

                # --- the no-blink deck button -------------------------------------
                # The toggle script deliberately does not touch the lamps when switching off, so
                # we get control while they are still on and can make them static first.
                # NO busy() guard here, deliberately. The daemon never calls this script, so a call can
                # only be a human pressing the button - and dropping it because we happen to be busy
                # meant the press was ignored and the script's 25s safety net did the work instead,
                # which is where "~30 seconds before anything happens" came from. Overlap is handled by
                # _off_pending and lamp_lock, not by ignoring the user.
                if domain == "script":
                    requested = service_data.get("entity_id") or []
                    if isinstance(requested, str):
                        requested = [requested]
                    called_toggle = (service == TOGGLE_SCRIPT
                                     or (service in ("turn_on", "toggle")
                                         and TOGGLE_SCRIPT_ENTITY in requested))
                    if called_toggle:
                        if any(c.is_on for c in controllers):
                            if bridge._off_pending:
                                print("[offcmd] ignoring duplicate toggle-script press",
                                      flush=True)
                                continue
                            bridge._off_pending = True
                            bridge.off_intent = True
                            print("[offcmd] toggle script pressed, lamps are on -> "
                                  "static then off (no blink)", flush=True)
                            bridge.spawn(bridge.ensure_off("toggle script pressed"))
                        else:
                            # the script switches them on itself; just stop holding them off
                            bridge.off_intent = False
                            bridge.cancel_off_watch()
                            print("[offcmd] toggle script pressed, lamps are off -> "
                                  "the script switches them on", flush=True)
                        continue

                # --- an OFF press, taken from the service call itself ------------
                # Reacting to lamp STATE cannot work here: a lamp that ignores turn_off produces no
                # state change, which is exactly the follower-lamp symptom.
                if domain == "light" and service in ("turn_off", "turn_on", "toggle"):
                    requested = service_data.get("entity_id") or []
                    if isinstance(requested, str):
                        requested = [requested]
                    touches_lamps = any(
                        entity == GROUP_ENTITY or bridge.controller_for(entity)
                        for entity in requested)
                    # `not busy` is essential, not an optimisation: power_cycle itself calls
                    # light.turn_off and light.turn_on, and without this guard those come back as a
                    # user OFF press (switching the lamps off mid-transition) or as a user ON press
                    # (clearing the off intent).
                    if touches_lamps and not bridge.busy():
                        going_off = service == "turn_off" or (
                            service == "toggle" and any(c.is_on for c in controllers))
                        if going_off:
                            # One press emits BOTH light.toggle on the group and light.turn_off on the
                            # members. Dedup synchronously here, before spawning: otherwise the second
                            # sequence sees applied_mode already Off and skips the go-static step.
                            if bridge._off_pending:
                                print(f"[offcmd] ignoring duplicate "
                                      f"light.{service} press", flush=True)
                                continue
                            bridge._off_pending = True
                            bridge.off_intent = True
                            bridge.spawn(bridge.ensure_off(f"light.{service} pressed"))
                            continue
                        # the user wants them on again - stop holding them off
                        bridge.off_intent = False
                        bridge.cancel_off_watch()

                rgb = service_data.get("rgb_color")
                if not (isinstance(rgb, (list, tuple)) and len(rgb) == 3):
                    continue
                if bridge.busy():
                    # our own power_cycle turn_on carries rgb_color; without this it reads back as a
                    # colour press and triggers a redundant BLE write on every transition
                    continue
                colour = tuple(int(component) for component in rgb)

                if (data.get("domain") == "script"
                        and data.get("service") == GROUP_COLOR_SCRIPT):
                    targets = controllers
                    why = "group colour changed"
                elif data.get("domain") == "light" and data.get("service") == "turn_on":
                    requested = service_data.get("entity_id") or []
                    if isinstance(requested, str):
                        requested = [requested]
                    targets = [c for c in controllers if c.entity_id in requested]
                    why = "lamp colour command"
                else:
                    continue

                for controller in targets:
                    controller.last_solid = colour
                if targets and bridge.desired_mode != OFF:
                    # goes through the worker too, so a flurry of colour presses does not queue up
                    bridge.request(bridge.desired_mode, why)
                continue

            eid = data.get("entity_id")

            if eid == POWER_INTENT:
                # The deck's power button. Acting on intent rather than on lamp state means a press is
                # never misread while DreamView is mid-activation, and the button never lags.
                wanted = (data.get("new_state") or {}).get("state")
                previous = (data.get("old_state") or {}).get("state")
                if wanted == previous or wanted not in ("on", "off"):
                    continue
                if time.monotonic() < bridge._intent_echo:
                    # our own sync of the switch, not a press
                    continue
                if wanted == "off":
                    if bridge._off_pending:
                        print("[intent] off ignored, a sequence is already running",
                              flush=True)
                        continue
                    bridge._off_pending = True
                    bridge.off_intent = True
                    print("[intent] off -> static then off", flush=True)
                    bridge.spawn(bridge.ensure_off("power intent off",
                                                   sync_intent=False))
                else:
                    bridge.off_intent = False
                    bridge.cancel_off_watch()
                    print("[intent] on -> switching both lamps on", flush=True)
                    await bridge.call("light", "turn_on",
                                      {"entity_id": [c.entity_id
                                                     for c in controllers]})
            elif eid == SELECT:
                new = (data.get("new_state") or {}).get("state")
                if new and new != bridge.desired_mode:
                    bridge.request(new, "select changed")
            elif eid == SENSITIVITY:
                try:
                    sensitivity = int(float((data.get("new_state") or {}).get("state")))
                except (TypeError, ValueError):
                    continue
                if sensitivity != bridge.sensitivity:
                    bridge.sensitivity = sensitivity
                    if bridge.desired_mode != OFF:
                        bridge.request(bridge.desired_mode, "sensitivity changed")
            else:
                controller = bridge.controller_for(eid)
                if not controller:
                    continue
                old_state = (data.get("old_state") or {}).get("state")
                new_state = (data.get("new_state") or {}).get("state")
                controller.note_power(data.get("new_state"))
                if bridge.mode == OFF:
                    # Only track colours while music is off. During music the lamp's reported
                    # colour is a stale register value plus DreamView power-on echoes, not intent.
                    controller.note_colour(data.get("new_state"))
                    continue
                if new_state in ("on", "off") and new_state != old_state:
                    bridge.spawn(bridge.handle_power(controller, new_state))


async def run_forever() -> None:
    """Keep the bridge alive across HA restarts and network blips.

    Without this a single dropped WebSocket ends the process - observed as
    `ConnectionResetError: [WinError 64] The specified network name is no longer available`
    followed by `ConnectionClosedError` - and music mode silently stops working until someone
    notices. main() re-primes all state on each connect, so reconnecting is safe.

    Backoff doubles to a 60s ceiling, and resets after any session that stayed up a while, so a
    long-lived connection that finally drops retries immediately rather than at the old ceiling.
    """
    backoff = 2.0
    while True:
        started = time.monotonic()
        try:
            await main()
            return
        except (websockets.exceptions.WebSocketException, ConnectionResetError,
                OSError, ValueError) as exc:
            if time.monotonic() - started > 60:
                backoff = 2.0
            print(f"[reconn] HA connection lost ({type(exc).__name__}: {exc}); "
                  f"retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    enable_daemon_log()
    try:
        SETTINGS.require_lamps()
    except settings.SettingsError as exc:
        print(f"configuration error: {exc}", flush=True)
        sys.exit(2)
    load_ha_config()  # fail fast on missing credentials rather than inside the reconnect loop
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("\nstopped")
