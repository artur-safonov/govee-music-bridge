ARG BUILD_FROM
FROM ${BUILD_FROM}

# glib provides gdbus, which govee_ble.py shells out to for BlueZ GATT writes.
# bluez is needed for the D-Bus interfaces themselves.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        glib \
        dbus \
        bluez \
    && pip3 install --no-cache-dir --break-system-packages "websockets>=12"

WORKDIR /app
COPY daemon.py lamps.py settings.py govee_lan.py govee_platform.py govee_ble.py \
     govee_ble_local.py govee_ble_ssh.py /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
