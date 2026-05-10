# Kiln Controller — Changes & Upgrade Guide

This document describes the round of improvements applied to the
`kiln-controller` project on top of the upstream
`jbruce12000/kiln-controller`. It is the canonical reference for what
changed, why it changed, what new files exist, and how to install /
upgrade a Raspberry Pi running the controller.

> **Safety reminder.** A kiln controller is a piece of safety-critical
> hardware. Software cannot save you from a stuck SSR, a melted
> thermocouple, or an under-rated wire. Keep a kiln-sitter or external
> high-limit cutoff in line with the controller. Test every change on
> the simulator (`config.simulate = True`) before connecting to a
> real kiln.

---

## 1. What changed (summary)

| # | Area | Improvement | Status |
|---|------|-------------|--------|
| 1 | Safety | Heating-element failure detection | Implemented |
| 2 | Safety | Derivative-spike clamp + low-pass on PID | Implemented |
| 3 | Safety | Cool-down monitoring + safe-to-open notification | Implemented |
| 4 | Safety | Multi-thermocouple averaging + hot/cold-zone delta alert | Implemented |
| 5 | Quality | Hold-phase auto-extension (clock pauses if not at temp) | Implemented |
| 6 | Quality | Heat-rate logging + SQLite firing history export | Implemented |
| 7 | Quality | Locked active-profile snapshot at run start | Implemented |
| 8 | Usability | Firing history UI (browse past firings, sample table) | Implemented |
| 9 | Usability | Email / Pushover / ntfy / Slack notifications | Implemented |
| 10 | Usability | Orton ramp/hold (°/hr) profile importer | Implemented |
| 11 | Ops | HTTP Basic authentication on `/api`, `/control`, `/storage` | Implemented |
| 12 | Ops | Config UI in the web interface (PID, safety, costs) | Implemented |
| — | Hygiene | `.gitignore` merge conflict fixed; new artifacts ignored | Fixed |
| — | Hygiene | `requirements.txt` pinned + reorganised | Updated |

Every feature is **opt-in** via flags in `config.py`. The default
behavior of the running controller is the same as before unless you
turn a feature on.

---

## 2. New / changed files

```
config.py                      # heavily expanded with new tunables
kiln-controller.py             # new endpoints, auth wiring, history wiring
requirements.txt               # pinned versions, optional cryptography
.gitignore                     # merge conflict cleaned + new artifacts

lib/oven.py                    # major refactor — see §3
lib/auth.py                    # NEW: PBKDF2 HTTP-Basic auth
lib/history.py                 # NEW: SQLite firing history
lib/notifications.py           # NEW: email/Pushover/ntfy/Slack dispatcher
lib/profile_importer.py        # NEW: Orton ramp/hold ↔ waypoint conversion

public/index.html              # toolbar buttons + 3 new modals
public/assets/js/picoreflow.js # frontend handlers for new modals/endpoints

CHANGES.md                     # this file
```

State / data files (created at runtime):

```
state.json                     # automatic-restart state (existing behaviour)
locked_profile.json            # NEW (#7): immutable copy of the running profile
auth.json                      # NEW: hashed credentials when auth_enabled=True
storage/history.db             # NEW (#8): SQLite firing history
```

---

## 3. Feature deep-dive

### #1 — Heating-element failure detection
**Where:** `lib/oven.py: ElementFailureDetector`, called from
`Oven.reset_if_emergency()`.

