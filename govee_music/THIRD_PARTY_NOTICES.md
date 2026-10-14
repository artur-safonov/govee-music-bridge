# Third-party components

Govee Music Bridge's own code and documentation are licensed under the MIT License in `LICENSE`.
That license does not cover dependencies, the container image, or Govee's services.

- **websockets**, BSD-3-Clause; installed from PyPI, not vendored.
  <https://github.com/python-websockets/websockets>
- **Home Assistant base image** (`ghcr.io/home-assistant/*-base`) and the Alpine packages
  installed in it (Python, GLib, D-Bus, BlueZ) keep their own licenses. When you distribute a built
  image, keep their notices. <https://github.com/home-assistant/docker-base>

Govee, DreamView and H6076 are names used to identify the hardware this project works with. Use of
the Govee Platform API is subject to Govee's terms; this project's license grants no rights to
Govee's services or brand.
