# Unit watch — always-on install

Runs the same `watch.py` that runs on the Mac, on a server, every five minutes,
whether or not any laptop is awake.

## What you need

A small Ubuntu 22.04/24.04 box. **2 GB of RAM**, because Chrome is required to
read the Essex pages — Avalon is plain HTTP and would run anywhere.

Cheapest that comfortably works:

| Host | Spec | Cost |
|---|---|---|
| Hetzner CAX11 (ARM) | 2 vCPU / 4 GB | ~€3.29/mo |
| Hetzner CX22 | 2 vCPU / 4 GB | ~€4.50/mo |
| AWS Lightsail | 1 GB | $5/mo |
| Oracle Cloud Always Free | 4 ARM / 24 GB | free, if you can get capacity |

Bandwidth is about 20 GB/month, which every one of these includes many times over.

## Install

From this folder, on your machine:

    scp watch.py install.sh root@YOUR_SERVER_IP:/root/
    ssh root@YOUR_SERVER_IP
    UNITWATCH_TOPIC=your-private-topic bash install.sh

That is the whole thing. It installs Chromium, creates an unprivileged
`unitwatch` user, and registers a systemd timer.

The topic is required and is never stored in this repository. It is the only
secret in the project: anyone who knows it can read every alert or post fake
ones.

## Checking on it

    systemctl list-timers unitwatch.timer   # when it next runs
    journalctl -u unitwatch -f              # live log
    systemctl start unitwatch               # run one check right now
    cat /opt/unitwatch/state.json           # what it currently believes

## Turning the Mac copy off

Once the server is running, stop the local one so you are not notified twice:

    launchctl unload ~/Library/LaunchAgents/com.irakee.unitwatch.plist

## Changing the move-in window

`WINDOW_START` / `WINDOW_END` near the top of `watch.py`. Only Avalon is
filtered by it — Essex publishes no per-unit dates, so its alerts fire on any
increase in a floorplan's availability count.
