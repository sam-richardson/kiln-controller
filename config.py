import logging
import os

########################################################################
#
#   General options
#
### Logging
log_level = logging.INFO
log_format = '%(asctime)s %(levelname)s %(name)s: %(message)s'

### Server
listening_host = "0.0.0.0"   # bind address. 0.0.0.0 = all interfaces (LAN)
listening_port = 8081

########################################################################
# Cost Information
# Used for both pre-run estimates and live cost tracking. My kiln has
# three elements that consume 9460W on high.
kwh_rate        = 0.27    # cost per kilowatt hour in your currency
kw_elements     = 9.460   # kiln element wattage (kilowatts) when fully on
currency_type   = "$"     # currency symbol shown in the UI

########################################################################
#
# Hardware Setup (uses BCM Pin Numbering)
#
# kiln-controller uses the SPI interface from the blinka library to read
# temperature data from a MAX31855 or MAX31856 thermocouple breakout.
# Blinka supports many boards. Tested on Raspberry Pi.
#
# Hardware SPI vs Software SPI
# - HW SPI: faster, fixed pins on RPi (SPI0_SCLK / SPI0_MISO / SPI0_MOSI),
#   no SPI pins listed in this file.
# - SW SPI: slower (fine for thermocouples), any GPIO pins, list them here.
#
# SPI is autodetected: if spi_sclk / spi_mosi / spi_miso are set below,
# software SPI is used. Otherwise the code falls back to board.SPI().
########################################################################

try:
    import board
    spi_sclk  = board.D15  # spi clock
    spi_miso  = board.D11  # spi Microcomputer In Serial Out
    spi_cs    = board.D13  # spi Chip Select for the primary thermocouple
    spi_mosi  = board.D10  # spi Microcomputer Out Serial In (not connected for tc)
    gpio_heat = board.D23  # output that controls the relay
except (NotImplementedError, AttributeError, ImportError):
    print("not running on a blinka-recognized board, probably a simulation")

#######################################
### Thermocouple breakout boards
#######################################
#   max31855 - K type only
#   max31856 - many thermocouple types
max31855 = 1
max31856 = 0
# Uncomment if using MAX-31856:
# import adafruit_max31856
# thermocouple_type = adafruit_max31856.ThermocoupleType.K

########################################################################
# Multiple thermocouple support (#4)
#
# When True, a second thermocouple is read each duty cycle. The control
# loop uses the AVERAGE of the two readings as the kiln temperature.
# A delta alert fires if the two probes disagree by more than
# multi_tc_delta_alert_degrees, which usually means a hot/cold zone or
# a failing probe. Each extra probe needs its own CS pin.
multiple_thermocouples = False
# spi_cs_2 is only used when multiple_thermocouples is True.
# Define it here when adding a second probe (uses the same SPI bus, second CS):
# spi_cs_2 = board.D6
multi_tc_delta_alert_degrees = 50  # alert threshold (in temp_scale units)

########################################################################
# If your kiln is above the starting temperature of the schedule when you
# click Start, skip ahead to the first point in the schedule matching
# the current temperature.
seek_start = True

########################################################################
# Duty cycle of the entire system in seconds.
# Every N seconds, a decision is made about switching the relay.
# Thermocouple is read temperature_average_samples times per cycle.
sensor_time_wait = 2

########################################################################
#
#   PID parameters
#
# These work well with the simulated oven; tune them for your kiln.
# pid_ki is INVERTED - smaller number means more integral action.
pid_kp = 10                 # Proportional
pid_ki = 80                 # Integral
pid_kd = 220.83497910261562 # Derivative

# Derivative spike limiter (#2)
# Thermocouple noise can produce sudden large derivative-of-error spikes
# that whip the relay. When True, the dErr value used by the PID is
# clamped to +/- pid_d_spike_limit (in temp_scale per second) and a
# small low-pass filter is applied. This prevents noise transients from
# slamming the SSR while still allowing real heat-up rates through.
pid_d_spike_limit_enabled = True
pid_d_spike_limit = 1.0     # max |dErr| in degrees/sec used by PID
pid_d_filter_alpha = 0.4    # 0..1; higher = more responsive, less smoothing

