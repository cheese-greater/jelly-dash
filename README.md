# jellydash

A single-page, btop-flavoured diagnostics dashboard for a Raspberry Pi 5 home server,
in the Dracula palette. Python standard library only: no pip, no node, nothing to build.

It shows, refreshing every two seconds:

- **power**: mains vs battery from the UPS through NUT (Network UPS Tools): charge, runtime
  left, load, mains and battery voltage, replace-battery and overload flags, self-test result.
  UPS HATs on I2C work too. Plus the Pi's own PSU input voltage and negotiated current limit,
  and firmware throttle flags (under-voltage, throttling, frequency cap, temperature limit)
- **cpu / mem**: per-core bars, history graphs, frequency, temperature, load, swap
- **net**: default interface, Wi-Fi SSID and signal strength (dBm, quality bar, bitrates),
  Ethernet link speed, addresses, rx/tx graph, every other interface including tailscale0
- **tailscale**: backend state, node name and IPs, DERP relay, exit node, health warnings,
  peers with direct vs relayed path, and who is viewing the page right now
- **services**: Jellyfin, SearXNG and Tailscale (systemd unit or docker container, HTTP
  health probe, version, uptime, latency, memory, restarts); add more in the config
- **disks**: every mounted block device with usage bar, model, transport, temperature,
  SMART health and wear, read/write rates and a graph
- **proc**: top processes by CPU share

## Install on the Pi (Ubuntu Server 24.04)

From this machine, with the Pi reachable over SSH:

```sh
./deploy/push.sh pi-user@pi-host
```

Put `pi-user@pi-host` in `deploy/target` (gitignored) to drop the argument next time. The
script copies the repo to `~/jelly-dash` on the Pi and runs `sudo ./deploy/install.sh`, which:

1. creates a `jellydash` system user in the `video`, `i2c`, `gpio` and `docker` groups
2. copies the code to `/opt/jellydash` and the config to `/etc/jellydash/config.toml`
3. installs a sudoers rule allowing only `smartctl` and `tailscale status --json`
4. installs udev rules so the service user can read I2C, GPIO and the firmware mailbox
5. enables and starts `jellydash.service` on port 8585

Optional packages on the Pi:

```sh
sudo apt install smartmontools gpiod   # SMART health; GPIO power-loss pin for Geekworm UPS HATs
```

### Keeping the SD card quiet

On a stock Ubuntu Server install the Pi wrote about 4.5 GB a day to the card while idle,
nearly all of it logging. The log volume itself is tiny; the cost is journald's persistent
binary journal, where every message dirties several scattered pages and ext4 then commits a
transaction for each flush. Two changes bring it down to roughly 200 MB a day:

```sh
# journal in RAM; rsyslog keeps the plain-text copy in /var/log/syslog across reboots
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=64M\n' | sudo tee /etc/systemd/journald.conf.d/sdcard.conf
sudo systemctl restart systemd-journald

# batch ext4 journal commits (60 s instead of 5 s) and skip access-time updates
sudo sed -i -E 's|^(LABEL=writable\s+/\s+ext4\s+)defaults(\s)|\1defaults,noatime,commit=60\2|' /etc/fstab
sudo mount -o remount,commit=60,noatime /
sudo systemctl daemon-reload
```

The 60 s commit interval means up to a minute of filesystem changes can be lost on a hard
power cut, which the UPS covers. To check the write rate afterwards:

```sh
a=$(awk '$3=="mmcblk0"{print $10}' /proc/diskstats); sleep 60; b=$(awk '$3=="mmcblk0"{print $10}' /proc/diskstats); echo "$(( (b-a)*512/1024 )) KB written in 60 s"
sudo iotop -b -a -o -P -d 60 -n 2      # apt install iotop; which processes wrote, over one minute
```

Jellyfin keeps its databases, metadata and transcode segments on the card under
`/var/lib/jellyfin` and `/var/cache/jellyfin`. Day to day that is a trickle, but transcoded
playback writes the whole stream as segments, so if transcoding is common move the
transcoding path (Dashboard, Playback, Transcoding) to the NVMe.

### Reaching it over Tailscale

The service binds to all interfaces by default, so it is already reachable at
`http://<pi tailscale ip>:8585` or `http://<pi>.<tailnet>.ts.net:8585` from any device on
your tailnet. The LAN can reach it too.

Recommended: let Tailscale front it with HTTPS and identity. Set `bind = "127.0.0.1"` in
`/etc/jellydash/config.toml`, restart, then on the Pi:

```sh
sudo tailscale serve --bg 8585
```

The dashboard is then at `https://<pi>.<tailnet>.ts.net`, only inside the tailnet, and the
tailscale box shows which tailnet user and device is viewing it. If port 443 is already in
use by another `tailscale serve`, pick a different HTTPS port: `sudo tailscale serve --bg --https=8443 8585`.

