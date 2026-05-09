"""SQLite-backed firing history.

Records every firing run so the web UI can show:
    - a list of past firings (date, profile, duration, peak temp, cost,
      result),
    - the actual temperature curve overlaid on the target profile,
    - cumulative cost over time.

Schema:
    firings(id, started_at, ended_at, profile_name, profile_json,
            outcome, peak_temp, total_cost, currency_type, notes)
    samples(firing_id, runtime_s, target_temp, actual_temp, heat,
            heat_rate, pid_out)

Outcome is one of: 'completed', 'aborted', 'emergency', 'running'.
The history database is small even after many firings: each sample is
~50 bytes, sampled every 30s by default; a 13hr firing => ~1500 samples
=> ~75KB.
"""
import datetime
import json
import logging
import os
import sqlite3
import threading

import config

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS firings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    profile_name    TEXT,
    profile_json    TEXT,
    outcome         TEXT    DEFAULT 'running',
    peak_temp       REAL    DEFAULT 0,
    total_cost      REAL    DEFAULT 0,
    currency_type   TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    firing_id   INTEGER NOT NULL,
    runtime_s   REAL    NOT NULL,
    target_temp REAL,
    actual_temp REAL,
    heat        REAL,
    heat_rate   REAL,
    pid_out     REAL,
    FOREIGN KEY (firing_id) REFERENCES firings(id)
);

CREATE INDEX IF NOT EXISTS idx_samples_firing ON samples(firing_id);
CREATE INDEX IF NOT EXISTS idx_firings_started ON firings(started_at);
"""


class FiringHistory:
    """Thread-safe SQLite recorder. Cheap singleton-ish: instantiated
    once per process by the oven."""

    def __init__(self):
        self.enabled = bool(getattr(config, "history_enabled", False))
        self.db_path = getattr(config, "history_db_path", None)
        self.sample_interval = float(
            getattr(config, "history_sample_interval_seconds", 30)
        )
        self._lock = threading.RLock()
        self.current_firing_id = None
        self._last_sample_runtime = None

        if not self.enabled or not self.db_path:
            log.info("firing history: disabled")
            return

        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with self._connect() as conn:
                conn.executescript(SCHEMA)
            log.info("firing history: ready at %s", self.db_path)
        except Exception:
            log.exception("firing history: failed to initialize")
            self.enabled = False

    def _connect(self):
        # check_same_thread=False so the recorder can be called from
        # the oven thread; we serialize ourselves with self._lock.
        return sqlite3.connect(
            self.db_path,
            timeout=5,
            check_same_thread=False,
        )

    def start_firing(self, profile_name, profile_data, currency_type=""):
        if not self.enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO firings (started_at, profile_name, "
                    "profile_json, outcome, currency_type) "
                    "VALUES (?, ?, ?, 'running', ?)",
                    (
                        datetime.datetime.utcnow().isoformat(),
                        profile_name,
                        json.dumps(profile_data),
                        currency_type,
                    ),
                )
                self.current_firing_id = cur.lastrowid
                self._last_sample_runtime = None
            log.info("firing history: started run #%d (%s)",
                     self.current_firing_id, profile_name)
            return self.current_firing_id
        except Exception:
            log.exception("firing history: start_firing failed")
            return None

    def record_sample(self, state):
        """Insert a downsampled sample row. Called every duty cycle by
        the oven; we honor the configured sample_interval here so we
        do not blow up the DB."""
        if not self.enabled or self.current_firing_id is None:
            return
        runtime = float(state.get("runtime", 0))
        if (self._last_sample_runtime is not None and
                runtime - self._last_sample_runtime < self.sample_interval):
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO samples (firing_id, runtime_s, target_temp, "
                    "actual_temp, heat, heat_rate, pid_out) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.current_firing_id,
                        runtime,
                        state.get("target", 0),
                        state.get("temperature", 0),
                        state.get("heat", 0),
                        state.get("heat_rate", 0),
                        (state.get("pidstats") or {}).get("out", 0),
                    ),
                )
                # keep firings.peak_temp + total_cost up to date so the
                # UI can show partial info even if the run never ended
                conn.execute(
                    "UPDATE firings SET peak_temp = MAX(peak_temp, ?), "
                    "total_cost = ? WHERE id = ?",
                    (
                        state.get("temperature", 0),
                        state.get("cost", 0),
                        self.current_firing_id,
                    ),
                )
            self._last_sample_runtime = runtime
        except Exception:
            log.exception("firing history: record_sample failed")

    def end_firing(self, outcome, state=None, notes=""):
        if not self.enabled or self.current_firing_id is None:
            return
        try:
            with self._lock, self._connect() as conn:
                cost = (state or {}).get("cost", 0)
                conn.execute(
                    "UPDATE firings SET ended_at = ?, outcome = ?, "
                    "total_cost = ?, notes = ? WHERE id = ?",
                    (
                        datetime.datetime.utcnow().isoformat(),
                        outcome,
                        cost,
                        notes,
                        self.current_firing_id,
                    ),
                )
            log.info("firing history: ended run #%d (%s)",
                     self.current_firing_id, outcome)
        except Exception:
            log.exception("firing history: end_firing failed")
        finally:
            self.current_firing_id = None
            self._last_sample_runtime = None

    # ------------------------------------------------------------------
    # Read APIs - used by the web UI

    def list_firings(self, limit=50):
        if not self.enabled:
            return []
        try:
            with self._lock, self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, started_at, ended_at, profile_name, "
                    "outcome, peak_temp, total_cost, currency_type, notes "
                    "FROM firings ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            log.exception("firing history: list_firings failed")
            return []

    def get_firing(self, firing_id):
        if not self.enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM firings WHERE id = ?",
                    (firing_id,),
                ).fetchone()
                if row is None:
                    return None
                samples = conn.execute(
                    "SELECT runtime_s, target_temp, actual_temp, "
                    "heat, heat_rate, pid_out "
                    "FROM samples WHERE firing_id = ? ORDER BY runtime_s",
                    (firing_id,),
                ).fetchall()
                d = dict(row)
                if d.get("profile_json"):
                    try:
                        d["profile"] = json.loads(d["profile_json"])
                    except json.JSONDecodeError:
                        d["profile"] = None
                d["samples"] = [dict(s) for s in samples]
                return d
        except Exception:
            log.exception("firing history: get_firing failed")
            return None

    def delete_firing(self, firing_id):
        if not self.enabled:
            return False
        try:
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM samples WHERE firing_id = ?",
                             (firing_id,))
                conn.execute("DELETE FROM firings WHERE id = ?",
                             (firing_id,))
            return True
        except Exception:
            log.exception("firing history: delete_firing failed")
            return False