########################################################################
# Initial heating and Integral Windup
# Deprecated, kept for backwards compatibility.
stop_integral_windup = True

########################################################################
#   Simulation parameters
simulate = False
sim_t_env      = 65
sim_c_heat     = 500.0
sim_c_oven     = 5000.0
sim_p_heat     = 5450.0
sim_R_o_nocool = 0.5
sim_R_o_cool   = 0.05
sim_R_ho_noair = 0.1
sim_R_ho_air   = 0.05
sim_speedup_factor = 1

########################################################################
#
#   Time and Temperature parameters
#
temp_scale          = "c"  # c = Celsius | f = Fahrenheit
time_scale_slope    = "h"  # s | m | h - units for displayed slope
time_scale_profile  = "m"  # s | m | h - units for editing profiles

# Emergency shutoff: just stops the schedule. If your SSR fails closed
# nothing in software can save you - that's why a kiln-sitter / fuse
# disconnect is still required.
emergency_shutoff_temp = 1300  # cone 7 in Celsius

# If the current temperature is outside the pid_control_window, delay
# the schedule until it gets back inside (so the kiln "catches up").
kiln_must_catch_up = True

# Hold-phase auto-extension (#5)
# When True, if the current schedule segment is a HOLD (flat in temp)
# and the kiln has not yet reached the hold temperature, runtime is
# paused at the start of that hold until the kiln reaches the target.
# This guarantees the full hold duration runs at the correct temperature
# instead of a partial hold that "the clock chewed through" before the
# kiln got hot enough.
hold_auto_extend = True
hold_at_temp_tolerance = 5  # degrees - "at temp" if within this band

# Window within which PID control occurs. Outside this window the
# elements are 100% on (too cold) or 100% off (too hot). Larger values
# mean more integral accumulation. Positive integer.
pid_control_window = 3  # degrees

# thermocouple offset
# If you put your thermocouple in ice water and it reads 36F, set this
# offset to -4 to compensate. (Ideally, buy a better thermocouple.)
thermocouple_offset = 0

# Number of temperature samples per duty cycle. Median of these is used.
temperature_average_samples = 10

# AC frequency rejection for the MAX31856 (50Hz outside North America).
ac_freq_50hz = False

########################################################################
# Heating-element failure detection (#1)
#
# If the PID is calling for full heat but the kiln is not actually
# heating up, an element has likely failed (open circuit, blown fuse,
# tripped breaker, SSR open). When True, the controller monitors heat
# rate while heating at full duty and aborts (with an alert) if the
# kiln fails to climb at the expected minimum rate for the configured
# window.
element_failure_detection = True
# How long the PID must be above 95% duty before checking heat rate.
element_failure_min_full_duty_seconds = 240
# Minimum acceptable heat rate (degrees per hour) at full duty.
# A typical electric pottery kiln climbs > 200 deg/hr in temp_scale.
# Cone 6 cones expect to fire from ~1000C in roughly 2-3hr.
element_failure_min_heat_rate = 80
# Below this temperature the kiln may not climb fast (low elements
# losing heat to room) - skip the check.
element_failure_min_temp = 200

########################################################################
# Cool-down monitoring (#3)
# After a profile completes the kiln keeps cooling. We track the
# temperature and notify when it drops below cool_down_safe_open_temp.
# Set to None to disable the safe-to-open notification.
cool_down_safe_open_temp = 150       # degrees in temp_scale
cool_down_notify_on_complete = True  # send notification when run completes
cool_down_notify_on_safe_open = True # send notification at safe_open temp

