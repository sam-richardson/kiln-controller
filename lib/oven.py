import copy
import datetime
import json
import logging
import os
import statistics
import threading
import time

import config

# Optional imports for hardware. The simulator path does not need them.
try:
    import digitalio
    import busio
    import adafruit_bitbangio as bitbangio
except (ImportError, NotImplementedError):
    digitalio = None
    busio = None
    bitbangio = None

from history import FiringHistory
import notifications

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------- #


class DupFilter:
    def __init__(self):
        self.msgs = set()

    def filter(self, record):
        rv = record.msg not in self.msgs
        self.msgs.add(record.msg)
        return rv


class Duplogger:
    def __init__(self):
        self.log = logging.getLogger("%s.dupfree" % __name__)
        self.log.addFilter(DupFilter())

    def logref(self):
        return self.log


duplog = Duplogger().logref()


# --------------------------------------------------------------------- #
# Output (relay)
# --------------------------------------------------------------------- #


class Output:
    """A GPIO output that drives the SSR controlling the kiln elements."""

    def __init__(self):
        self.active = False
        self.heater = digitalio.DigitalInOut(config.gpio_heat)
        self.heater.direction = digitalio.Direction.OUTPUT

    def heat(self, sleepfor):
        self.heater.value = True
        time.sleep(sleepfor)

    def cool(self, sleepfor):
        """No active cooling - just sleep with the heater off."""
        self.heater.value = False
        time.sleep(sleepfor)


# --------------------------------------------------------------------- #
# Boards / temperature sensors
# --------------------------------------------------------------------- #


class Board:
    """Represents the host single-board computer (Raspberry Pi etc.)."""

    def __init__(self):
        log.info("board: %s", self.name)
        self.temp_sensor.start()
        # Optional secondary thermocouple - started independently so a
        # crashed secondary never takes the primary down.
        if getattr(self, "temp_sensor_2", None) is not None:
            self.temp_sensor_2.start()


class RealBoard(Board):
    """A blinka-compatible board with one or two thermocouple breakouts."""

    def __init__(self):
        self.name = None
        self.load_libs()
        self.temp_sensor = self.choose_tempsensor(config.spi_cs)
        self.temp_sensor_2 = None
        if getattr(config, "multiple_thermocouples", False):
            cs2 = getattr(config, "spi_cs_2", None)
            if cs2 is None:
                log.error(
                    "multiple_thermocouples enabled but spi_cs_2 not set "
                    "in config.py - secondary disabled"
                )
            else:
                self.temp_sensor_2 = self.choose_tempsensor(cs2)
        Board.__init__(self)

    def load_libs(self):
        import board
        self.name = board.board_id

    def choose_tempsensor(self, cs_pin):
        if config.max31855:
            return Max31855(cs_pin)
        if config.max31856:
            return Max31856(cs_pin)


class SimulatedBoard(Board):
    """No-hardware board for software simulations."""

    def __init__(self):
        self.name = "simulated"
        self.temp_sensor = TempSensorSimulated()
        self.temp_sensor_2 = None
        Board.__init__(self)


# --------------------------------------------------------------------- #
# Temperature sensors
# --------------------------------------------------------------------- #


