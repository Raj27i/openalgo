# blueprints/stbt.py
#
# STBT tab backend — a thin façade over the /python Strategy Host.
#
# Each STBT config is a normal python-host strategy under the hood:
#   strategies/stbt/{id}_config.json   — the strategy parameters (this module owns it)
#   strategies/scripts/{id}.py         — auto-generated launcher stub that runs
#                                        strategies/stbt/engine.py with STRATEGY_ID env
#   STRATEGY_CONFIGS[id]               — the host's registry entry (marked "stbt": True)
#   strategies/stbt/{id}_status.json   — live snapshot written by the engine for the UI
#
# Process management (subprocess isolation, scheduling, crash reaping, SSE) is
# delegated entirely to blueprints/python_strategy.py — nothing is reimplemented.

import json
import os
import re
from pathlib import Path

from flask import Blueprint, jsonify, request, session

from blueprints.python_strategy import (
    STRATEGIES_DIR,
    STRATEGY_CONFIGS,
    get_ist_time,
    save_configs,
    schedule_strategy,
    start_strategy_process,
    stop_strategy_process,
    unschedule_strategy,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

stbt_bp = Blueprint("stbt_bp", __name__, url_prefix="/stbt")

STBT_DIR = Path("strategies") / "stbt"

# Underlying → host exchange (drives the host's holiday/session gating).
SUPPORTED_UNDERLYINGS = {
    "SENSEX": "BSE",
    "BANKEX": "BSE",
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "FINNIFTY": "NSE",
    "MIDCPNIFTY": "NSE",
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Editable engine parameters: name -> (type, min, max). Times validated separately.
_NUMERIC_PARAMS = {
    "entry_drop_pct": (float, 0.0, 50.0),
    "sl_pct": (float, 1.0, 200.0),
    "max_reentries": (int, 0, 5),
    "hedge_target_premium": (float, 1.0, 1000.0),
    "lot_multiplier": (int, 1, 100),
    "max_loss": (float, 0.0, 10_000_000.0),  # session kill switch ₹; 0 = disabled
}
_TIME_PARAMS = ("entry_time", "hedge_time", "ws_close_time", "day2_open_time", "force_exit_time")
_REENTRY_METHODS = {"CANDLE_CLOSE", "LTP"}

LAUNCHER_TEMPLATE = '''#!/usr/bin/env python
"""Auto-generated STBT launcher — managed by the /stbt tab.

Do not edit: parameters live in strategies/stbt/{strategy_id}_config.json
and the trading logic in strategies/stbt/engine.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # strategies/

from stbt.engine import main

if __name__ == "__main__":
    main()
'''


def _config_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_config.json"


def _status_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_status.json"


def _history_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_history.json"


def _runtime_paths(strategy_id: str) -> list[Path]:
    return [
        _config_path(strategy_id),
        _status_path(strategy_id),
        _history_path(strategy_id),
        STBT_DIR / f"{strategy_id}_state.json",
        STBT_DIR / f"{strategy_id}_order_intent.json",
        STRATEGIES_DIR / f"{strategy_id}.py",
    ]


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read {path}: {exc}")
        return None


def _write_json_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _verify_stbt(strategy_id: str, user_id: str):
    """Validate id shape, existence, STBT marker, and ownership.

    Returns (config_entry, None) on success or (None, (response, code)) on error.
    """
    if not strategy_id or ".." in strategy_id or "/" in strategy_id or "\\" in strategy_id:
        return None, (jsonify({"status": "error", "message": "Invalid strategy ID"}), 400)
    entry = STRATEGY_CONFIGS.get(strategy_id)
    if not entry or not entry.get("stbt"):
        return None, (jsonify({"status": "error", "message": "STBT config not found"}), 404)
    owner = entry.get("user_id")
    if owner and owner != user_id:
        return None, (jsonify({"status": "error", "message": "Unauthorized"}), 403)
    return entry, None


def _validate_params(data: dict, partial: bool = False) -> tuple[dict, str | None]:
    """Validate and normalize engine parameters from the request body.

    With partial=True only validates keys that are present (for updates).
    Returns (clean_params, error_message).
    """
    clean: dict = {}

    if "underlying" in data or not partial:
        underlying = str(data.get("underlying", "SENSEX")).upper().strip()
        if underlying not in SUPPORTED_UNDERLYINGS:
            return {}, f"Unsupported underlying: {underlying}"
        clean["underlying"] = underlying

    for key, (cast, lo, hi) in _NUMERIC_PARAMS.items():
        if key not in data:
            if partial:
                continue
            data[key] = None  # fall through to default below
        if data.get(key) is None:
            continue  # engine default applies
        try:
            value = cast(data[key])
        except (TypeError, ValueError):
            return {}, f"Invalid value for {key}"
        if not lo <= value <= hi:
            return {}, f"{key} must be between {lo} and {hi}"
        clean[key] = value

    for key in _TIME_PARAMS:
        if key not in data or data.get(key) in (None, ""):
            continue
        value = str(data[key]).strip()
        if not _TIME_RE.match(value):
            return {}, f"{key} must be HH:MM (24-hour)"
        clean[key] = value

    if data.get("reentry_method"):
        method = str(data["reentry_method"]).upper().strip()
        if method not in _REENTRY_METHODS:
            return {}, f"reentry_method must be one of {sorted(_REENTRY_METHODS)}"
        clean["reentry_method"] = method

    if "allow_day2_reentry" in data and data["allow_day2_reentry"] is not None:
        clean["allow_day2_reentry"] = bool(data["allow_day2_reentry"])

    if "telegram_alerts" in data and data["telegram_alerts"] is not None:
        clean["telegram_alerts"] = bool(data["telegram_alerts"])

    return clean, None


def _validate_schedule(data: dict) -> tuple[str, str, list[str], str | None]:
    start = str(data.get("schedule_start") or "09:10").strip()
    stop = str(data.get("schedule_stop") or "16:00").strip()
    days = data.get("schedule_days") or ["mon", "tue", "wed", "thu", "fri"]
    if not _TIME_RE.match(start) or not _TIME_RE.match(stop):
        return "", "", [], "Schedule times must be HH:MM (24-hour)"
    if (
        not isinstance(days, list)
        or not days
        or any(str(d).lower() not in _VALID_DAYS for d in days)
    ):
        return "", "", [], "schedule_days must be a non-empty list of mon..sun"
    return start, stop, [str(d).lower() for d in days], None


def _serialize(strategy_id: str, entry: dict) -> dict:
    params = _read_json(_config_path(strategy_id)) or {}
    return {
        "strategy_id": strategy_id,
        "name": entry.get("name", strategy_id),
        "underlying": params.get("underlying", "SENSEX"),
        "params": params,
        "is_running": entry.get("is_running", False),
        "is_scheduled": entry.get("is_scheduled", False),
        "is_error": entry.get("is_error", False),
        "error_message": entry.get("error_message"),
        "manually_stopped": entry.get("manually_stopped", False),
        "schedule_start": entry.get("schedule_start"),
        "schedule_stop": entry.get("schedule_stop"),
        "schedule_days": entry.get("schedule_days", []),
        "created_at": entry.get("created_at"),
        "last_started": entry.get("last_started"),
        "last_stopped": entry.get("last_stopped"),
    }


# ─── Routes ──────────────────────────────────────────────────────────────────


@stbt_bp.route("/api/configs", methods=["GET"])
@check_session_validity
def list_configs():
    user_id = session.get("user")
    items = [
        _serialize(sid, entry)
        for sid, entry in STRATEGY_CONFIGS.items()
        if entry.get("stbt") and (not entry.get("user_id") or entry.get("user_id") == user_id)
    ]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return jsonify({"status": "success", "configs": items})


@stbt_bp.route("/api/configs", methods=["POST"])
@check_session_validity
def create_config():
    user_id = session.get("user")
    if not user_id:
        return jsonify({"status": "error", "message": "Session expired"}), 401

    data = request.get_json(silent=True) or {}
    params, err = _validate_params(data)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    start, stop, days, err = _validate_schedule(data)
    if err:
        return jsonify({"status": "error", "message": err}), 400

    underlying = params["underlying"]
    ist_now = get_ist_time()
    strategy_id = f"stbt_{underlying.lower()}_{ist_now.strftime('%Y%m%d%H%M%S')}"
    raw_name = str(data.get("name") or f"{underlying} STBT").strip()[:100]

    try:
        # Owner username rides in the params JSON so the engine subprocess can
        # resolve the Telegram chat id without host-only context.
        params["user_id"] = user_id
        _write_json_atomic(_config_path(strategy_id), params)

        launcher_path = STRATEGIES_DIR / f"{strategy_id}.py"
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        launcher_path.write_text(
            LAUNCHER_TEMPLATE.format(strategy_id=strategy_id), encoding="utf-8"
        )
        if os.name != "nt":
            try:
                os.chmod(launcher_path, 0o755)
            except Exception:
                pass

        STRATEGY_CONFIGS[strategy_id] = {
            "name": raw_name,
            "file_path": str(launcher_path),
            "file_name": f"{strategy_id}.py",
            "exchange": SUPPORTED_UNDERLYINGS[underlying],
            "is_running": False,
            "is_scheduled": True,
            "created_at": ist_now.isoformat(),
            "user_id": user_id,
            "schedule_start": start,
            "schedule_stop": stop,
            "schedule_days": days,
            "stbt": True,
        }
        save_configs()
        schedule_strategy(strategy_id, start_time=start, stop_time=stop, days=days)
    except Exception as e:
        logger.exception(f"Failed to create STBT config: {e}")
        # Roll back partial artifacts so a failed create leaves nothing behind.
        STRATEGY_CONFIGS.pop(strategy_id, None)
        for path in _runtime_paths(strategy_id):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return jsonify({"status": "error", "message": str(e)}), 500

    logger.info(f"STBT config created: {strategy_id} ({underlying}) by {user_id}")
    return jsonify(
        {
            "status": "success",
            "message": f'STBT config "{raw_name}" created',
            "config": _serialize(strategy_id, STRATEGY_CONFIGS[strategy_id]),
        }
    )


@stbt_bp.route("/api/configs/<strategy_id>", methods=["PUT"])
@check_session_validity
def update_config(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error
    if entry.get("is_running"):
        return jsonify(
            {"status": "error", "message": "Stop the strategy before editing its parameters"}
        ), 409

    data = request.get_json(silent=True) or {}
    updates, err = _validate_params(data, partial=True)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    if "underlying" in updates and updates["underlying"] != (
        (_read_json(_config_path(strategy_id)) or {}).get("underlying")
    ):
        return jsonify(
            {"status": "error", "message": "Underlying cannot be changed — create a new config"}
        ), 400
    updates.pop("underlying", None)

    params = _read_json(_config_path(strategy_id)) or {}
    params.update(updates)
    try:
        _write_json_atomic(_config_path(strategy_id), params)

        if data.get("name"):
            entry["name"] = str(data["name"]).strip()[:100]

        if any(k in data for k in ("schedule_start", "schedule_stop", "schedule_days")):
            start, stop, days, err = _validate_schedule(
                {
                    "schedule_start": data.get("schedule_start") or entry.get("schedule_start"),
                    "schedule_stop": data.get("schedule_stop") or entry.get("schedule_stop"),
                    "schedule_days": data.get("schedule_days") or entry.get("schedule_days"),
                }
            )
            if err:
                return jsonify({"status": "error", "message": err}), 400
            entry.update(
                {
                    "schedule_start": start,
                    "schedule_stop": stop,
                    "schedule_days": days,
                    "is_scheduled": True,
                }
            )
            unschedule_strategy(strategy_id)
            schedule_strategy(strategy_id, start_time=start, stop_time=stop, days=days)

        save_configs()
    except Exception as e:
        logger.exception(f"Failed to update STBT config {strategy_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify(
        {
            "status": "success",
            "message": "STBT config updated",
            "config": _serialize(strategy_id, entry),
        }
    )


@stbt_bp.route("/api/configs/<strategy_id>", methods=["DELETE"])
@check_session_validity
def delete_config(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    if entry.get("is_running"):
        success, message = stop_strategy_process(strategy_id)
        if not success:
            return jsonify(
                {"status": "error", "message": f"Could not stop before delete: {message}"}
            ), 500

    try:
        unschedule_strategy(strategy_id)
    except Exception as e:
        logger.warning(f"Unschedule during delete failed for {strategy_id}: {e}")

    STRATEGY_CONFIGS.pop(strategy_id, None)
    save_configs()
    for path in _runtime_paths(strategy_id):
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Could not remove {path}: {e}")

    logger.info(f"STBT config deleted: {strategy_id} by {user_id}")
    return jsonify({"status": "success", "message": "STBT config deleted"})


@stbt_bp.route("/api/start/<strategy_id>", methods=["POST"])
@check_session_validity
def start_config(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    entry.pop("manually_stopped", None)
    success, message = start_strategy_process(strategy_id)
    if not success:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "success", "message": message})


@stbt_bp.route("/api/stop/<strategy_id>", methods=["POST"])
@check_session_validity
def stop_config(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    success, message = stop_strategy_process(strategy_id)
    if success:
        # Mirror the host's stop route: block scheduler auto-restarts until
        # the user explicitly starts again.
        entry["manually_stopped"] = True
        save_configs()
        return jsonify({"status": "success", "message": message})
    return jsonify({"status": "error", "message": message}), 400


@stbt_bp.route("/api/status/<strategy_id>", methods=["GET"])
@check_session_validity
def get_status(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    status = _read_json(_status_path(strategy_id))
    return jsonify(
        {
            "status": "success",
            "is_running": entry.get("is_running", False),
            "is_error": entry.get("is_error", False),
            "error_message": entry.get("error_message"),
            "live": status,  # None until the engine writes its first snapshot
        }
    )


@stbt_bp.route("/api/history/<strategy_id>", methods=["GET"])
@check_session_validity
def get_history(strategy_id):
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    raw = _read_json(_history_path(strategy_id))
    records = raw if isinstance(raw, list) else []
    total_net = round(sum(r.get("net_pnl", 0) or 0 for r in records), 2)
    return jsonify(
        {
            "status": "success",
            "records": list(reversed(records)),  # newest first for the UI
            "total_net": total_net,
        }
    )