########################################################################
# Emergency error handling
# In some cases you might want to ignore a specific error, log it and
# continue running. Only set True if you know what you are doing.
ignore_temp_too_high = False
ignore_tc_lost_connection = False
ignore_tc_cold_junction_range_error = False
ignore_tc_range_error = False
ignore_tc_cold_junction_temp_high = False
ignore_tc_cold_junction_temp_low = False
ignore_tc_temp_high = False
ignore_tc_temp_low = False
ignore_tc_voltage_error = False
ignore_tc_short_errors = False
ignore_tc_unknown_error = False
# Overrides ALL thermocouple errors - dangerous.
ignore_tc_too_many_errors = False

########################################################################
# Automatic restarts after power outages.
# State file is written every sensor_time_wait seconds and is consulted
# at startup. DO NOT put it in /tmp.
automatic_restarts = True
automatic_restart_window = 15  # max minutes since power outage
automatic_restart_state_file = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'state.json')
)

# Locked active profile copy (#7).
# At run-start the live profile is deep-copied to this file. The oven
# control loop and history use the LOCKED copy, so editing the source
# JSON mid-firing never silently changes what is being fired.
locked_profile_file = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'locked_profile.json')
)

########################################################################
# Profile storage directory
# A community profile repo lives at https://github.com/jbruce12000/kiln-profiles
kiln_profiles_directory = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "storage", "profiles")
)

########################################################################
# Firing history database (#8)
# SQLite database that persists every completed firing's profile,
# actual temperature curve, cost, and a few meta fields. The web UI
# uses this to render past firings and overlay actual vs. target.
history_enabled  = True
history_db_path  = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "storage", "history.db")
)
# Sample rate for stored history points (seconds between samples).
# Storing every duty cycle (2s) for a 13hr firing is ~24K points per
# run; downsampling to 30s keeps the DB small and graphs snappy.
history_sample_interval_seconds = 30

########################################################################
# Notifications (#9)
# Multiple backends can be enabled simultaneously. Triggers:
#   - kiln run completed (cool-down begins)
#   - safe-to-open temperature reached
#   - emergency shutoff (over-temp, element failure, TC errors)
#
# Email backend (uses stdlib smtplib over TLS or STARTTLS):
notify_email_enabled  = False
notify_email_to       = ["you@example.com"]
notify_email_from     = "kiln@example.com"
notify_email_smtp_host = "smtp.gmail.com"
notify_email_smtp_port = 587
notify_email_smtp_user = ""        # often the same as notify_email_from
notify_email_smtp_pass = ""        # an app password is recommended
notify_email_use_tls   = True      # STARTTLS (port 587). Set False + SSL via 465.

# Pushover (https://pushover.net) - mobile push notifications:
notify_pushover_enabled = False
notify_pushover_user_key = ""
notify_pushover_app_token = ""

# ntfy (https://ntfy.sh) - free, anonymous, mobile push:
notify_ntfy_enabled = False
notify_ntfy_topic   = "kiln-controller-yourtopic"
notify_ntfy_server  = "https://ntfy.sh"

# Slack (incoming webhook URL):
notify_slack_enabled = False
notify_slack_webhook_url = ""

########################################################################
# Web UI authentication (#11)
# When True, /api, /control and /storage routes require HTTP Basic auth.
# The credentials live in auth.json (created on first run with the
# values below). Edit that file or use the web UI to rotate them.
auth_enabled  = False
auth_username = "admin"
# Initial password used on first start. Stored hashed in auth.json,
# then can be safely cleared from this file. CHANGE THIS BEFORE
# enabling auth on a network-accessible kiln.
auth_initial_password = "kiln"
auth_file = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "auth.json")
)

########################################################################
# Low-temperature element throttling.
# Kiln elements have lots of power and tend to overshoot drastically at
# low temperatures. When below throttle_below_temp and outside the PID
# window, only throttle_percent of full duty is allowed.
# Set throttle_percent = 100 to disable throttling.
throttle_below_temp = 300
throttle_percent = 20