class TempSensor(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.time_step = config.sensor_time_wait
        self.status = ThermocoupleTracker()


class TempSensorSimulated(TempSensor):
    def __init__(self):
        TempSensor.__init__(self)
        self.simulated_temperature = config.sim_t_env

    def temperature(self):
        return self.simulated_temperature

    def run(self):
        # nothing to do - simulator updates self.simulated_temperature
        # directly. We still need a thread so the API is symmetric.
        while True:
            time.sleep(self.time_step)


class TempSensorReal(TempSensor):
    """Reads a thermocouple breakout via hardware or software SPI.

    Each instance owns its own CS pin so multiple thermocouples can
    share the same SPI bus.
    """

    def __init__(self, cs_pin):
        TempSensor.__init__(self)
        self.sleeptime = self.time_step / float(config.temperature_average_samples)
        self.temptracker = TempTracker()
        self.spi_setup()
        self.cs = digitalio.DigitalInOut(cs_pin)

    def spi_setup(self):
        if (hasattr(config, "spi_sclk")
                and hasattr(config, "spi_mosi")
                and hasattr(config, "spi_miso")):
            self.spi = bitbangio.SPI(
                config.spi_sclk, config.spi_mosi, config.spi_miso
            )
            log.info("Software SPI selected for reading thermocouple")
        else:
            import board
            self.spi = board.SPI()
            log.info("Hardware SPI selected for reading thermocouple")

    def get_temperature(self):
        try:
            temp = self.raw_temp()  # subclass-provided, always degrees C
            self.status.good()
            return temp
        except ThermocoupleError as tce:
            if tce.ignore:
                log.error("Problem reading temp (ignored) %s", tce.message)
                self.status.good()
            else:
                log.error("Problem reading temp %s", tce.message)
                self.status.bad()
        return None

    def temperature(self):
        return self.temptracker.get_avg_temp()

    def run(self):
        while True:
            temp = self.get_temperature()
            if temp is not None:
                self.temptracker.add(temp)
            time.sleep(self.sleeptime)


class TempTracker:
    """Sliding window of N temperature samples per duty cycle."""

    def __init__(self):
        self.size = config.temperature_average_samples
        self.temps = [0 for _ in range(self.size)]

    def add(self, temp):
        self.temps.append(temp)
        while len(self.temps) > self.size:
            del self.temps[0]

    def get_avg_temp(self, chop=25):
        return statistics.median(self.temps)


class ThermocoupleTracker:
    """Sliding success/failure window over the last two duty cycles."""

    def __init__(self):
        self.size = config.temperature_average_samples * 2
        self.status = [True for _ in range(self.size)]
        self.limit = 30

    def good(self):
        self.status.append(True)
        del self.status[0]

    def bad(self):
        self.status.append(False)
        del self.status[0]

    def error_percent(self):
        errors = sum(s is False for s in self.status)
        return (errors / self.size) * 100

    def over_error_limit(self):
        return self.error_percent() > self.limit


# --------------------------------------------------------------------- #
# Thermocouple breakout drivers (Adafruit MAX31855 / 31856)
# --------------------------------------------------------------------- #


class Max31855(TempSensorReal):
    def __init__(self, cs_pin):
        TempSensorReal.__init__(self, cs_pin)
        log.info("thermocouple MAX31855 (cs=%s)", cs_pin)
        import adafruit_max31855
        self.thermocouple = adafruit_max31855.MAX31855(self.spi, self.cs)

    def raw_temp(self):
        try:
            return self.thermocouple.temperature_NIST
        except RuntimeError as rte:
            if rte.args and rte.args[0]:
                raise Max31855_Error(rte.args[0])
            raise Max31855_Error("unknown")


class Max31856(TempSensorReal):
    def __init__(self, cs_pin):
        TempSensorReal.__init__(self, cs_pin)
        log.info("thermocouple MAX31856 (cs=%s)", cs_pin)
        import adafruit_max31856
        self.thermocouple = adafruit_max31856.MAX31856(
            self.spi, self.cs, thermocouple_type=config.thermocouple_type
        )
        if config.ac_freq_50hz:
            self.thermocouple.noise_rejection = 50
        else:
            self.thermocouple.noise_rejection = 60

    def raw_temp(self):
        # Adafruit's library does not raise; faults live in a dict on
        # self.thermocouple.fault.
        temp = self.thermocouple.temperature
        for k, v in self.thermocouple.fault.items():
            if v:
                raise Max31856_Error(k)
        return temp


class ThermocoupleError(Exception):
    """Base for normalized thermocouple exceptions."""

    def __init__(self, message):
        self.ignore = False
        self.message = message
        self.map_message()
        self.set_ignore()
        super().__init__(self.message)

    def set_ignore(self):
        msg_to_flag = {
            "not connected": "ignore_tc_lost_connection",
            "short circuit": "ignore_tc_short_errors",
            "unknown": "ignore_tc_unknown_error",
            "cold junction range fault": "ignore_tc_cold_junction_range_error",
            "thermocouple range fault": "ignore_tc_range_error",
            "cold junction temp too high": "ignore_tc_cold_junction_temp_high",
            "cold junction temp too low": "ignore_tc_cold_junction_temp_low",
            "thermocouple temp too high": "ignore_tc_temp_high",
            "thermocouple temp too low": "ignore_tc_temp_low",
            "voltage too high or low": "ignore_tc_voltage_error",
        }
        flag = msg_to_flag.get(self.message)
        if flag is not None and getattr(config, flag, False):
            self.ignore = True

    def map_message(self):
        try:
            self.message = self.map[self.orig_message]
        except KeyError:
            self.message = "unknown"


class Max31855_Error(ThermocoupleError):
    def __init__(self, message):
        self.orig_message = message
        self.map = {
            "thermocouple not connected": "not connected",
            "short circuit to ground": "short circuit",
            "short circuit to power": "short circuit",
        }
        super().__init__(message)


class Max31856_Error(ThermocoupleError):
    def __init__(self, message):
        self.orig_message = message
        self.map = {
            "cj_range": "cold junction range fault",
            "tc_range": "thermocouple range fault",
            "cj_high": "cold junction temp too high",
            "cj_low": "cold junction temp too low",
            "tc_high": "thermocouple temp too high",
            "tc_low": "thermocouple temp too low",
            "voltage": "voltage too high or low",
            "open_tc": "not connected",
        }
        super().__init__(message)


# --------------------------------------------------------------------- #
# Element-failure detector  (#1)
# --------------------------------------------------------------------- #


class ElementFailureDetector:
    """Detects "calling for full heat but kiln is not climbing".

    Once the PID has been at >=95% duty for ``min_full_duty_seconds``,
    we expect the kiln temperature to be increasing at at least
    ``min_heat_rate`` deg/hr. If it is not, we raise.

    Skipped below ``min_temp`` since low-temp ramps can be slow.
    Skipped during cool-down (target < temp by more than the PID window)
    since the PID is intentionally at 0% duty.
    """

    def __init__(self):
        self.enabled = bool(getattr(config, "element_failure_detection", True))
        self.min_seconds = float(
            getattr(config, "element_failure_min_full_duty_seconds", 240)
        )
        self.min_rate = float(
            getattr(config, "element_failure_min_heat_rate", 80)
        )
        self.min_temp = float(
            getattr(config, "element_failure_min_temp", 200)
        )
        self.full_duty_since = None

    def reset(self):
        self.full_duty_since = None

    def check(self, pid_out, current_temp, target_temp, heat_rate, now):
        """Returns a string failure reason if the kiln looks dead, else None."""
        if not self.enabled:
            return None
        # only meaningful when the controller is calling for nearly full heat
        if pid_out < 0.95:
            self.full_duty_since = None
            return None
        # don't bother below threshold temp - kiln may climb slowly there
        if current_temp < self.min_temp:
            self.full_duty_since = None
            return None
        # skip if we are nearly at temperature - PID can momentarily clip
        # to 100% while inside the control window
        if target_temp - current_temp <= getattr(config, "pid_control_window", 3):
            self.full_duty_since = None
            return None

        if self.full_duty_since is None:
            self.full_duty_since = now
            return None

        elapsed = (now - self.full_duty_since).total_seconds()
        if elapsed < self.min_seconds:
            return None

        if heat_rate < self.min_rate:
            return (
                "elements not heating: PID at full duty for %ds, "
                "heat_rate=%.1f deg/hr (min=%.1f), temp=%.1f, target=%.1f"
                % (int(elapsed), heat_rate, self.min_rate,
                   current_temp, target_temp)
            )
        return None


# --------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------- #


class Profile:
    """Linear-interpolation firing profile.

    Internal data is a list of [time_seconds, temperature] waypoints.
    """

    def __init__(self, json_data):
        if isinstance(json_data, str):
            obj = json.loads(json_data)
        else:
            obj = json_data
        self.name = obj["name"]
        self.data = sorted(obj["data"])

    # ------ shape helpers ------------------------------------------------

    def get_duration(self):
        return max(t for (t, _x) in self.data)

    @staticmethod
    def find_x_given_y_on_line_from_two_points(y, point1, point2):
        if point1[0] > point2[0]:
            return 0
        if point1[1] >= point2[1]:
            return 0
        return (
            (y - point1[1]) * (point2[0] - point1[0])
            / (point2[1] - point1[1])
            + point1[0]
        )

    def find_next_time_from_temperature(self, temperature):
        time_s = 0
        for index, point2 in enumerate(self.data):
            if point2[1] >= temperature:
                if index > 0:
                    if self.data[index - 1][1] <= temperature:
                        time_s = self.find_x_given_y_on_line_from_two_points(
                            temperature, self.data[index - 1], point2
                        )
                        if time_s == 0:
                            if self.data[index - 1][1] == point2[1]:
                                time_s = self.data[index - 1][0]
                                break
        return time_s

    def get_surrounding_points(self, time_s):
        if time_s > self.get_duration():
            return (None, None)
        prev_point = None
        next_point = None
        for i in range(len(self.data)):
            if time_s < self.data[i][0]:
                prev_point = self.data[i - 1]
                next_point = self.data[i]
                break
        return (prev_point, next_point)

    def get_target_temperature(self, time_s):
        if time_s > self.get_duration():
            return 0
        prev_point, next_point = self.get_surrounding_points(time_s)
        incl = (
            float(next_point[1] - prev_point[1])
            / float(next_point[0] - prev_point[0])
        )
        return prev_point[1] + (time_s - prev_point[0]) * incl

    # ------ hold detection -----------------------------------------------

    def is_in_hold(self, time_s, tolerance_per_sec=0.01):
        """True if the current segment is "flat" (a hold).

        A segment is treated as a hold if its absolute slope is below
        ``tolerance_per_sec`` degrees/second (0.01 deg/s = 36 deg/hr).
        """
        if time_s > self.get_duration():
            return False
        prev_point, next_point = self.get_surrounding_points(time_s)
        if prev_point is None or next_point is None:
            return False
        dt = next_point[0] - prev_point[0]
        if dt <= 0:
            return False
        slope = abs(next_point[1] - prev_point[1]) / dt
        return slope < tolerance_per_sec


# --------------------------------------------------------------------- #
# Oven (parent of SimulatedOven and RealOven)
# --------------------------------------------------------------------- #


class Oven(threading.Thread):
    """Common control logic for both real and simulated ovens."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.temperature = 0
        self.time_step = config.sensor_time_wait
        # one shared history recorder per process
        self.history = FiringHistory()
        # element-failure detector lives across firings, just reset
        self.element_failure = ElementFailureDetector()
        self.reset()

    def reset(self):
        self.cost = 0
        self.state = "IDLE"
        self.profile = None
        self.start_time = 0
        self.runtime = 0
        self.totaltime = 0
        self.target = 0
        self.heat = 0
        self.heat_rate = 0
        self.heat_rate_temps = []
        # multi-tc latest delta
        self.tc_delta = 0
        self.tc_temp_2 = None
        # cool-down tracking
        self.cool_down_armed = False
        self.cool_down_safe_open_notified = False
        self.cool_down_complete_notified = False
        # last delta-alert flag so we don't spam
        self.tc_delta_notified = False
        # reset element failure history
        self.element_failure.reset()
        # build a fresh PID with the current config
        self.pid = PID(
            ki=config.pid_ki, kd=config.pid_kd, kp=config.pid_kp
        )

    # ------ start helpers ------------------------------------------------

    @staticmethod
    def get_start_from_temperature(profile, temp):
        target_temp = profile.get_target_temperature(0)
        if temp > target_temp + 5:
            startat = profile.find_next_time_from_temperature(temp)
            log.info(
                "seek_start is in effect, starting at: %d s, %d deg",
                round(startat), round(temp),
            )
        else:
            startat = 0
        return startat

    def set_heat_rate(self, runtime, temp):
        """Maintain a sliding window of (time, temp) so we can compute a
        smoothed degrees-per-hour heat rate."""
        numtemps = 60
        self.heat_rate_temps.append((runtime, temp))
        if len(self.heat_rate_temps) > numtemps:
            self.heat_rate_temps = self.heat_rate_temps[-numtemps:]
        time2 = self.heat_rate_temps[-1][0]
        time1 = self.heat_rate_temps[0][0]
        temp2 = self.heat_rate_temps[-1][1]
        temp1 = self.heat_rate_temps[0][1]
        if time2 > time1:
            self.heat_rate = ((temp2 - temp1) / (time2 - time1)) * 3600

    # ------ run / abort --------------------------------------------------

    def _lock_profile_to_disk(self, profile):
        """Snapshot the live profile so mid-firing edits to the source
        JSON cannot silently change what is actually being fired (#7)."""
        try:
            payload = {
                "name": profile.name,
                "type": "profile",
                "data": copy.deepcopy(profile.data),
                "locked_at": datetime.datetime.utcnow().isoformat(),
            }
            with open(config.locked_profile_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            log.info("locked profile snapshot written to %s",
                     config.locked_profile_file)
        except Exception:
            log.exception("failed to write locked profile snapshot")

    def run_profile(self, profile, startat=0, allow_seek=True):
        log.debug("run_profile run on thread %s",
                  threading.current_thread().name)
        runtime = startat * 60
        if allow_seek and self.state == "IDLE" and config.seek_start:
            temp = self.board.temp_sensor.temperature()
            runtime += self.get_start_from_temperature(profile, temp)

        self.reset()
        self.startat = startat * 60
        self.runtime = runtime
        self.start_time = (
            datetime.datetime.now()
            - datetime.timedelta(seconds=self.startat)
        )
        # active profile is a deep copy. Editing the source JSON
        # mid-firing has no effect on the running schedule.
        self.profile = Profile(json.dumps({
            "name": profile.name,
            "data": copy.deepcopy(profile.data),
        }))
        self._lock_profile_to_disk(self.profile)
        self.totaltime = self.profile.get_duration()
        self.state = "RUNNING"
        # history record
        try:
            self.history.start_firing(
                self.profile.name,
                {"name": self.profile.name,
                 "type": "profile",
                 "data": copy.deepcopy(self.profile.data)},
                getattr(config, "currency_type", ""),
            )
        except Exception:
            log.exception("history start_firing call failed (ignored)")
        log.info("Running schedule %s starting at %d minutes",
                 self.profile.name, startat)
        log.info("Starting")

    def abort_run(self, outcome="aborted", reason=""):
        # mark history before reset (which clears state) so we know
        # which firing to close out
        try:
            state = self.get_state()
            self.history.end_firing(outcome, state=state, notes=reason)
        except Exception:
            log.exception("history end_firing call failed (ignored)")
        # arm cooldown monitoring if we had been running
        was_running = self.state == "RUNNING"
        self.reset()
        if was_running:
            self.cool_down_armed = True
            if (outcome == "completed"
                    and getattr(config, "cool_down_notify_on_complete", True)):
                try:
                    notifications.notify_run_complete(self.get_state())
                except Exception:
                    log.exception("notify_run_complete failed")
        self.save_automatic_restart_state()

    # ------ runtime helpers ----------------------------------------------

    def get_start_time(self):
        return (
            datetime.datetime.now()
            - datetime.timedelta(milliseconds=self.runtime * 1000)
        )

    def kiln_must_catch_up(self):
        """Shift the schedule forward when the kiln cannot keep up (#5
        "hold auto-extension" extends this further by also pausing during
        a hold segment until the hold target is reached)."""
        if not config.kiln_must_catch_up:
            return
        temp = (
            self.board.temp_sensor.temperature()
            + config.thermocouple_offset
        )
        # standard catch-up
        if self.target - temp > config.pid_control_window:
            duplog.info("kiln must catch up, too cold, shifting schedule")
            self.start_time = self.get_start_time()
        if temp - self.target > config.pid_control_window:
            duplog.info("kiln must catch up, too hot, shifting schedule")
            self.start_time = self.get_start_time()

        # #5 hold-phase auto-extension: while the schedule says HOLD,
        # but we have not reached the hold target yet, freeze the
        # runtime clock at the start of the hold so the full hold
        # duration is honored after we get there.
        if not getattr(config, "hold_auto_extend", True):
            return
        if self.profile is None:
            return
        if not self.profile.is_in_hold(self.runtime):
            return
        tol = float(getattr(config, "hold_at_temp_tolerance", 5))
        # if we have not yet reached hold target, hold the clock
        if (self.target - temp) > tol:
            duplog.info(
                "hold auto-extend: target=%.1f temp=%.1f - holding clock",
                self.target, temp,
            )
            self.start_time = self.get_start_time()

    def update_runtime(self):
        runtime_delta = datetime.datetime.now() - self.start_time
        if runtime_delta.total_seconds() < 0:
            runtime_delta = datetime.timedelta(0)
        self.runtime = runtime_delta.total_seconds()

    def update_target_temp(self):
        self.target = self.profile.get_target_temperature(self.runtime)

    def reset_if_emergency(self):
        """Detect: over-temp, repeated TC errors, element failure, large
        hot/cold zone delta. Anything caught here aborts the firing and
        sends an emergency notification."""
        # over-temperature
        cur = (
            self.board.temp_sensor.temperature()
            + config.thermocouple_offset
        )
        if cur >= config.emergency_shutoff_temp:
            log.error("emergency!!! temperature too high (%.1f)", cur)
            if not config.ignore_temp_too_high:
                try:
                    notifications.notify_emergency(
                        "temperature exceeded emergency_shutoff_temp",
                        self.get_state(),
                    )
                except Exception:
                    log.exception("notify_emergency failed")
                self.abort_run(
                    outcome="emergency",
                    reason="over-temp (%.1f)" % cur,
                )
                return

        # too many thermocouple errors
        if self.board.temp_sensor.status.over_error_limit():
            log.error("emergency!!! too many TC errors in a short period")
            if not config.ignore_tc_too_many_errors:
                try:
                    notifications.notify_emergency(
                        "too many thermocouple errors",
                        self.get_state(),
                    )
                except Exception:
                    log.exception("notify_emergency failed")
                self.abort_run(
                    outcome="emergency",
                    reason="thermocouple errors over limit",
                )
                return

        # multi-thermocouple delta alert
        if (getattr(self.board, "temp_sensor_2", None) is not None):
            t2 = self.board.temp_sensor_2.temperature()
            if t2 is not None and t2 != 0:
                self.tc_temp_2 = t2
                self.tc_delta = abs(cur - t2)
                threshold = float(
                    getattr(config, "multi_tc_delta_alert_degrees", 50)
                )
                if (self.tc_delta >= threshold
                        and not self.tc_delta_notified):
                    msg = (
                        "thermocouple delta alert: "
                        "primary=%.1f secondary=%.1f delta=%.1f"
                        % (cur, t2, self.tc_delta)
                    )
                    log.warning(msg)
                    self.tc_delta_notified = True
                    try:
                        notifications.notify_emergency(msg, self.get_state())
                    except Exception:
                        log.exception("notify_emergency failed")

        # element failure
        pid_out = (self.pid.pidstats.get("out", 0) if self.pid else 0)
        reason = self.element_failure.check(
            pid_out=pid_out,
            current_temp=cur,
            target_temp=self.target,
            heat_rate=self.heat_rate,
            now=datetime.datetime.now(),
        )
        if reason:
            log.error("emergency!!! %s", reason)
            try:
                notifications.notify_emergency(reason, self.get_state())
            except Exception:
                log.exception("notify_emergency failed")
            self.abort_run(outcome="emergency", reason=reason)
            return

    def reset_if_schedule_ended(self):
        if self.runtime > self.totaltime:
            log.info("schedule ended, shutting down")
            log.info("total cost = %s%.2f",
                     config.currency_type, self.cost)
            self.abort_run(outcome="completed",
                           reason="schedule ended")

    def update_cost(self):
        if self.heat:
            cost = (config.kwh_rate * config.kw_elements) * (self.heat / 3600)
        else:
            cost = 0
        self.cost = self.cost + cost

    def get_state(self):
        temp = 0
        try:
            temp = (
                self.board.temp_sensor.temperature()
                + config.thermocouple_offset
            )
        except AttributeError:
            temp = 0

        self.set_heat_rate(self.runtime, temp)

        state = {
            "cost": self.cost,
            "runtime": self.runtime,
            "temperature": temp,
            "target": self.target,
            "state": self.state,
            "heat": self.heat,
            "heat_rate": self.heat_rate,
            "totaltime": self.totaltime,
            "kwh_rate": config.kwh_rate,
            "currency_type": config.currency_type,
            "profile": self.profile.name if self.profile else None,
            "pidstats": self.pid.pidstats,
            "tc_temp_2": self.tc_temp_2,
            "tc_delta": self.tc_delta,
            "cool_down_armed": self.cool_down_armed,
        }
        return state

    # ------ persistence --------------------------------------------------

    def save_state(self):
        with open(
            config.automatic_restart_state_file, "w", encoding="utf-8"
        ) as f:
            json.dump(self.get_state(), f, ensure_ascii=False, indent=4)

    def state_file_is_old(self):
        if os.path.isfile(config.automatic_restart_state_file):
            state_age = os.path.getmtime(config.automatic_restart_state_file)
            now = time.time()
            minutes = (now - state_age) / 60
            if minutes <= config.automatic_restart_window:
                return False
        return True

    def save_automatic_restart_state(self):
        if not config.automatic_restarts:
            return False
        self.save_state()

    def should_i_automatic_restart(self):
        if not config.automatic_restarts:
            return False
        if self.state_file_is_old():
            duplog.info(
                "automatic restart not possible. state file does not "
                "exist or is too old."
            )
            return False
        with open(config.automatic_restart_state_file) as infile:
            d = json.load(infile)
        if d["state"] != "RUNNING":
            duplog.info(
                "automatic restart not possible. state = %s", d["state"]
            )
            return False
        return True

    def automatic_restart(self):
        with open(config.automatic_restart_state_file) as infile:
            d = json.load(infile)
        startat = d["runtime"] / 60
        # prefer the locked profile snapshot (#7) over the source JSON
        locked_path = getattr(config, "locked_profile_file", None)
        if locked_path and os.path.exists(locked_path):
            log.info("automatic restart: using locked profile snapshot %s",
                     locked_path)
            with open(locked_path) as infile:
                profile_payload = json.load(infile)
            profile_json = json.dumps({
                "name": profile_payload["name"],
                "data": profile_payload["data"],
            })
        else:
            filename = "%s.json" % d["profile"]
            profile_path = os.path.abspath(os.path.join(
                os.path.dirname(__file__),
                "..", "storage", "profiles", filename,
            ))
            log.info("automatically restarting profile = %s at minute = %d",
                     profile_path, startat)
            with open(profile_path) as infile:
                profile_json = json.dumps(json.load(infile))
        profile = Profile(profile_json)
        self.run_profile(profile, startat=startat, allow_seek=False)
        self.cost = d["cost"]
        time.sleep(1)
        self.ovenwatcher.record(profile)

    def set_ovenwatcher(self, watcher):
        log.info("ovenwatcher set in oven class")
        self.ovenwatcher = watcher

    # ------ cool-down monitoring (#3) -----------------------------------

    def cool_down_check(self):
        """When a firing has ended (state IDLE) but the kiln may still be
        very hot, watch the temperature and notify the user when it has
        dropped below cool_down_safe_open_temp."""
        if not self.cool_down_armed:
            return
        try:
            temp = (
                self.board.temp_sensor.temperature()
                + config.thermocouple_offset
            )
        except Exception:
            return
        # save sample to history if a firing is open (safety: usually it
        # was closed by abort_run, but if user is monitoring cool-down
        # of a manually-aborted run, samples can keep flowing)
        threshold = getattr(config, "cool_down_safe_open_temp", None)
        if threshold is None:
            return
        if temp <= threshold:
            if (not self.cool_down_safe_open_notified
                    and getattr(config, "cool_down_notify_on_safe_open", True)):
                try:
                    notifications.notify_safe_to_open(self.get_state())
                except Exception:
                    log.exception("notify_safe_to_open failed")
                self.cool_down_safe_open_notified = True
            # disarm so we don't keep checking forever
            self.cool_down_armed = False

    # ------ main loop ----------------------------------------------------

    def run(self):
        while True:
            log.debug("Oven running on %s", threading.current_thread().name)
            if self.state == "IDLE":
                if self.should_i_automatic_restart():
                    self.automatic_restart()
                else:
                    self.cool_down_check()
                time.sleep(1)
                continue
            if self.state == "RUNNING":
                self.update_cost()
                self.save_automatic_restart_state()
                self.kiln_must_catch_up()
                self.update_runtime()
                self.update_target_temp()
                self.heat_then_cool()
                # record sample BEFORE emergency check so a final sample
                # is captured for the firing history even if emergency
                # ends the run.
                try:
                    self.history.record_sample(self.get_state())
                except Exception:
                    log.exception("history record_sample failed (ignored)")
                self.reset_if_emergency()
                self.reset_if_schedule_ended()


# --------------------------------------------------------------------- #
# Simulated oven
# --------------------------------------------------------------------- #


class SimulatedOven(Oven):
    def __init__(self):
        self.board = SimulatedBoard()
        self.t_env = config.sim_t_env
        self.c_heat = config.sim_c_heat
        self.c_oven = config.sim_c_oven
        self.p_heat = config.sim_p_heat
        self.R_o_nocool = config.sim_R_o_nocool
        self.R_ho_noair = config.sim_R_ho_noair
        self.R_ho = self.R_ho_noair
        self.speedup_factor = config.sim_speedup_factor
        self.t = config.sim_t_env
        self.t_h = self.t_env

        super().__init__()

        self.start()
        log.info("SimulatedOven started")

    def get_start_time(self):
        return (
            datetime.datetime.now()
            - datetime.timedelta(milliseconds=self.runtime * 1000 / self.speedup_factor)
        )

    def update_runtime(self):
        runtime_delta = datetime.datetime.now() - self.start_time
        if runtime_delta.total_seconds() < 0:
            runtime_delta = datetime.timedelta(0)
        self.runtime = runtime_delta.total_seconds() * self.speedup_factor

    def update_target_temp(self):
        self.target = self.profile.get_target_temperature(self.runtime)

    def heating_energy(self, pid):
        self.Q_h = self.p_heat * self.time_step * pid

    def temp_changes(self):
        self.t_h += self.Q_h / self.c_heat
        self.p_ho = (self.t_h - self.t) / self.R_ho
        self.t += self.p_ho * self.time_step / self.c_oven
        self.t_h -= self.p_ho * self.time_step / self.c_heat
        self.p_env = (self.t - self.t_env) / self.R_o_nocool
        self.t -= self.p_env * self.time_step / self.c_oven
        self.temperature = self.t
        self.board.temp_sensor.simulated_temperature = self.t

    def heat_then_cool(self):
        now_simulator = (
            self.start_time
            + datetime.timedelta(milliseconds=self.runtime * 1000)
        )
        pid = self.pid.compute(
            self.target,
            self.board.temp_sensor.temperature() + config.thermocouple_offset,
            now_simulator,
        )

        heat_on = float(self.time_step * pid)
        heat_off = float(self.time_step * (1 - pid))

        self.heating_energy(pid)
        self.temp_changes()

        self.heat = 0.0
        if heat_on > 0:
            self.heat = heat_on

        log.info(
            "simulation: -> %dW heater: %.0f -> %dW oven: %.0f -> %dW env",
            int(self.p_heat * pid), self.t_h, int(self.p_ho), self.t,
            int(self.p_env),
        )
        self._log_pid_stats(heat_on, heat_off)
        time.sleep(self.time_step / self.speedup_factor)

    def _log_pid_stats(self, heat_on, heat_off):
        time_left = self.totaltime - self.runtime
        try:
            log.info(
                "temp=%.2f, target=%.2f, error=%.2f, pid=%.2f, p=%.2f, "
                "i=%.2f, d=%.2f, heat_on=%.2f, heat_off=%.2f, run_time=%d, "
                "total_time=%d, time_left=%d",
                self.pid.pidstats["ispoint"],
                self.pid.pidstats["setpoint"],
                self.pid.pidstats["err"],
                self.pid.pidstats["pid"],
                self.pid.pidstats["p"],
                self.pid.pidstats["i"],
                self.pid.pidstats["d"],
                heat_on, heat_off,
                self.runtime, self.totaltime, time_left,
            )
        except KeyError:
            pass


# --------------------------------------------------------------------- #
# Real oven
# --------------------------------------------------------------------- #


class RealOven(Oven):
    def __init__(self):
        self.board = RealBoard()
        self.output = Output()
        self.reset()
        Oven.__init__(self)
        self.start()

    def reset(self):
        super().reset()
        if hasattr(self, "output"):
            self.output.cool(0)

    def heat_then_cool(self):
        pid = self.pid.compute(
            self.target,
            self.board.temp_sensor.temperature() + config.thermocouple_offset,
            datetime.datetime.now(),
        )

        heat_on = float(self.time_step * pid)
        heat_off = float(self.time_step * (1 - pid))

        self.heat = 0.0
        if heat_on > 0:
            self.heat = 1.0

        if heat_on:
            self.output.heat(heat_on)
        if heat_off:
            self.output.cool(heat_off)
        self._log_pid_stats(heat_on, heat_off)

    def _log_pid_stats(self, heat_on, heat_off):
        time_left = self.totaltime - self.runtime
        try:
            log.info(
                "temp=%.2f, target=%.2f, error=%.2f, pid=%.2f, p=%.2f, "
                "i=%.2f, d=%.2f, heat_on=%.2f, heat_off=%.2f, run_time=%d, "
                "total_time=%d, time_left=%d",
                self.pid.pidstats["ispoint"],
                self.pid.pidstats["setpoint"],
                self.pid.pidstats["err"],
                self.pid.pidstats["pid"],
                self.pid.pidstats["p"],
                self.pid.pidstats["i"],
                self.pid.pidstats["d"],
                heat_on, heat_off,
                self.runtime, self.totaltime, time_left,
            )
        except KeyError:
            pass


# --------------------------------------------------------------------- #
# PID
# --------------------------------------------------------------------- #


class PID:
    """Custom PID with config.pid_control_window cutoff and
    derivative-spike filtering (#2).

    The window-based on/off cutoff outside the control band prevents
    integral wind-up while the kiln is far from the setpoint. Inside
    the band, normal PID is computed.

    The derivative term operates on error rather than measurement, so
    a noisy thermocouple sample can produce a large transient dErr.
    To prevent that from slamming the relay, dErr is clamped and
    low-pass filtered when ``config.pid_d_spike_limit_enabled`` is True.
    """

    def __init__(self, ki=1, kp=1, kd=1):
        self.ki = ki
        self.kp = kp
        self.kd = kd
        self.lastNow = datetime.datetime.now()
        self.iterm = 0
        self.lastErr = 0
        self.last_dErr_filtered = 0.0
        self.pidstats = {}

    def compute(self, setpoint, ispoint, now):
        timeDelta = (now - self.lastNow).total_seconds()
        if timeDelta <= 0:
            timeDelta = 1e-3

        window_size = 100
        error = float(setpoint - ispoint)

        icomp = 0
        output = 0
        out4logs = 0
        dErr = 0
        dErr_raw = 0

        if error < (-1 * config.pid_control_window):
            duplog.info("kiln outside pid control window, max cooling")
            output = 0
        elif error > (1 * config.pid_control_window):
            duplog.info("kiln outside pid control window, max heating")
            output = 1
            if config.throttle_below_temp and config.throttle_percent:
                if ispoint <= config.throttle_below_temp:
                    output = config.throttle_percent / 100
                    duplog.info(
                        "max heating throttled at %d percent below %d "
                        "degrees to prevent overshoot",
                        config.throttle_percent,
                        config.throttle_below_temp,
                    )
        else:
            icomp = error * timeDelta * (1 / self.ki)
            self.iterm += error * timeDelta * (1 / self.ki)

            dErr_raw = (error - self.lastErr) / timeDelta
            dErr = self._filter_d(dErr_raw)

            output = self.kp * error + self.iterm + self.kd * dErr
            output = sorted([-1 * window_size, output, window_size])[1]
            out4logs = output
            output = float(output / window_size)

        self.lastErr = error
        self.lastNow = now

        # no active cooling
        if output < 0:
            output = 0

        self.pidstats = {
            "time": time.mktime(now.timetuple()),
            "timeDelta": timeDelta,
            "setpoint": setpoint,
            "ispoint": ispoint,
            "err": error,
            "errDelta": dErr,
            "errDeltaRaw": dErr_raw,
            "p": self.kp * error,
            "i": self.iterm,
            "d": self.kd * dErr,
            "kp": self.kp,
            "ki": self.ki,
            "kd": self.kd,
            "pid": out4logs,
            "out": output,
        }

        return output

    def _filter_d(self, dErr_raw):
        """Apply spike clamp + first-order low-pass to the derivative
        term so thermocouple noise does not whip the SSR (#2)."""
        if not getattr(config, "pid_d_spike_limit_enabled", True):
            return dErr_raw
        limit = float(getattr(config, "pid_d_spike_limit", 1.0))
        clamped = max(-limit, min(limit, dErr_raw))
        alpha = float(getattr(config, "pid_d_filter_alpha", 0.4))
        alpha = max(0.0, min(1.0, alpha))
        filtered = alpha * clamped + (1.0 - alpha) * self.last_dErr_filtered
        self.last_dErr_filtered = filtered
        return filtered
