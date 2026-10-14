# Govee Music Bridge

A Home Assistant add-on that keeps Govee H6076 floor lamps in **music mode with a colour you choose**.
The lamps still pulse to the beat through their own microphone, but they hold your colour instead
of cycling through the rainbow, and changing the colour repaints the palette without leaving music mode.

## Why this exists

Govee's Platform API advertises a fixed-colour option for music mode. The H6076 accepts that request,
reports success, and keeps cycling colours anyway. The fixed palette lives in the separate **Music
DreamView** feature, and the cloud API can only switch DreamView on and off.

This bridge combines both channels:

1. the Govee Platform API switches the lamp's saved Music DreamView session on;
2. the Home Assistant host writes an eight-colour palette to the lamp over Bluetooth LE.

The palette format, two checksummed `0xA3` frames written to vendor characteristic
`00010203-0405-0607-0809-0a0b0c0d2b11`, was reconstructed from the Govee app's own Bluetooth
traffic and verified on real lamps.

## Requirements

- Home Assistant OS or Supervised, on a host with a Bluetooth adapter within range of the lamps.
- Govee H6076 lamps added to Home Assistant as `light` entities (for example with the
  built-in Govee lights local integration). Other Govee models with Music DreamView may work but are
  untested.
- A Govee API key: Govee Home app, **Settings > Apply for API Key**.
- A Music DreamView session on the lamp. In the Govee app, configure music mode with **Auto colour
  off** once; the bridge reuses that saved session. A lamp without one is reported as a DreamView
  follower and is left alone.

## Install

Add this repository to the Home Assistant app store:

```text
https://github.com/artur-safonov/govee-music-bridge
```

Install **Govee Music Bridge** and follow its [documentation](govee_music/DOCS.md) to create three
helpers and list your lamps. Alternatively, copy `govee_music/` to `/addons` on your Home
Assistant host and reload the local apps list.

## Status

Version 1.1.0 is the first public release. The bridge has been in use on two H6076 lamps. This
release moved every lamp and entity name from the source into add-on options; that new
configuration path is covered by unit tests but has not yet run on a Home Assistant host. Bug
reports with the add-on log are welcome.

## Development

```sh
cd govee_music
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To run the daemon on a workstation, put your lamps in `govee_music/options.json` (same format
as the add-on options, ignored by Git) and set `HA_URL`, `HA_TOKEN` and `GOVEE_API_KEY` in the
environment or a local `.env`.

## Acknowledgements

These open source projects documented parts of the Govee cloud, LAN and Bluetooth protocols and
informed this work. This repository does not include their code.

- [wez/govee2mqtt](https://github.com/wez/govee2mqtt)
- [homebridge-plugins/homebridge-govee](https://github.com/homebridge-plugins/homebridge-govee)
- [lasswellt/govee-homeassistant](https://github.com/lasswellt/govee-homeassistant)
- [Galorhallen/govee-local-api](https://github.com/Galorhallen/govee-local-api)
- [regnirof/hciparse](https://github.com/regnirof/hciparse), used to read Bluetooth captures

## License

[MIT](LICENSE). Dependencies keep their own licenses; see
[third-party notices](govee_music/THIRD_PARTY_NOTICES.md). This project is not affiliated with or
endorsed by Govee.