A failing kiln element (open coil, blown fuse, tripped breaker, SSR
that won't close) shows up to the controller as: PID screaming for
full duty, but the temperature isn't climbing. The detector watches
that exact pattern.

**Logic.** When PID output is ≥ 95 %, current temp is above
`element_failure_min_temp`, and we're outside the normal control
window, start a stopwatch. If the stopwatch reaches
`element_failure_min_full_duty_seconds` (default 240s = 4 min) and
the smoothed `heat_rate` is still below
`element_failure_min_heat_rate` (default 80 °/hr), the run is
aborted as `outcome = "emergency"` and a notification is dispatched.

**Disable:** `element_failure_detection = False`.

### #2 — Derivative-spike limiter on PID
**Where:** `lib/oven.py: PID._filter_d`.

A noisy thermocouple reading produces a one-cycle blip in `error`,
which the derivative term turns into a giant `dErr` and slams into
the relay for one duty cycle. The fix: clamp `dErr` to ±
`pid_d_spike_limit` (deg/sec) and apply a first-order low-pass
filter `(alpha = pid_d_filter_alpha)` so the d-term acts on the
trend, not on a single sample.

**Disable:** `pid_d_spike_limit_enabled = False`.

### #3 — Cool-down monitoring + safe-to-open alert
**Where:** `lib/oven.py: Oven.cool_down_check()`, runs in the IDLE
loop after a firing ends.

When a firing ends (completed or aborted) the kiln is still very
hot. The controller now keeps polling temperature, and when it
drops below `cool_down_safe_open_temp` (default 150 °) it fires the
"safe to open" notification. Disabling: set to `None` or set
`cool_down_notify_on_safe_open = False`.

There is also a `cool_down_notify_on_complete` notification when a
schedule finishes successfully.

### #4 — Multiple thermocouple support
**Where:** `lib/oven.py: RealBoard, Oven.reset_if_emergency()`.

Set `multiple_thermocouples = True` and define `spi_cs_2` to a
second CS pin to enable a second thermocouple on the same SPI bus.
The control loop continues to use the *primary* probe for closed
loop control, but the difference between primary and secondary is
recorded in `state.tc_delta`. If the delta crosses
`multi_tc_delta_alert_degrees` (default 50) — i.e. there is a
sustained hot/cold zone or a probe is failing — an emergency
notification is dispatched.

> **Hardware note.** Both probes share the SPI clock/MISO/MOSI
> wires, but each needs its own CS GPIO and its own MAX31855/856
> breakout. Pick the second CS pin so it isn't the relay pin. The
> second probe is started on a separate thread so a probe crash
> never takes the primary down.

### #5 — Hold-phase auto-extension
**Where:** `lib/oven.py: Profile.is_in_hold()` +
`Oven.kiln_must_catch_up()`.

The original "kiln must catch up" logic shifts the schedule clock
forward when the kiln is too far below target. That works for ramps
but is wrong for a hold: if you ask for a 30-minute hold at 250 °C
and the kiln only reaches 250 °C after 25 minutes of the hold has
elapsed, the original logic would still finish the hold 5 minutes
later, having only held for those 5 minutes.

The new behavior: while the schedule says we're in a flat segment
(slope < 0.01 °/s ≈ 36 °/hr) and the kiln has not yet reached the
hold target within `hold_at_temp_tolerance` degrees, the runtime
clock is paused at the start of the hold. As soon as the kiln
arrives, the clock resumes — guaranteeing the *full* hold at temp.

**Disable:** `hold_auto_extend = False`.

### #6 — Heat-rate logging + firing-data export
The previous `Oven` already computed a smoothed `heat_rate`. The new
SQLite history (#8) writes that value alongside target, actual,
heat duty, and PID output every `history_sample_interval_seconds`
(default 30s). This is the "atmosphere / rate-of-change" log; it is
queryable via `/api/history/<id>` and can be exported as JSON for
post-firing analysis.

### #7 — Locked active profile copy
**Where:** `lib/oven.py: Oven._lock_profile_to_disk()`.

When a firing starts:
1. The live `Profile` object is replaced with a deep copy of the
   waypoints, so editing `obj.data` in place can't affect it.
2. That deep copy is written to `locked_profile.json` (path from
   `config.locked_profile_file`).
3. Automatic-restart now prefers the locked snapshot over the
   source profile JSON, so a power outage cannot accidentally pick
   up edits the user made to the source profile mid-firing.

The locked file is overwritten at the start of every new firing.

### #8 — Firing history UI
**Where:** `lib/history.py`, `kiln-controller.py:/api/history*`,
`public/assets/js/picoreflow.js: loadHistory/showFiring/deleteFiring`.

Database lives at `storage/history.db`. Schema:

* `firings(id, started_at, ended_at, profile_name, profile_json,
  outcome, peak_temp, total_cost, currency_type, notes)`
* `samples(firing_id, runtime_s, target_temp, actual_temp, heat,
  heat_rate, pid_out)` — one row per
  `history_sample_interval_seconds`.

UI: clock icon in the toolbar opens the History modal with a list of
recent firings. Click *View* to see per-sample detail. *Delete*
removes a firing.

A 13-hour firing at 30s sample interval ≈ 1500 rows ≈ 75 KB. Pi SD
cards have plenty of room.

**Disable:** `history_enabled = False`.

### #9 — Email / push notifications
**Where:** `lib/notifications.py`.

Triggers (each notification is sent on every enabled backend):

* `notify_run_complete(state)` — when a profile finishes successfully
* `notify_safe_to_open(state)` — when temp drops below safe threshold
* `notify_emergency(reason, state)` — over-temp, element failure,
  TC errors, hot-zone delta alert.

Backends are toggled by `notify_<backend>_enabled` flags. Network
calls happen on a daemon thread, so a flaky upstream never blocks
the control loop. A failure in one backend is logged but never
propagated.

| Backend | Type | Setup |
|---------|------|-------|
| Email | SMTP/STARTTLS | Gmail "app password" recommended |
| Pushover | mobile push | https://pushover.net + app token |
| ntfy | mobile push | https://ntfy.sh — totally free, anonymous |
| Slack | incoming webhook | works in parallel with `watcher.py` |

### #10 — Orton ramp-rate profile importer
**Where:** `lib/profile_importer.py`,
`POST /api/profile/import`,
toolbar button → `#ortonImportModal` in the UI.

Most kiln-firing recipes are written in *Orton* form:
"ramp at 110 °C/hr to 120 °C, hold 30 min, ramp at 220 °C/hr to
1040 °C, ramp at 60 °C/hr to 1220 °C, hold 10 min, fast cool to
815 °C…". The importer accepts this in JSON form:

```json
{
  "name": "cone-6-glaze",
  "start_temp": 20,
  "segments": [
    {"type": "ramp", "rate": 110, "target": 120},
    {"type": "hold", "minutes": 30},
    {"type": "ramp", "rate": 220, "target": 1040},
    {"type": "ramp", "rate": 60, "target": 1220},
    {"type": "hold", "minutes": 10},
    {"type": "cool", "rate": 9999, "target": 815},
    {"type": "ramp", "rate": 55, "target": 815}
  ]
}
```

`rate` is degrees C per **hour**. A rate ≥ 9000
is treated as "as fast as the kiln can manage" — a near-vertical
segment in the waypoint output. Use that for cooling segments where
you want unrestricted radiant cooling.

The reverse function `waypoints_to_orton` is also available for
showing imported profiles back as ramp/hold tables in future UI
work.

### #11 — Authentication on /api, /control, /storage
**Where:** `lib/auth.py`,
`kiln-controller.py: _check_auth_or_401, get_websocket_from_request`.

`auth_enabled = True` activates HTTP Basic on every mutating
endpoint **and** every websocket. Browsers cannot put an
`Authorization` header on a WebSocket open, so the websockets also
accept a `?token=<base64(user:pass)>` query param.

Storage:
* `auth.json` is created on first run from `auth_username` /
  `auth_initial_password` (PBKDF2-HMAC-SHA256, 200 000 iterations,
  16-byte salt).
* `POST /api/auth/change-password` (or just edit `auth.json` and
  delete the hash) rotates the password.

> **Important.** Once you set `auth_enabled = True` and confirm the
> initial password works, set `auth_initial_password` to an empty
> string in `config.py` so the cleartext does not sit in source
> control. The hash in `auth.json` is what is actually used.

### #12 — Config UI in the web interface
**Where:** `kiln-controller.py: /api/config`,
`public/assets/js/picoreflow.js: loadSettings/saveSettings`,
`#settingsModal` in `index.html`.

A cog icon in the toolbar opens the Settings modal. The modal lists
a curated subset of `config.py` keys (`EDITABLE_CONFIG_KEYS` in
`kiln-controller.py`) grouped into PID tuning / cost / safety /
notifications.

**Lifetime.** Edits made in the UI live in the running process —
they are not written back to `config.py`. To persist them across
restarts, copy the new values into `config.py`. (The intent here is
to let you tune PID gains *during* a firing without dropping into
SSH; permanent operational values still belong in source.)

Every endpoint that mutates state is guarded by the auth wrapper, so
remote config edits require credentials.

---

## 4. Configuration reference (new flags only)

These were added to `config.py`. Unmentioned settings still behave
as before. Defaults shown.

```python
# Server
listening_host                            = "0.0.0.0"

# Multi-thermocouple (#4)
multiple_thermocouples                    = False
# spi_cs_2                                = board.D6   # set if needed
multi_tc_delta_alert_degrees              = 50

# Derivative-spike limiter (#2)
pid_d_spike_limit_enabled                 = True
pid_d_spike_limit                         = 1.0   # deg/sec
pid_d_filter_alpha                        = 0.4   # 0..1

# Hold-phase auto-extension (#5)
hold_auto_extend                          = True
hold_at_temp_tolerance                    = 5

# Element-failure detection (#1)
element_failure_detection                 = True
element_failure_min_full_duty_seconds     = 240
element_failure_min_heat_rate             = 80
element_failure_min_temp                  = 200

# Cool-down monitoring (#3)
cool_down_safe_open_temp                  = 150
cool_down_notify_on_complete              = True
cool_down_notify_on_safe_open             = True

# Locked profile snapshot (#7)
locked_profile_file                       = "<repo>/locked_profile.json"

# Firing history (#8)
history_enabled                           = True
history_db_path                           = "<repo>/storage/history.db"
history_sample_interval_seconds           = 30

# Notifications (#9)
notify_email_enabled                      = False
notify_email_to                           = ["you@example.com"]
notify_email_from                         = "kiln@example.com"
notify_email_smtp_host                    = "smtp.gmail.com"
notify_email_smtp_port                    = 587
notify_email_smtp_user                    = ""
notify_email_smtp_pass                    = ""
notify_email_use_tls                      = True
notify_pushover_enabled                   = False
notify_pushover_user_key                  = ""
notify_pushover_app_token                 = ""
notify_ntfy_enabled                       = False
notify_ntfy_topic                         = "kiln-controller-yourtopic"
notify_ntfy_server                        = "https://ntfy.sh"
notify_slack_enabled                      = False
notify_slack_webhook_url                  = ""

# Auth (#11)
auth_enabled                              = False
auth_username                             = "admin"
auth_initial_password                     = "kiln"   # CHANGE BEFORE ENABLING
auth_file                                 = "<repo>/auth.json"
```

---

## 5. New / updated HTTP API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/stats` | no | PID stats snapshot (unchanged) |
| POST | `/api` | yes | run / stop / memo / stats commands |
| GET | `/api/config` | yes | dump current editable config |
| POST | `/api/config` | yes | mutate any subset of `EDITABLE_CONFIG_KEYS` |
| GET | `/api/history` | yes | list recent firings (param `limit=50`) |
| GET | `/api/history/<id>` | yes | full firing detail with samples |
| DELETE | `/api/history/<id>` | yes | delete a firing |
| POST | `/api/profile/import` | yes | Orton spec → waypoint profile (saves) |
| POST | `/api/auth/change-password` | yes | rotate password |

WebSockets (`/control`, `/storage`, `/config`, `/status`) all
require auth when enabled. Browsers must pass `?token=` since they
can't set Authorization on the open handshake.

---

## 6. Raspberry Pi installation guide

These steps assume Raspberry Pi OS Bookworm (32-bit or 64-bit) on a
Pi 3 / 4 / 5 / Zero 2 W. SD card 8 GB+. The same steps work on
Ubuntu Server.

### 6.1 Flash the OS

1. Download the latest Raspberry Pi Imager:
   <https://www.raspberrypi.com/software/>
2. Imager → choose "Raspberry Pi OS (64-bit)" or "Raspberry Pi OS
   Lite (64-bit)" → choose your SD card → click the gear icon and:
   * set hostname (e.g. `kiln`)
   * enable SSH
   * set username/password
   * configure Wi-Fi (or use Ethernet)
3. Write and boot the Pi.

### 6.2 Wire up the hardware

| RPi pin (BCM) | MAX31855/856 | Notes |
|---------------|--------------|-------|
| 3.3V | Vin | |
| GND | GND | |
| SCLK (BCM 11 = phys 23) | CLK | HW SPI; `spi_sclk` in config if SW |
| MISO (BCM 9  = phys 21) | DO  | HW SPI; `spi_miso` in config if SW |
| BCM 5 (or any free GPIO) | CS | `spi_cs` in config |

Relay drive pin (default `gpio_heat = D23`) goes to a transistor +
pull-up that drives the SSR. The original README's schematic still
applies. **If you want a second thermocouple (#4),** add a second
breakout to the same SPI bus and pick a free GPIO for `spi_cs_2`.

### 6.3 Install the controller

```bash
sudo apt-get update
sudo apt-get -y full-upgrade
sudo apt-get -y install git python3-venv python3-pip

git clone https://github.com/sam-richardson/kiln-controller.git
cd kiln-controller

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

# Enable SPI
sudo raspi-config nonint do_spi 0
```

The `pip install` step will pull in `bottle`, `gevent`,
`gevent-websocket`, `requests`, `RPi.GPIO`, the Adafruit blinka
drivers, and the MAX31855/MAX31856 + bitbangio modules.

### 6.4 First-boot configuration

1. `cp config.py config.py.bak` (always keep a known-good copy)
2. Edit `config.py`:
   * `simulate = True` initially — you should run a full firing in
     simulation before connecting hardware.
   * `kwh_rate`, `kw_elements`, `currency_type`
   * thermocouple pins if you're using software SPI
   * `emergency_shutoff_temp` — set this **below** anything that
     would melt elements/wiring; for cone 7 firings, 1300 °C
     is a reasonable hard stop.
   * leave the new safety features at their defaults to start; tune
     after a successful test firing.

### 6.5 Smoke test

```bash
source venv/bin/activate

# Test the thermocouple alone
./test-thermocouple.py
# expected: prints temperature once a second; values change when you
# warm the probe with your hand

# Test the SSR drive line (will toggle the relay!)
./test-output.py

# Test the full server in simulation
./kiln-controller.py
# Open http://<pi-ip>:8081 in a browser, pick a profile, click Start.
# The graph should follow the schedule.
```

### 6.6 Run as a service

The repo includes a systemd unit at
`lib/init/kiln-controller.service`. Adjust the path inside it if
you cloned somewhere other than `/home/pi/kiln-controller`, then:

```bash
sudo cp lib/init/kiln-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kiln-controller
sudo journalctl -u kiln-controller -f
```

The included helper script `start-on-boot` performs the copy and
enable in one step.

### 6.7 Enable authentication

When the Pi is on the same Wi-Fi as the rest of the house, the
default unauthenticated controller lets anyone start or stop a
firing. To lock it down:

```bash
# stop the service so we can edit config safely
sudo systemctl stop kiln-controller

# edit config.py
nano config.py
#   auth_enabled = True
#   auth_username = "potter"
#   auth_initial_password = "<a strong password>"

# delete any stale auth.json so the new password takes effect
rm -f auth.json

sudo systemctl start kiln-controller
```

Visit the UI; the browser prompts for credentials. Once it works,
edit `config.py` again and set `auth_initial_password = ""` so the
cleartext stops living in source control. The hashed copy in
`auth.json` (mode 0600) is what the running process actually uses.

### 6.8 Enable notifications (optional)

Pick whichever backends you want and edit `config.py`. The simplest
free path is **ntfy**:

```python
notify_ntfy_enabled = True
notify_ntfy_topic   = "<some-random-string-only-you-know>"
notify_ntfy_server  = "https://ntfy.sh"
```

Install the **ntfy** Android/iOS app, subscribe to the same topic,
and you'll get push messages on firing complete / safe-to-open /
emergency.

For email through Gmail you must use an "App password" (Google
account → Security → 2-step verification → App passwords). Plug it
into `notify_email_smtp_pass` and leave the rest at defaults.

### 6.9 Enable a second thermocouple (optional, #4)

Wire a second MAX31855 (or 31856) with its **own CS pin**, then in
`config.py`:

```python
multiple_thermocouples       = True
spi_cs_2                     = board.D6
multi_tc_delta_alert_degrees = 50
```

Restart the controller. Both probes are read every duty cycle; the
primary still drives PID, but `state.tc_delta` shows the spread
and an emergency notification fires when it exceeds the threshold.

### 6.10 Upgrading from an older clone

```bash
cd kiln-controller
git pull
source venv/bin/activate
pip install -r requirements.txt          # picks up new pins
sudo systemctl restart kiln-controller
```

Your existing `config.py` will keep working — every new setting has
a default, and missing settings are accessed via `getattr(...,
default)` so old configs are forward compatible. To use any new
feature, copy the relevant block from the new `config.py` template
into your own.

---

## 7. Migration notes for forked installs

If you maintain a fork of this repo:

* `lib/oven.py` was substantially refactored. Most public methods
  (`run_profile`, `abort_run`, `get_state`, `automatic_restart`)
  keep their signatures, but `abort_run` now accepts
  `outcome="aborted|completed|emergency"` and `reason=""`. Old
  zero-arg call sites are still supported.
* `Oven.get_state()` now includes `tc_temp_2`, `tc_delta`, and
  `cool_down_armed`. Anyone consuming the websocket payload should
  ignore unknown keys.
* `Profile` gained `is_in_hold(time_s)`. Backwards compatible.
* `PID.compute()` keeps the same signature; new keys appear in
  `pidstats`.
* New runtime artifacts (`auth.json`, `locked_profile.json`,
  `storage/history.db`) are git-ignored.
