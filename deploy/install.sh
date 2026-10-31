#!/usr/bin/env bash
# Installs jellydash on the Pi: /opt/jellydash, a system user, sudoers + udev rules, systemd unit.
# Run from the repo on the Pi:  sudo ./deploy/install.sh
set -euo pipefail

INSTALL_DIR=/opt/jellydash
CONFIG_DIR=/etc/jellydash
SERVICE_USER=jellydash
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
	echo "run with sudo: sudo $0" >&2
	exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
	echo "python3 >= 3.11 is required (Ubuntu 24.04 ships 3.12)" >&2
	exit 1
fi

echo "==> service user and groups"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
	useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in i2c gpio; do
	if ! getent group "$group" >/dev/null; then
		groupadd --system "$group"
	fi
done
for group in video i2c gpio docker; do
	if getent group "$group" >/dev/null; then
		usermod -aG "$group" "$SERVICE_USER"
	fi
done

echo "==> files"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
rm -rf "$INSTALL_DIR/jellydash"
cp -r "$SOURCE_DIR/jellydash" "$INSTALL_DIR/"
find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
python3 -m compileall -q "$INSTALL_DIR/jellydash"
chown -R root:root "$INSTALL_DIR"
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
	cp "$SOURCE_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
	echo "    wrote $CONFIG_DIR/config.toml (edit it to match your UPS HAT and services)"
fi

echo "==> sudoers (smartctl and tailscale status only)"
install -m 0440 "$SOURCE_DIR/deploy/sudoers-jellydash" /etc/sudoers.d/jellydash
visudo -cf /etc/sudoers.d/jellydash >/dev/null

echo "==> udev rules for i2c, gpio and vcio"
install -m 0644 "$SOURCE_DIR/deploy/99-jellydash.rules" /etc/udev/rules.d/99-jellydash.rules
udevadm control --reload-rules || true
udevadm trigger || true

echo "==> systemd"
install -m 0644 "$SOURCE_DIR/deploy/jellydash.service" /etc/systemd/system/jellydash.service
systemctl daemon-reload
systemctl enable jellydash >/dev/null
systemctl restart jellydash
sleep 2
systemctl --no-pager --lines=5 status jellydash || true

PORT=$(grep -E '^port *= *' "$CONFIG_DIR/config.toml" | head -1 | sed -E 's/.*= *([0-9]+).*/\1/')
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
echo
echo "jellydash is up on port ${PORT:-8585}"
if [[ -n "$TS_IP" ]]; then
	echo "  over tailscale:  http://$TS_IP:${PORT:-8585}"
	echo "  or run:          sudo tailscale serve --bg ${PORT:-8585}   (HTTPS at your MagicDNS name)"
fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
	echo
	echo "ufw is active and will block port ${PORT:-8585}. Either:"
	echo "  tailnet only:   sudo ufw allow in on tailscale0 to any port ${PORT:-8585} proto tcp"
	LAN_SUBNET=$(ip -4 route 2>/dev/null | awk '/proto kernel/ && /src/ {print $1; exit}')
	echo "  LAN as well:    sudo ufw allow from ${LAN_SUBNET:-<lan-subnet>/24} to any port ${PORT:-8585} proto tcp"
	echo "  or no rule at all: set bind = \"127.0.0.1\" in $CONFIG_DIR/config.toml and use tailscale serve"
fi
for tool in smartctl gpioget; do
	if ! command -v "$tool" >/dev/null; then
		echo "  optional: apt install $( [[ $tool == smartctl ]] && echo smartmontools || echo gpiod )   (for $tool)"
	fi
done
