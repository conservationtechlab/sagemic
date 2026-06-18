#!/bin/bash
set -e

APN="america.bics"
IFACE="wwan0"

echo "Waiting for modem with data port..."

for i in {1..60}; do
    MODEM_ID=$(mmcli -L 2>/dev/null | grep -o 'Modem/[0-9]*' | awk -F/ '{print $2}' | head -1)

    if [ -n "$MODEM_ID" ] && mmcli -m "$MODEM_ID" 2>/dev/null | grep -q "$IFACE"; then
        echo "Found modem $MODEM_ID with $IFACE"
        break
    fi

    sleep 2
done

if [ -z "$MODEM_ID" ]; then
    echo "No usable modem detected"
    exit 1
fi

ip link set "$IFACE" up || true

echo "Disconnecting stale sessions..."
mmcli -m "$MODEM_ID" --simple-disconnect || true
sleep 3

echo "Connecting LTE..."
mmcli -m "$MODEM_ID" --simple-connect="apn=${APN},ip-type=ipv4"

sleep 8

BEARER=""

for B in $(mmcli -m "$MODEM_ID" | grep -o 'Bearer/[0-9]*' | awk -F/ '{print $2}'); do
    if mmcli -b "$B" | grep -q 'connected: yes'; then
        BEARER="$B"
    fi
done

if [ -z "$BEARER" ]; then
    echo "No connected bearer found"
    mmcli -m "$MODEM_ID"
    exit 1
fi

echo "Using connected bearer: $BEARER"

BEARER_INFO=$(mmcli -b "$BEARER")

IP=$(echo "$BEARER_INFO" | awk '/address:/ {print $3}')
PREFIX=$(echo "$BEARER_INFO" | awk '/prefix:/ {print $3}')
GATEWAY=$(echo "$BEARER_INFO" | awk '/gateway:/ {print $3}')

if [ -z "$IP" ] || [ -z "$PREFIX" ] || [ -z "$GATEWAY" ]; then
    echo "Failed to get bearer network info"
    echo "$BEARER_INFO"
    exit 1
fi

ip addr flush dev "$IFACE"
ip addr add "${IP}/${PREFIX}" dev "$IFACE"
ip route replace default via "$GATEWAY" dev "$IFACE"

echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "nameserver 8.8.4.4" >> /etc/resolv.conf

echo "LTE connected: $IP/$PREFIX via $GATEWAY"
