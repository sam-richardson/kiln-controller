#!/usr/bin/env python
"""kiln-controller entry point.

Routes:
    GET  /                          redirect to UI
    GET  /picoreflow/<path>         static UI files

    HTTP API (POST JSON to /api):
        cmd=run    {profile}            start a firing
        cmd=stop                        abort the run
        cmd=memo   {memo}               annotate the log
        cmd=stats                       return PID stats
    GET  /api/stats                     PID stats (snapshot)
    GET  /api/config                    current config snapshot (UI)
    POST /api/config                    update a subset of safe config keys
    GET  /api/history                   list recent firings
    GET  /api/history/<id>              full firing detail (target+actual curves)
    DELETE /api/history/<id>            delete a firing
    POST /api/profile/import            convert an Orton-style spec to a
                                        waypoint profile (saves it too)
    POST /api/auth/change-password      rotate the controller password

    WebSockets:
        /control    run/stop commands
        /storage    list/save/delete profiles
        /config     stream the current config to the client
        /status     stream live oven state to the client
"""
import json
import logging
import os
import sys
import time

import bottle
import gevent
import geventwebsocket
from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket import WebSocketError

import config

logging.basicConfig(level=config.log_level, format=config.log_format)
log = logging.getLogger("kiln-controller")
log.info("Starting kiln controller")

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir + "/lib/")
profile_path = config.kiln_profiles_directory

import auth  # noqa: E402  - sys.path was just patched
import notifications  # noqa: E402
from oven import SimulatedOven, RealOven, Profile  # noqa: E402
from ovenWatcher import OvenWatcher  # noqa: E402
from profile_importer import orton_to_waypoints  # noqa: E402

app = bottle.Bottle()

# Initialize auth file (no-op if disabled).
auth.init()

if config.simulate is True:
    log.info("this is a simulation")
    oven = SimulatedOven()
else:
    log.info("this is a real kiln")
    oven = RealOven()
ovenWatcher = OvenWatcher(oven)
oven.set_ovenwatcher(ovenWatcher)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _check_auth_or_401():
    """Returns None if request is authorized, else a 401 response body."""
    if auth.check_request_auth():
        return None
    bottle.response.headers["WWW-Authenticate"] = (
        'Basic realm="kiln-controller"'
    )
    bottle.response.status = 401
    return {"success": False, "error": "auth required"}


def _json_request_body():
    try:
        return bottle.request.json or {}
    except Exception:
        return {}


# --------------------------------------------------------------------- #
# Static + index
# --------------------------------------------------------------------- #


@app.route("/")
def index():
    return bottle.redirect("/picoreflow/index.html")


@app.route("/picoreflow/:filename#.*#")
def send_static(filename):
    log.debug("serving %s", filename)
    return bottle.static_file(
        filename,
        root=os.path.join(
            os.path.dirname(os.path.realpath(sys.argv[0])), "public"
        ),
    )


# --------------------------------------------------------------------- #
# /api - control + stats
# --------------------------------------------------------------------- #


@app.get("/api/stats")
def api_stats():
    log.info("/api/stats command received")
    if hasattr(oven, "pid") and hasattr(oven.pid, "pidstats"):
        return json.dumps(oven.pid.pidstats)


@app.post("/api")
def api():
    err = _check_auth_or_401()
    if err is not None:
        return err

    body = _json_request_body()
    log.info("/api is alive")

    cmd = body.get("cmd")

    if cmd == "run":
        wanted = body["profile"]
        log.info("api requested run of profile = %s", wanted)
        startat = body.get("startat", 0) or 0
        allow_seek = startat <= 0

        profile = find_profile(wanted)
        if profile is None:
            return {"success": False,
                    "error": "profile %s not found" % wanted}

        profile_obj = Profile(json.dumps(profile))
        oven.run_profile(profile_obj, startat=startat, allow_seek=allow_seek)
        ovenWatcher.record(profile_obj)

    elif cmd == "stop":
        log.info("api stop command received")
        oven.abort_run(outcome="aborted", reason="user stop via /api")

    elif cmd == "memo":
        log.info("api memo command received")
        memo = body.get("memo")
        log.info("memo=%s", memo)

    elif cmd == "stats":
        log.info("api stats command received")
        if hasattr(oven, "pid") and hasattr(oven.pid, "pidstats"):
            return json.dumps(oven.pid.pidstats)

    return {"success": True}


# --------------------------------------------------------------------- #
# /api/config - read and write a safe subset of config
# --------------------------------------------------------------------- #


