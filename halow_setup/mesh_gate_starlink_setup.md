*This file was created with help from Claude version Opus 5.5*
# Pi HaLow Hub with Starlink Backhaul

Sets up a Raspberry Pi as the DHCP server for a HaLow MeshGate and every device on the mesh, and shares a Starlink connection with them. The local network keeps working when Starlink is off; only internet access drops.

## Hardware

- Raspberry Pi running Raspberry Pi OS Trixie
- USB-to-ethernet adapter
- HaLow dongle configured as a MeshGate
- Starlink with an ethernet output (router port or Starlink Ethernet Adapter)

## Wiring

| Device | Connects to |
|---|---|
| HaLow MeshGate | Pi built-in ethernet port (`eth0`) |
| Starlink | USB-to-ethernet adapter in a Pi USB port (`eth1`) |

No switch is needed. If you add one later for more wired devices on the HaLow side, it goes on `eth0`. Starlink must never share a switch with the HaLow side, or its DHCP server will conflict with the Pi's.

## Network layout

| Interface | Role | Address |
|---|---|---|
| `eth0` | HaLow side (LAN) | Pi is `10.42.0.1/24`, hands out `10.42.0.x` |
| `eth1` | Starlink side (WAN) | Gets an address from Starlink |

## Setup

Run these with a keyboard and monitor, or over Wi-Fi or Tailscale. Don't run them over the ethernet connection you're changing.

### 1. Check interfaces

```bash
nmcli device
nmcli con show
```

Confirm `eth0` and `eth1` are present. If the USB adapter has a different name (e.g. `enx...`), use that wherever `eth1` appears below.

Confirm `eth1` is the Starlink (it should show a `192.168.1.x` address) and that `wlan0` is **not** on `10.42.0.x`:

```bash
ip -4 addr show eth1
ip -4 addr show wlan0
```

### 2. Install dnsmasq-base

```bash
sudo apt install -y dnsmasq-base
```

### 3. Remove existing ethernet profiles

Delete any auto-created ethernet profiles listed by `nmcli con show` (e.g. `netplan-eth0`, `Wired connection 1`). Leave Wi-Fi and Tailscale profiles alone.

```bash
sudo nmcli con delete netplan-eth0
```

### 4. Create the WAN (Starlink) profile

```bash
sudo nmcli con add type ethernet ifname eth1 con-name wan ipv4.method auto
```

### 5. Create the LAN (HaLow) profile

`shared` mode makes the Pi the DHCP server, the DNS server and the NAT gateway for `eth0`.

```bash
sudo nmcli con add type ethernet ifname eth0 con-name lan ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled
```

### 6. Bring both up

```bash
sudo nmcli con up wan
sudo nmcli con up lan
```

Both start automatically on boot.

### 7. Configure the HaLow side

- **MeshGate:** Set its LAN to DHCP client.
- **Mesh points:** Set them to bridge mode with their DHCP server off.

Power-cycle the MeshGate so it requests an address from the Pi.

### 8. (Optional) Give the MeshGate a fixed address

Find its MAC address in the leases file:

```bash
cat /var/lib/NetworkManager/dnsmasq-eth0.leases
```

Reserve `10.42.0.2` for it (replace the MAC with yours):

```bash
echo "dhcp-host=AA:BB:CC:DD:EE:FF,10.42.0.2,meshgate" | sudo tee /etc/NetworkManager/dnsmasq-shared.d/halow.conf
```

```bash
sudo nmcli con up lan
```

Power-cycle the MeshGate again. Its admin page is now at `http://10.42.0.2`.

## Verify

Check that both profiles are active (`eth0` → `lan`, `eth1` → `wan`):

```bash
nmcli device
```

Check that the Pi has internet through Starlink:

```bash
ping -c 3 8.8.8.8
```

List the devices that got addresses from the Pi:

```bash
cat /var/lib/NetworkManager/dnsmasq-eth0.leases
```

Then:

- A mesh device should get a `10.42.0.x` address and be able to browse the web.
- With Starlink off, mesh devices keep their addresses and can reach the Pi, but have no internet.
- When Starlink comes back on, internet returns without any changes.

## Notes

- **The Pi is required.** It's the only DHCP server, so if it's off, nothing on the mesh gets an address. Give it reliable power.
- **The clock can drift.** Without internet, the Pi can't sync its time after a reboot. Add an RTC (built in on Pi 5 with its battery) if you need accurate timestamps offline.
- **The dish stays reachable.** Starlink's own management page is still at `192.168.100.1` via `eth1`.
