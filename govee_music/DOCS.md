# Govee Music Bridge

## 1. Create the helpers

The bridge stores the selected music mode and a few flags in Home Assistant helpers. Create them under
**Settings > Devices & services > Helpers**, or in YAML:

```yaml
input_select:
  govee_music_mode:
    name: Govee music mode
    options: ["Off"]          # the bridge fills in the full list on start

input_boolean:
  govee_music_active:
    name: Govee music active  # on while music mode is applied

input_number:
  govee_music_sensitivity:
    name: Govee music sensitivity
    min: 0
    max: 100
    step: 1
```

The sensitivity helper is optional; without it the bridge uses 100. If you already have helpers with
other names, enter those names in the options below instead.

## 2. Find each lamp's device id

The Govee device id is eight bytes, such as `AA:BB:CC:DD:EE:FF:00:11`. The last six bytes are
the lamp's Bluetooth address. You can read it in the Govee app (device settings > device info), or
list every device on your account:

```sh
GOVEE_API_KEY=... python govee_platform.py devices
```

## 3. Configure the add-on

```yaml
govee_api_key: your-govee-api-key
sku: H6076
lamps:
  - name: Left lamp
    entity_id: light.left_lamp
    device_id: AA:BB:CC:DD:EE:FF:00:11
  - name: Right lamp
    entity_id: light.right_lamp
    device_id: 11:22:33:44:55:66:77:88
mode_select: input_select.govee_music_mode
music_guard: input_boolean.govee_music_active
sensitivity_entity: input_number.govee_music_sensitivity
```

Start the add-on and check its log. It lists each lamp with its discovered LAN address and the
transport it will use, then reports **watching** once it is ready. A configuration mistake is
reported in the log and the add-on waits for you to fix the options.

## Using it

- Pick a mode in **Govee music mode**: the lamps switch to that music mode in their current colour.
- Change a lamp's colour as usual: the bridge repaints the music palette and the lamps stay in music.
- Pick **Off**: the lamps return to a steady colour.
- Switching the lamps off while music is selected switches them off and keeps them off; the bridge
  stops the DreamView session, which would otherwise power them back on.
- A lamp that is off is never armed: arming DreamView would switch it on.

DreamView is a group feature. When several lamps share one session, arming the lamp that owns it arms
all of them, and the others report "The device does not has DreamView". That is expected; the bridge
then sends nothing to the followers.

## Optional integrations

These are for wall or Stream Deck buttons that control several lights at once. Leave them empty to
disable them.

| Option | Use |
|---|---|
| `group_color_script` | A script that sets one colour on many lights. The bridge watches its calls and repaints the music palette directly, without a flicker. Turn `music_guard` into a condition in that script so it skips the Govee lamps while music is active. |
| `group_light` | A light group containing the lamps. Switching it off is treated as switching the lamps off. |
| `toggle_script` | A script for a power button that does not cut power itself, so the bridge can show a steady colour before switching the lamps off. |
| `power_intent` | An `input_boolean` for a power button. A boolean changes instantly, so the button never misreads the lamps' brief "off" while DreamView starts. |

## Network and permissions

The add-on uses host networking for Govee's LAN discovery (UDP multicast on ports 4001 and 4002,
commands on 4003) and the host D-Bus to reach BlueZ. It uses the Home Assistant API through
Supervisor, so no long-lived access token is stored. Govee cloud requests go to
`openapi.api.govee.com` with your API key.

## Troubleshooting

- **No LAN address for a lamp:** enable the LAN control switch for the lamp in the Govee app.
- **Music works but the colour is ignored:** the lamp has no Music DreamView session; configure music
  with Auto colour off in the Govee app once.
- **Bluetooth writes fail:** the host adapter must be in range, and another device must not keep the
  lamp's Bluetooth connection open (close the Govee app on your phone).