# keys that the web UI is allowed to modify at runtime
EDITABLE_CONFIG_KEYS = {
    # tuning
    "pid_kp", "pid_ki", "pid_kd",
    "pid_control_window",
    "pid_d_spike_limit_enabled",
    "pid_d_spike_limit",
    "pid_d_filter_alpha",
    "throttle_below_temp", "throttle_percent",
    # economics
    "kwh_rate", "kw_elements", "currency_type",
    # display
    "time_scale_slope", "time_scale_profile",
    # safety
    "emergency_shutoff_temp",
    "kiln_must_catch_up",
    "hold_auto_extend",
    "hold_at_temp_tolerance",
    "thermocouple_offset",
    "element_failure_detection",
    "element_failure_min_full_duty_seconds",
    "element_failure_min_heat_rate",
    "element_failure_min_temp",
    "cool_down_safe_open_temp",
    "cool_down_notify_on_complete",
    "cool_down_notify_on_safe_open",
    "multi_tc_delta_alert_degrees",
    # notifications - safe to edit at runtime
    "notify_email_enabled", "notify_email_to",
    "notify_pushover_enabled",
    "notify_ntfy_enabled", "notify_ntfy_topic",
    "notify_slack_enabled",
}


@app.get("/api/config")
def api_config_get():
    err = _check_auth_or_401()
    if err is not None:
        return err
    out = {}
    for k in EDITABLE_CONFIG_KEYS:
        if hasattr(config, k):
            out[k] = getattr(config, k)
    bottle.response.content_type = "application/json"
    return json.dumps(out)


@app.post("/api/config")
def api_config_set():
    err = _check_auth_or_401()
    if err is not None:
        return err
    body = _json_request_body()
    if not isinstance(body, dict):
        return {"success": False, "error": "expected JSON object"}
    changed = {}
    rejected = []
    for k, v in body.items():
        if k not in EDITABLE_CONFIG_KEYS:
            rejected.append(k)
            continue
        setattr(config, k, v)
        changed[k] = v
    log.info("/api/config updated keys=%s rejected=%s",
             list(changed.keys()), rejected)
    return {"success": True, "changed": changed, "rejected": rejected}


# --------------------------------------------------------------------- #
# /api/history - past firings (#8)
# --------------------------------------------------------------------- #


@app.get("/api/history")
def api_history_list():
    err = _check_auth_or_401()
    if err is not None:
        return err
    try:
        limit = int(bottle.request.query.get("limit", 50))
    except ValueError:
        limit = 50
    bottle.response.content_type = "application/json"
    return json.dumps({"firings": oven.history.list_firings(limit=limit)})


@app.get("/api/history/<firing_id:int>")
def api_history_get(firing_id):
    err = _check_auth_or_401()
    if err is not None:
        return err
    firing = oven.history.get_firing(firing_id)
    if firing is None:
        bottle.response.status = 404
        return {"success": False, "error": "not found"}
    bottle.response.content_type = "application/json"
    return json.dumps(firing)


@app.delete("/api/history/<firing_id:int>")
def api_history_delete(firing_id):
    err = _check_auth_or_401()
    if err is not None:
        return err
    ok = oven.history.delete_firing(firing_id)
    return {"success": ok}


# --------------------------------------------------------------------- #
# /api/profile/import - Orton ramp/hold spec -> waypoint profile (#10)
# --------------------------------------------------------------------- #


@app.post("/api/profile/import")
def api_profile_import():
    err = _check_auth_or_401()
    if err is not None:
        return err
    body = _json_request_body()
    save = bool(body.pop("save", True))
    try:
        profile = orton_to_waypoints(body)
    except (ValueError, KeyError, TypeError) as exc:
        bottle.response.status = 400
        return {"success": False, "error": str(exc)}
    if save:
        save_profile(profile, force=True)
    return {"success": True, "profile": profile}


# --------------------------------------------------------------------- #
# /api/auth/change-password - rotate password (#11)
# --------------------------------------------------------------------- #


@app.post("/api/auth/change-password")
def api_change_password():
    if not auth.is_enabled():
        return {"success": False, "error": "auth disabled"}
    err = _check_auth_or_401()
    if err is not None:
        return err
    body = _json_request_body()
    if not body.get("old_password") or not body.get("new_password"):
        return {"success": False,
                "error": "old_password and new_password required"}
    if len(body["new_password"]) < 6:
        return {"success": False,
                "error": "new password must be at least 6 chars"}
    ok = auth.change_password(body["old_password"], body["new_password"])
    return {"success": ok, "error": None if ok else "old password incorrect"}


# --------------------------------------------------------------------- #
# Profile helpers
# --------------------------------------------------------------------- #


def find_profile(wanted):
    profiles = get_profiles()
    json_profiles = json.loads(profiles)
    for profile in json_profiles:
        if profile["name"] == wanted:
            return profile
    return None


def get_profiles():
    try:
        profile_files = os.listdir(profile_path)
        profile_files.sort()
    except OSError:
        profile_files = []
    profiles = []
    for filename in profile_files:
        if filename.startswith("._"):
            continue
        if filename.endswith(".json"):
            with open(os.path.join(profile_path, filename), "r") as f:
                profiles.append(json.load(f))
    return json.dumps(profiles)


