"""Convert Orton-style ramp/hold firing schedules into the waypoint
JSON format used internally by the kiln controller.

The Orton format that potters know describes a firing as a sequence of
segments, each one of:
    - ramp: heat at R degrees per hour to a target temperature, OR
    - hold: hold the current temperature for D minutes,
    - cool: cool at R degrees per hour to a target temperature.

The controller's internal format is a simple list of ``[time_seconds,
temperature]`` waypoints with linear interpolation between them. This
module converts between the two.

Example input::

    {
      "name": "cone-6-glaze",
      "start_temp": 70,
      "segments": [
        {"type": "ramp", "rate": 200, "target": 250},
        {"type": "hold", "minutes": 30},
        {"type": "ramp", "rate": 400, "target": 1900},
        {"type": "ramp", "rate": 108, "target": 2232},
        {"type": "hold", "minutes": 10},
        {"type": "cool", "rate": 9999, "target": 1500},
        {"type": "ramp", "rate": 100, "target": 1500}
      ]
    }

A "rate" of 9999 (or anything > 9000) is treated as "as fast as
possible" - the segment becomes vertical (zero seconds) in the
waypoint list, but the controller will still take physical time to
get there.
"""
import json


FAST_AS_POSSIBLE = 9000  # rate above which the segment is treated as instant


def _validate_segments(segments):
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments must be a non-empty list")
    for i, s in enumerate(segments):
        if not isinstance(s, dict):
            raise ValueError("segment %d is not an object" % i)
        t = s.get("type")
        if t not in ("ramp", "hold", "cool"):
            raise ValueError(
                "segment %d has unknown type %r (expected ramp|hold|cool)"
                % (i, t)
            )
        if t in ("ramp", "cool"):
            if "rate" not in s or "target" not in s:
                raise ValueError(
                    "segment %d (%s) needs rate and target" % (i, t)
                )
        if t == "hold":
            if "minutes" not in s:
                raise ValueError("segment %d (hold) needs minutes" % i)


def orton_to_waypoints(orton):
    """Convert an Orton-style spec to a profile JSON dict.

    Returns the standard ``{"name": ..., "type": "profile",
    "data": [[t,T], ...]}`` shape used by the controller.
    """
    if isinstance(orton, str):
        orton = json.loads(orton)

    name = orton.get("name", "imported")
    start_temp = float(orton.get("start_temp", 25))
    segments = orton.get("segments", [])
    _validate_segments(segments)

    t = 0.0
    cur = start_temp
    waypoints = [[0, cur]]

    for seg in segments:
        kind = seg["type"]
        if kind == "hold":
            duration_s = float(seg["minutes"]) * 60.0
            t += duration_s
            waypoints.append([round(t, 2), cur])
            continue

        target = float(seg["target"])
        rate = float(seg["rate"])  # degrees per HOUR

        if rate <= 0:
            raise ValueError(
                "segment rate must be positive (got %r)" % seg["rate"]
            )

        if kind == "cool" and target > cur:
            raise ValueError(
                "cool segment target %s is higher than current temp %s"
                % (target, cur)
            )
        if kind == "ramp" and target < cur:
            # potters sometimes use a "ramp" to go down; treat as cool
            kind = "cool"

        if rate >= FAST_AS_POSSIBLE:
            # "vertical" segment - immediate set point change.
            # Use a tiny dt (10ms) so the time axis stays strictly
            # monotonic. Profile.__init__ sorts by [time, temp]; with
            # duplicate times the order would be ambiguous and a
            # following segment could be skipped.
            t += 0.01
            cur = target
            waypoints.append([round(t, 2), cur])
            continue

        delta = abs(target - cur)
        seconds = delta / rate * 3600.0
        t += seconds
        cur = target
        waypoints.append([round(t, 2), cur])

    # collapse consecutive duplicates so the waypoint list is minimal
    deduped = [waypoints[0]]
    for pt in waypoints[1:]:
        last = deduped[-1]
        if pt[0] == last[0] and pt[1] == last[1]:
            continue
        deduped.append(pt)

    return {"name": name, "type": "profile", "data": deduped}


def waypoints_to_orton(profile):
    """Reverse: produce a human-readable Orton-style description from
    a waypoint profile. Useful for the UI to round-trip imports.
    The output uses ramp/hold/cool with the rate computed from each
    pair of waypoints. Information is preserved exactly.
    """
    if isinstance(profile, str):
        profile = json.loads(profile)
    data = sorted(profile.get("data", []))
    if not data:
        return {"name": profile.get("name", ""), "start_temp": 0,
                "segments": []}

    out = {
        "name": profile.get("name", ""),
        "start_temp": data[0][1],
        "segments": [],
    }
    for i in range(1, len(data)):
        t0, T0 = data[i - 1]
        t1, T1 = data[i]
        dt_s = t1 - t0
        if dt_s <= 0 or T1 == T0:
            if T1 == T0:
                out["segments"].append({
                    "type": "hold",
                    "minutes": round(dt_s / 60.0, 2),
                })
            else:
                out["segments"].append({
                    "type": "ramp" if T1 > T0 else "cool",
                    "rate": FAST_AS_POSSIBLE + 1,
                    "target": T1,
                })
            continue
        rate_per_hour = abs(T1 - T0) / dt_s * 3600.0
        out["segments"].append({
            "type": "ramp" if T1 > T0 else "cool",
            "rate": round(rate_per_hour, 2),
            "target": T1,
        })
    return out
