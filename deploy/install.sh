#!/usr/bin/env bash
#
# Puts the unit watcher on a fresh Ubuntu/Debian host and starts it.
#
#   UNITWATCH_TOPIC=your-private-topic sudo -E bash install.sh
#
# The topic is never written into this file or the repository — it is the one
# secret here, and anyone holding it can read every alert.
#
# Safe to run again — it replaces what it installed last time.
set -euo pipefail

APP_DIR=/opt/unitwatch
SERVICE_USER=unitwatch

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# chromium is the only heavy thing here; Essex cannot be read without a browser.
apt-get install -y -qq python3 curl chromium ca-certificates fonts-liberation >/dev/null

# Ubuntu ships chromium as a snap on some releases, which cannot run headless
# from a system service. Fall back to the .deb from the chromium PPA if so.
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "==> chromium missing, trying google-chrome-stable"
  curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb
  apt-get install -y -qq /tmp/chrome.deb >/dev/null
fi

# A 1GB free-tier box has no swap by default, and Chrome will be killed the
# first time a page is heavy. One gigabyte of swap costs nothing and turns a
# hard failure into a slow check.
if [ "$(free -m | awk '/^Swap:/{print $2}')" = "0" ] && [ ! -f /swapfile ]; then
  echo "==> adding 1G swap (none present)"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> user and directory"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$APP_DIR"
install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" watch.py "$APP_DIR/watch.py"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "==> service"
cat >/etc/systemd/system/unitwatch.service <<UNIT
[Unit]
Description=Check Bellevue apartment availability
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=UNITWATCH_TOPIC=${UNITWATCH_TOPIC:?set UNITWATCH_TOPIC when running install.sh}
ExecStart=/usr/bin/python3 $APP_DIR/watch.py
# Chrome is the only thing here that can misbehave; cap it rather than let it
# take the box down.
# Sized for the smallest free-tier boxes. Chrome is the only thing here that
# can misbehave, and on a 1GB host an unbounded render takes the machine with
# it rather than just failing one check.
MemoryMax=700M
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/unitwatch.timer <<'UNIT'
[Unit]
Description=Run the apartment check every five minutes

[Timer]
# One minute after boot, then every five minutes from the end of the last run,
# so a slow run never overlaps the next one.
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now unitwatch.timer >/dev/null

echo "==> first run"
systemctl start unitwatch.service || true
sleep 2

echo
echo "Installed. Useful commands:"
echo "  systemctl list-timers unitwatch.timer   # when it next runs"
echo "  journalctl -u unitwatch -f              # what it is seeing"
echo "  systemctl start unitwatch               # run one check now"