def save_profile(profile, force=False):
    profile_json = json.dumps(profile)
    filename = profile["name"] + ".json"
    filepath = os.path.join(profile_path, filename)
    if not force and os.path.exists(filepath):
        log.error("Could not write, %s already exists", filepath)
        return False
    with open(filepath, "w+") as f:
        f.write(profile_json)
    log.info("Wrote %s", filepath)
    return True


def delete_profile(profile):
    filename = profile["name"] + ".json"
    filepath = os.path.join(profile_path, filename)
    os.remove(filepath)
    log.info("Deleted %s", filepath)
    return True


def get_config_for_ui():
    """Subset of config served to the read-only UI websocket."""
    return json.dumps(
        {
            "time_scale_slope": config.time_scale_slope,
            "time_scale_profile": config.time_scale_profile,
            "kwh_rate": config.kwh_rate,
            "currency_type": config.currency_type,
            "auth_enabled": config.auth_enabled,
            "history_enabled": config.history_enabled,
            "multiple_thermocouples": getattr(
                config, "multiple_thermocouples", False
            ),
        }
    )


# --------------------------------------------------------------------- #
# Websockets
# --------------------------------------------------------------------- #


def get_websocket_from_request():
    env = bottle.request.environ
    wsock = env.get("wsgi.websocket")
    if not wsock:
        bottle.abort(400, "Expected WebSocket request.")
    if not auth.check_websocket_auth(env):
        try:
            wsock.close()
        except Exception:
            pass
        bottle.abort(401, "auth required")
    return wsock


@app.route("/control")
def handle_control():
    wsock = get_websocket_from_request()
    log.info("websocket (control) opened")
    profile = None
    while True:
        try:
            message = wsock.receive()
            if message:
                log.info("Received (control): %s", message)
                msgdict = json.loads(message)
                if msgdict.get("cmd") == "RUN":
                    log.info("RUN command received")
                    profile_obj = msgdict.get("profile")
                    if profile_obj:
                        profile = Profile(json.dumps(profile_obj))
                    if profile is not None:
                        oven.run_profile(profile)
                        ovenWatcher.record(profile)
                elif msgdict.get("cmd") == "SIMULATE":
                    log.info("SIMULATE command received")
                elif msgdict.get("cmd") == "STOP":
                    log.info("Stop command received")
                    oven.abort_run(outcome="aborted",
                                   reason="user stop via /control")
            time.sleep(1)
        except WebSocketError as e:
            log.error(e)
            break
    log.info("websocket (control) closed")


@app.route("/storage")
def handle_storage():
    wsock = get_websocket_from_request()
    log.info("websocket (storage) opened")
    while True:
        try:
            message = wsock.receive()
            if not message:
                break
            log.debug("websocket (storage) received: %s", message)
            try:
                msgdict = json.loads(message)
            except Exception:
                msgdict = {}

            if message == "GET":
                log.info("GET command received")
                wsock.send(get_profiles())
            elif msgdict.get("cmd") == "DELETE":
                log.info("DELETE command received")
                profile_obj = msgdict.get("profile")
                if delete_profile(profile_obj):
                    msgdict["resp"] = "OK"
                wsock.send(json.dumps(msgdict))
            elif msgdict.get("cmd") == "PUT":
                log.info("PUT command received")
                profile_obj = msgdict.get("profile")
                force = True
                if profile_obj:
                    if save_profile(profile_obj, force):
                        msgdict["resp"] = "OK"
                    else:
                        msgdict["resp"] = "FAIL"
                    log.debug("websocket (storage) sent: %s", message)
                    wsock.send(json.dumps(msgdict))
                    wsock.send(get_profiles())
            time.sleep(1)
        except WebSocketError:
            break
    log.info("websocket (storage) closed")


@app.route("/config")
def handle_config():
    wsock = get_websocket_from_request()
    log.info("websocket (config) opened")
    while True:
        try:
            wsock.receive()
            wsock.send(get_config_for_ui())
        except WebSocketError:
            break
        time.sleep(1)
    log.info("websocket (config) closed")


@app.route("/status")
def handle_status():
    wsock = get_websocket_from_request()
    ovenWatcher.add_observer(wsock)
    log.info("websocket (status) opened")
    while True:
        try:
            message = wsock.receive()
            wsock.send("Your message was: %r" % message)
        except WebSocketError:
            break
        time.sleep(1)
    log.info("websocket (status) closed")


# --------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------- #

def main():
    ip = getattr(config, "listening_host", "0.0.0.0")
    port = config.listening_port
    log.info("listening on %s:%d", ip, port)
    server = WSGIServer((ip, port), app, handler_class=WebSocketHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
