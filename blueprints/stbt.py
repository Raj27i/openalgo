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
from database.auth_db import get_api_key_for_tradingview
from services.place_smart_order_service import place_smart_order
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

# Underlying → options exchange (mirror of engine INDEX_MAP), for the panic
# close-all path which flattens by symbol directly (independent of the engine).
_OPT_EXCHANGE = {
    "SENSEX": "BFO",
    "BANKEX": "BFO",
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Config kinds hosted by this tab. "stbt" = short strangle engine.py,
# "btst" = long paper-short-flip btst_engine.py.
_STRATEGY_TYPES = {"stbt", "btst"}

# Editable engine parameters per type: name -> (type, min, max).
# Times validated separately.
_NUMERIC_PARAMS = {
    "stbt": {
        "entry_drop_pct": (float, 0.0, 50.0),
        "sl_pct": (float, 1.0, 200.0),
        "max_reentries": (int, 0, 5),
        "hedge_target_premium": (float, 1.0, 1000.0),
        "lot_multiplier": (int, 1, 100),
        "max_loss": (float, 0.0, 10_000_000.0),  # session kill switch ₹; 0 = disabled
        "take_profit_pct": (float, 0.0, 99.0),  # combined profit target %; 0 = disabled
    },
    "btst": {
        "moneyness": (int, 1, 5),  # ITM-N CE strike selection
        "drop_pct": (float, 0.0, 50.0),  # paper-short trigger
        "vsl_pct": (float, 1.0, 200.0),  # virtual SL distance → real-buy trigger
        "real_sl_pct": (float, 1.0, 99.0),  # SL below the bought premium
        "be_trigger_pct": (float, 0.0, 200.0),  # D2 breakeven arm level; 0 = off
        "vix_max": (float, 0.0, 100.0),  # India VIX entry ceiling; 0 = disabled
        "dte_min": (int, 1, 30),
        "dte_max": (int, 1, 30),
        "lot_multiplier": (int, 1, 100),
        "max_loss": (float, 0.0, 10_000_000.0),
    },
}
_TIME_PARAMS = {
    "stbt": ("entry_time", "hedge_time", "ws_close_time", "day2_open_time", "force_exit_time"),
    "btst": (
        "ref_time",
        "entry_start_time",
        "entry_end_time",
        "ws_close_time",
        "day2_open_time",
        "force_exit_time",
    ),
}
_REENTRY_METHODS = {"CANDLE_CLOSE", "LTP"}
_CANDLE_SOURCES = {"WS", "HISTORY"}  # btst signal-candle source (CANDLE_CLOSE mode)
_TRIGGER_MODES = {"TICK", "CANDLE_CLOSE"}  # btst signal trigger mode

LAUNCHER_TEMPLATE = '''#!/usr/bin/env python
"""Auto-generated STBT launcher — managed by the /stbt tab.

Do not edit: parameters live in strategies/stbt/{strategy_id}_config.json
and the trading logic in strategies/stbt/{engine_module}.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # strategies/

from stbt.{engine_module} import main

if __name__ == "__main__":
    main()
'''

# strategy_type -> engine module under strategies/stbt/
_ENGINE_MODULES = {"stbt": "engine", "btst": "btst_engine"}


def _config_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_config.json"


def _status_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_status.json"


def _history_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_history.json"


def _journal_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_journal.json"


def _state_path(strategy_id: str) -> Path:
    return STBT_DIR / f"{strategy_id}_state.json"


def _runtime_paths(strategy_id: str) -> list[Path]:
    return [
        _config_path(strategy_id),
        _status_path(strategy_id),
        _history_path(strategy_id),
        _journal_path(strategy_id),
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


def _validate_params(
    data: dict, partial: bool = False, strategy_type: str = "stbt"
) -> tuple[dict, str | None]:
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

    for key, (cast, lo, hi) in _NUMERIC_PARAMS[strategy_type].items():
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

    if strategy_type == "btst":
        dte_min = clean.get("dte_min")
        dte_max = clean.get("dte_max")
        if dte_min is not None and dte_max is not None and dte_min > dte_max:
            return {}, "dte_min cannot be greater than dte_max"

    for key in _TIME_PARAMS[strategy_type]:
        if key not in data or data.get(key) in (None, ""):
            continue
        value = str(data[key]).strip()
        if not _TIME_RE.match(value):
            return {}, f"{key} must be HH:MM (24-hour)"
        clean[key] = value

    if strategy_type == "stbt":
        if data.get("reentry_method"):
            method = str(data["reentry_method"]).upper().strip()
            if method not in _REENTRY_METHODS:
                return {}, f"reentry_method must be one of {sorted(_REENTRY_METHODS)}"
            clean["reentry_method"] = method

        if "allow_day2_reentry" in data and data["allow_day2_reentry"] is not None:
            clean["allow_day2_reentry"] = bool(data["allow_day2_reentry"])

    if strategy_type == "btst" and data.get("trigger_mode"):
        mode = str(data["trigger_mode"]).upper().strip()
        if mode not in _TRIGGER_MODES:
            return {}, f"trigger_mode must be one of {sorted(_TRIGGER_MODES)}"
        clean["trigger_mode"] = mode

    if strategy_type == "btst" and data.get("candle_source"):
        source = str(data["candle_source"]).upper().strip()
        if source not in _CANDLE_SOURCES:
            return {}, f"candle_source must be one of {sorted(_CANDLE_SOURCES)}"
        clean["candle_source"] = source

    if strategy_type == "btst" and data.get("entry_weekdays") is not None:
        days = data["entry_weekdays"]
        if (
            not isinstance(days, list)
            or not days
            or any(str(d).lower() not in _VALID_DAYS for d in days)
        ):
            return {}, "entry_weekdays must be a non-empty list of mon..sun"
        clean["entry_weekdays"] = [str(d).lower() for d in days]

    if "telegram_alerts" in data and data["telegram_alerts"] is not None:
        clean["telegram_alerts"] = bool(data["telegram_alerts"])

    if "use_smart_exit" in data and data["use_smart_exit"] is not None:
        clean["use_smart_exit"] = bool(data["use_smart_exit"])

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
        "strategy_type": params.get("strategy_type", "stbt"),
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
    strategy_type = str(data.get("strategy_type") or "stbt").lower().strip()
    if strategy_type not in _STRATEGY_TYPES:
        return jsonify(
            {"status": "error", "message": f"strategy_type must be one of {sorted(_STRATEGY_TYPES)}"}
        ), 400
    params, err = _validate_params(data, strategy_type=strategy_type)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    start, stop, days, err = _validate_schedule(data)
    if err:
        return jsonify({"status": "error", "message": err}), 400

    underlying = params["underlying"]
    ist_now = get_ist_time()
    strategy_id = f"{strategy_type}_{underlying.lower()}_{ist_now.strftime('%Y%m%d%H%M%S')}"
    default_name = f"{underlying} {'BTST Flip' if strategy_type == 'btst' else 'STBT'}"
    raw_name = str(data.get("name") or default_name).strip()[:100]

    try:
        # Owner username rides in the params JSON so the engine subprocess can
        # resolve the Telegram chat id without host-only context. The type
        # rides there too so the engine and the panic path can read it back.
        params["user_id"] = user_id
        params["strategy_type"] = strategy_type
        _write_json_atomic(_config_path(strategy_id), params)

        launcher_path = STRATEGIES_DIR / f"{strategy_id}.py"
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        launcher_path.write_text(
            LAUNCHER_TEMPLATE.format(
                strategy_id=strategy_id, engine_module=_ENGINE_MODULES[strategy_type]
            ),
            encoding="utf-8",
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
    params = _read_json(_config_path(strategy_id)) or {}
    strategy_type = str(params.get("strategy_type", "stbt")).lower()
    if strategy_type not in _STRATEGY_TYPES:
        strategy_type = "stbt"
    if data.get("strategy_type") and str(data["strategy_type"]).lower() != strategy_type:
        return jsonify(
            {"status": "error", "message": "strategy_type cannot be changed — create a new config"}
        ), 400
    updates, err = _validate_params(data, partial=True, strategy_type=strategy_type)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    if "underlying" in updates and updates["underlying"] != params.get("underlying"):
        return jsonify(
            {"status": "error", "message": "Underlying cannot be changed — create a new config"}
        ), 400
    updates.pop("underlying", None)
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


def _owned_stbt_ids(user_id: str) -> set[str]:
    """STBT config ids owned by this user (or ownerless legacy configs)."""
    ids = set()
    for sid, entry in STRATEGY_CONFIGS.items():
        if not entry.get("stbt"):
            continue
        owner = entry.get("user_id")
        if not owner or owner == user_id:
            ids.add(sid)
    return ids


@stbt_bp.route("/api/analytics", methods=["GET"])
@check_session_validity
def get_analytics():
    """AlgoTest-style analytics seeded from STBT per-cycle journals.

    Aggregates every closed-cycle record (across the user's STBT configs, or a
    single config via ?config=) into per-day totals, a cumulative equity curve,
    and headline stats. Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD date filter.
    """
    user_id = session.get("user")
    owned = _owned_stbt_ids(user_id)

    config_filter = (request.args.get("config") or "").strip()
    if config_filter:
        if config_filter not in owned:
            return jsonify({"status": "error", "message": "STBT config not found"}), 404
        owned = {config_filter}

    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    # Collect cycle rows from each owned config's journal.
    rows = []
    for sid in owned:
        raw = _read_json(_journal_path(sid))
        if isinstance(raw, list):
            rows.extend(raw)

    # Per-day aggregation.
    daily: dict[str, dict] = {}
    for r in rows:
        d = str(r.get("trade_date", ""))
        if not d:
            continue
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        gross = float(r.get("gross", 0) or 0)
        charges = float(r.get("charges", 0) or 0)
        net = float(r.get("net", gross - charges) or 0)
        cell = daily.setdefault(
            d,
            {
                "date": d,
                "gross": 0.0,
                "charges": 0.0,
                "net": 0.0,
                "cycles": 0,
                "wins": 0,
                "losses": 0,
            },
        )
        cell["gross"] += gross
        cell["charges"] += charges
        cell["net"] += net
        cell["cycles"] += 1
        if net > 0:
            cell["wins"] += 1
        elif net < 0:
            cell["losses"] += 1

    daily_list = [
        {
            "date": c["date"],
            "gross": round(c["gross"], 2),
            "charges": round(c["charges"], 2),
            "net": round(c["net"], 2),
            "cycles": c["cycles"],
            "wins": c["wins"],
            "losses": c["losses"],
        }
        for c in sorted(daily.values(), key=lambda x: x["date"])
    ]

    # Cumulative equity curve (date-ordered).
    curve = []
    cum_net = cum_gross = 0.0
    for c in daily_list:
        cum_net += c["net"]
        cum_gross += c["gross"]
        curve.append(
            {
                "date": c["date"],
                "cumulative_net": round(cum_net, 2),
                "cumulative_gross": round(cum_gross, 2),
            }
        )

    win_days = sum(1 for c in daily_list if c["net"] > 0)
    loss_days = sum(1 for c in daily_list if c["net"] < 0)
    best = max(daily_list, key=lambda x: x["net"], default=None)
    worst = min(daily_list, key=lambda x: x["net"], default=None)
    totals = {
        "gross": round(sum(c["gross"] for c in daily_list), 2),
        "charges": round(sum(c["charges"] for c in daily_list), 2),
        "net": round(sum(c["net"] for c in daily_list), 2),
        "cycles": sum(c["cycles"] for c in daily_list),
        "trading_days": len(daily_list),
        "win_days": win_days,
        "loss_days": loss_days,
        "best_day": best,
        "worst_day": worst,
    }

    configs = []
    for sid in sorted(_owned_stbt_ids(user_id)):
        cfg_params = _read_json(_config_path(sid)) or {}
        configs.append(
            {
                "strategy_id": sid,
                "name": STRATEGY_CONFIGS[sid].get("name", sid),
                "underlying": cfg_params.get("underlying", ""),
                "strategy_type": cfg_params.get("strategy_type", "stbt"),
            }
        )

    return jsonify(
        {
            "status": "success",
            "daily": daily_list,
            "curve": curve,
            "totals": totals,
            "configs": configs,
        }
    )


def _open_symbols(strategy_id: str) -> list[str]:
    """Every option symbol this config could currently hold, from the live
    status + persisted state snapshots (deduped, order preserved).

    Over-inclusion is harmless: the panic flatten targets position_size=0, so
    a symbol already flat is a no-op. We'd rather flatten a stale symbol than
    miss a live one."""
    seen: dict[str, None] = {}
    for path in (_status_path(strategy_id), _state_path(strategy_id)):
        snap = _read_json(path)
        if not isinstance(snap, dict):
            continue
        for leg in snap.get("main_legs") or []:
            sym = (leg or {}).get("symbol")
            if sym:
                seen.setdefault(str(sym), None)
        hedge = snap.get("hedge")
        if isinstance(hedge, dict) and hedge.get("symbol"):
            seen.setdefault(str(hedge["symbol"]), None)
    return list(seen.keys())


@stbt_bp.route("/api/panic/<strategy_id>", methods=["POST"])
@check_session_validity
def panic_close(strategy_id):
    """PANIC: stop the strategy and force-flatten every position it holds.

    Works independently of the engine subprocess (which may be hung) — the
    blueprint places the closing orders directly via placesmartorder to
    position_size=0 for each held symbol. Idempotent: already-flat symbols are
    no-ops. This is the operator's emergency square-off + kill switch.
    """
    user_id = session.get("user")
    entry, error = _verify_stbt(strategy_id, user_id)
    if error:
        return error

    # 1. Stop the running process FIRST so the engine cannot re-enter while we
    #    flatten. Best-effort; a hung/dead process must not block the close.
    if entry.get("is_running"):
        try:
            stop_strategy_process(strategy_id)
        except Exception as exc:
            logger.warning(f"panic: stop process failed for {strategy_id}: {exc}")
    entry["manually_stopped"] = True  # prevent scheduler auto-restart
    entry["is_running"] = False
    save_configs()

    # 2. Resolve what to close and how.
    params = _read_json(_config_path(strategy_id)) or {}
    underlying = str(params.get("underlying", "SENSEX")).upper()
    opt_exchange = _OPT_EXCHANGE.get(underlying, "BFO")
    product = str(params.get("product", "NRML")).upper()
    strategy_kind = str(params.get("strategy_type", "stbt")).upper()
    strategy_tag = f"{underlying}_{strategy_kind}"
    symbols = _open_symbols(strategy_id)

    api_key = get_api_key_for_tradingview(user_id)
    if not api_key:
        return jsonify(
            {"status": "error", "message": "No API key for this user — cannot place close orders"}
        ), 400

    # 3. Flatten each symbol to zero (position-size reconcile). Idempotent.
    closed, already_flat, failed = [], [], []
    for symbol in symbols:
        order = {
            "strategy": strategy_tag,
            "symbol": symbol,
            "exchange": opt_exchange,
            "action": "BUY",  # real side derived from the position sign
            "quantity": 0,  # 0 makes the already-flat case a true no-op
            "position_size": 0,
            "product": product,
            "pricetype": "MARKET",
        }
        try:
            success, resp, _code = place_smart_order(order, api_key=api_key)
            msg = str((resp or {}).get("message", "")).lower()
            if success and ("matched" in msg or "no action" in msg):
                already_flat.append(symbol)
            elif success:
                closed.append(symbol)
            else:
                failed.append(symbol)
                logger.error(f"panic: flatten failed for {symbol}: {resp}")
        except Exception as exc:
            failed.append(symbol)
            logger.exception(f"panic: flatten exception for {symbol}: {exc}")

    # 4. Clear the persisted state only if nothing failed — a failed close means
    #    a position may remain, so keep state for the operator + a restart.
    if not failed:
        for p in (_state_path(strategy_id), _status_path(strategy_id)):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    status = "success" if not failed else "partial"
    logger.info(
        f"panic close {strategy_id}: closed={closed} already_flat={already_flat} failed={failed}"
    )
    return jsonify(
        {
            "status": status,
            "closed": closed,
            "already_flat": already_flat,
            "failed": failed,
            "message": (
                f"Closed {len(closed)}, {len(already_flat)} already flat"
                if not failed
                else f"{len(failed)} FAILED — check broker positions now"
            ),
        }
    )