To share port 443 with other services by path instead, mount the dashboard under one and
tell it so (the bare LAN URL keeps working):

```sh
# in /etc/jellydash/config.toml:  base_path = "/jellydash"
sudo systemctl restart jellydash
sudo tailscale serve --bg --set-path /jellydash 8585
sudo tailscale serve --bg --set-path /searxng 80       # nginx's /searxng, for example
```

## Running `update -r` from the dashboard

The top bar has an `update -r` button. It opens a dialog that shows the exact command,
asks for confirmation, then streams the output live, with colors and progress bars, the
way it looks in a terminal. The button pulses while the update runs, a reload mid-update
reattaches to the output, and the dialog says so if the connection drops because the
update reboots the Pi.

Two things on the Pi make it work:

1. `update` has to be an executable on the sudo path. Check with `which update`; if it is a
   shell function or alias, save it as `/usr/local/bin/update` and `chmod +x` it.
2. The sudoers rule in `deploy/sudoers-jellydash` (installed by `install.sh`) lets the
   `jellydash` user run exactly `/usr/local/bin/update -r` as root without a password. If
   the script lives elsewhere, change that line and the `command` in the config.

The command runs in a real terminal, so if it stops at a prompt (SearXNG's updater asks
what to do with a changed `settings.yml`, apt asks to continue), type the answer in the
input line under the output and press Enter, exactly as you would in SSH. Enter on its own
sends a bare newline, `^C` sends Ctrl-C, and `stop` terminates the whole process group.
To make such prompts go away entirely, export `FORCE_TIMEOUT=10` in your update script
before it calls SearXNG's `utils/searxng.sh`: its menus then pick their default after ten
seconds, and apt accepts `-y`.

More buttons: add another `[[actions]]` entry to the config, each with its own sudoers line.
Starting an action needs a same-origin browser request with a custom header, so a random
website you visit cannot trigger it, and only one run per action happens at a time.
Avoid restarting the jellydash service while an update runs: the update is its child.

## Install it as an app

The page is a Progressive Web App: a manifest, icons and a small service worker that caches
the page shell, so the installed app opens instantly and shows DISCONNECTED when the Pi is
unreachable instead of a browser error. Live data is never cached.

Browsers only offer installation on HTTPS, so use the `tailscale serve` setup above and open
`https://<pi>.<tailnet>.ts.net`. Then:

- **Chrome / Edge / Brave on desktop and Android**: click the `install app` button in the
  top bar (it appears once the browser deems the page installable), or the install icon in
  the address bar.
- **iPhone / iPad (Safari)**: Share, then "Add to Home Screen". It opens full screen with
  the Dracula status bar.
- **Firefox on Android**: menu, then "Install" or "Add to Home screen".

Over plain `http://100.x.y.z:8585` the page works exactly the same in a tab, but there is no
install prompt and no offline shell, since neither is allowed on an insecure origin.

## Configuration

See [config.example.toml](config.example.toml); every key has a comment. The things most
likely to need a change:

- **UPS**: `source = "nut"` reads the UPS from upsd on `localhost:3493` using NUT's own
  TCP protocol, so nothing beyond your existing NUT setup is needed. It takes the first UPS
  that upsd lists; set `nut_ups` to pin the name from `/etc/nut/ups.conf`. If upsd only
  listens for `LISTEN 127.0.0.1` that is enough. No login is needed for read-only queries
  (same as `upsc`); `nut_username` / `nut_password` exist for the rare upsd that insists. (`auto` tries NUT first, then kernel
  batteries, then I2C fuel-gauge HATs such as Geekworm X120x or Waveshare INA219 boards.)
- **services**: `url` can be a list and the first that answers wins; SearXNG defaults to
  `http://127.0.0.1/searxng/healthz` behind a reverse proxy, then the bare port 8080. If
  your proxy only listens on the LAN address, put that address in the list. Entries with
  `optional = true` (NUT, nginx, Caddy, Docker) only appear when they exist.
- **bind / port**: as above.

Restart after editing: `sudo systemctl restart jellydash`. Logs: `journalctl -u jellydash -f`.

## Run it locally

```sh
python3 -m jellydash            # http://127.0.0.1:8585 with defaults
JELLYDASH_CONFIG=config.example.toml JELLYDASH_PORT=9000 python3 -m jellydash -v
```

On a desktop most Pi-specific readings simply report as unavailable; the page still renders.

## Layout

- `jellydash/server.py`: threaded HTTP server; `/` serves the page, `/api/status` the JSON
- `jellydash/sampler.py`: background threads (fast loop for cpu/mem/net/disk/power/processes,
  slow loop for tailscale, services and SMART) plus the ring-buffer history for the graphs
- `jellydash/collectors/`: one module per panel, all read-only and failure-tolerant
- `jellydash/static/`: the page (`index.html`, `style.css`, `app.js`); no dependencies
- `deploy/`: systemd unit, sudoers, udev rules, installer and push script
