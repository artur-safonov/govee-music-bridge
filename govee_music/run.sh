#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

export GOVEE_API_KEY="$(bashio::config 'govee_api_key')"
export GOVEE_SKU="$(bashio::config 'sku')"

# Running on the Pi: talk to BlueZ directly instead of hopping over SSH.
export GOVEE_BLE_MODE="local"

# SUPERVISOR_TOKEN is injected because config.yaml sets homeassistant_api: true, so the daemon
# authenticates through the Supervisor Core proxy and needs no stored long-lived token.
if bashio::var.has_value "${SUPERVISOR_TOKEN:-}"; then
    bashio::log.info "using the Supervisor Core proxy for Home Assistant access"
else
    bashio::log.warning "no SUPERVISOR_TOKEN - falling back to HA_URL/HA_TOKEN if present"
fi

if ! bashio::var.has_value "${GOVEE_API_KEY}"; then
    bashio::log.warning \
        "govee_api_key is empty; colour-locked music needs it. Set it in the add-on options."
fi

bashio::log.info "starting the Govee music bridge"
cd /app

# Supervise the daemon in-container. daemon.py already survives dropped Home Assistant websockets on
# its own, but any other unhandled exit would stop the container and Supervisor will not restart it
# without a watchdog endpoint - and this add-on listens on no port to give one. So restart it here.
while true; do
    status=0
    python3 -u daemon.py || status=$?
    if [ "${status}" -eq 2 ]; then
        bashio::log.error "configuration error (see above); fix the add-on options and restart it"
        sleep 300
    else
        bashio::log.warning "daemon exited (${status}), restarting in 5s"
        sleep 5
    fi
done
