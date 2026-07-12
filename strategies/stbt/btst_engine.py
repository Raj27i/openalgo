#!/usr/bin/env python
# =============================================================================
#  BTST FLIP ENGINE  v1.0.0  — config-driven engine for the /stbt tab
#  (sibling of strategies/stbt/engine.py; deliberately self-contained so the
#   live-tested STBT short engine is never touched)
#
#  Strategy : BTST "Paper-Short Flip" — CE only, LONG overnight.
#             Backtested on Volrix (SENSEX weekly, 2023-12 → 2026-07):
#             run 53962bf9-c628-4a1e-9d5b-b1284039bf3d (filtered).
#
#  RULES
#  ─────
#  DAY-1  ref time (default 11:00, Mon–Thu only, DTE 1–3, not expiry day)
#    1. Fetch option chain → identify the ITM-{moneyness} CE and snapshot its
#       premium as REFERENCE.
#    2. On every closed 1-minute candle inside the entry window
#       (default 11:01–14:59):
#         • close ≤ ref × (1 − drop_pct%)      → open a PAPER short
#           (virtual entry = close, virtual SL = close × (1 + vsl_pct%))
#         • close ≥ virtual SL                 → the paper short is stopped
#           out (upward momentum) → REAL BUY of the CE, but only if
#           India VIX ≤ vix_max at that moment (signal stays armed and may
#           fire on a later candle once VIX cools). Max 1 entry per day.
#    3. Risk: real_sl_pct% stop-loss below the buy premium — live on ticks
#       Day-1, across the overnight gap, and Day-2 morning.
#    4. Breakeven arming: once the premium CLOSES ≥ entry × (1 + be_trigger%),
#       the breakeven stop is ARMED (any day). Day-1 pullbacks to entry are
#       tolerated; the stop only acts on Day 2+.
#  DAY-1  ws close (default 15:29) — disconnect WS; persist state if long.
#  DAY-2  open (default 09:16)     — reconcile with broker, resume SL watch.
#         If breakeven is armed and the premium closes ≤ entry → exit at
#         market (a Day-2 giveback is a dying trade).
#  DAY-2  force exit (default 10:30) — SELL the position at market; cleanup.
#  EXPIRY DAY — skip Day-1 (cannot hold overnight); Day-2 exit only.
#
#  Reuses the STBT tab plumbing unchanged: {id}_config/status/state/history/
#  journal JSON contract, panic close-all, analytics, Telegram alerts.
# =============================================================================

import json
import logging
import os
import queue
import signal
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openalgo import api

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  — loaded from strategies/stbt/{STRATEGY_ID}_config.json
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.1.0"

IST = ZoneInfo("Asia/Kolkata")

STBT_DIR = Path(__file__).resolve().parent

# App root on sys.path (see engine.py — needed for Telegram platform imports).
import sys  # noqa: E402

_APP_ROOT = str(STBT_DIR.parent.parent)
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

STRATEGY_ID = os.getenv("STRATEGY_ID", "").strip()
if not STRATEGY_ID:
    raise SystemExit(
        "[BTST] STRATEGY_ID env var missing — this engine must be launched "
        "through the OpenAlgo Strategy Manager (via the /stbt tab)."
    )

INDEX_MAP = {
    "SENSEX": ("BSE_INDEX", "BFO"),
    "BANKEX": ("BSE_INDEX", "BFO"),
    "NIFTY": ("NSE_INDEX", "NFO"),
    "BANKNIFTY": ("NSE_INDEX", "NFO"),
    "FINNIFTY": ("NSE_INDEX", "NFO"),
    "MIDCPNIFTY": ("NSE_INDEX", "NFO"),
}

_CONFIG_FILE = STBT_DIR / f"{STRATEGY_ID}_config.json"

_CONFIG_DEFAULTS = {
    "underlying": "SENSEX",
    "strategy_type": "btst",
    "product": "NRML",  # overnight — must NOT be MIS
    "default_lot_size": 20,
    "lot_multiplier": 1,
    "moneyness": 2,  # ITM-N CE strike (2 = two strikes in the money)
    "drop_pct": 5.0,  # paper-short trigger: % drop from reference
    "vsl_pct": 20.0,  # paper-short SL distance → real BUY trigger
    "real_sl_pct": 30.0,  # stop-loss % below the bought premium
    "be_trigger_pct": 30.0,  # arm breakeven at entry × (1 + this%); 0 = off
    "vix_max": 18.0,  # no entries while India VIX above this (0 = off)
    "candle_source": "WS",  # 1-min candle closes: WS ticks first | HISTORY API first
    "dte_min": 1,  # allowed days-to-expiry window for entries
    "dte_max": 3,
    "entry_weekdays": ["mon", "tue", "wed", "thu"],  # no Friday entries
    "max_loss": 0.0,  # session kill switch in ₹ (0 = disabled)
    "telegram_alerts": True,
    "use_smart_exit": True,  # exits via placesmartorder (idempotent flatten)
    "user_id": "",  # owner username (written by blueprints/stbt.py)
    "ref_time": "11:00",
    "entry_start_time": "11:01",
    "entry_end_time": "14:59",
    "ws_close_time": "15:29",
    "day2_open_time": "09:16",
    "force_exit_time": "10:30",
    # Charges / brokerage estimate (BSE option defaults).
    "brokerage_per_order": 20.0,
    "brokerage_pct": 0.0,
    "stt_option_sell_rate": 0.0015,
    "exchange_txn_rate": 0.0000325,
    "sebi_turnover_rate": 0.000001,
    "stamp_duty_option_buy_rate": 0.00003,
    "gst_rate": 0.18,
}


def _load_config() -> dict:
    if not _CONFIG_FILE.exists():
        raise SystemExit(f"[BTST] Config file not found: {_CONFIG_FILE}")
    with open(_CONFIG_FILE, encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    cfg = dict(_CONFIG_DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if v is not None})
    return cfg


def _hhmm(value: str) -> tuple[int, int]:
    h, m = str(value).strip().split(":")
    return int(h), int(m)


_CFG = _load_config()

# ── Connection (env only — injected by the Strategy Manager) ─────────────────
API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
HOST = (os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST") or "").strip()
WS_URL = os.getenv("WEBSOCKET_URL", "").strip() or None

# ── Instrument ───────────────────────────────────────────────────────────────
UNDERLYING = str(_CFG["underlying"]).upper()
if UNDERLYING not in INDEX_MAP:
    raise SystemExit(
        f"[BTST] Unsupported underlying {UNDERLYING!r} — supported: {sorted(INDEX_MAP)}"
    )
INDEX_EXCHANGE, OPT_EXCHANGE = INDEX_MAP[UNDERLYING]
STRATEGY = f"{UNDERLYING}_BTST"

# India VIX quote source for the entry filter (NSE index, any broker).
VIX_SYMBOL = "INDIAVIX"
VIX_EXCHANGE = "NSE_INDEX"

# ── Position sizing ──────────────────────────────────────────────────────────
PRODUCT = str(_CFG["product"]).upper()
DEFAULT_LOT_SIZE = int(_CFG["default_lot_size"])
LOT_MULTIPLIER = int(_CFG["lot_multiplier"])

# ── Strategy parameters ──────────────────────────────────────────────────────
MONEYNESS = int(_CFG["moneyness"])
DROP_PCT = float(_CFG["drop_pct"])
VSL_PCT = float(_CFG["vsl_pct"])
REAL_SL_PCT = float(_CFG["real_sl_pct"])
BE_TRIGGER_PCT = float(_CFG["be_trigger_pct"])
CANDLE_SOURCE = str(_CFG["candle_source"]).upper()  # WS | HISTORY
VIX_MAX = float(_CFG["vix_max"])
DTE_MIN = int(_CFG["dte_min"])
DTE_MAX = int(_CFG["dte_max"])
ENTRY_WEEKDAYS = {str(d).lower()[:3] for d in (_CFG["entry_weekdays"] or [])}
MAX_LOSS = float(_CFG["max_loss"])
TELEGRAM_ALERTS = bool(_CFG["telegram_alerts"])
USE_SMART_EXIT = bool(_CFG["use_smart_exit"])
OWNER_USER_ID = str(_CFG["user_id"] or "")

BROKERAGE_PER_ORDER = float(_CFG["brokerage_per_order"])
BROKERAGE_PCT = float(_CFG["brokerage_pct"])
STT_OPTION_SELL_RATE = float(_CFG["stt_option_sell_rate"])
EXCHANGE_TXN_RATE = float(_CFG["exchange_txn_rate"])
SEBI_TURNOVER_RATE = float(_CFG["sebi_turnover_rate"])
STAMP_DUTY_OPTION_BUY_RATE = float(_CFG["stamp_duty_option_buy_rate"])
GST_RATE = float(_CFG["gst_rate"])

# ── Timing  (24-hour, IST) ───────────────────────────────────────────────────
REF_H, REF_M = _hhmm(_CFG["ref_time"])  # reference snapshot
ENTRY_START_H, ENTRY_START_M = _hhmm(_CFG["entry_start_time"])
ENTRY_END_H, ENTRY_END_M = _hhmm(_CFG["entry_end_time"])  # inclusive candle
WS_CLOSE_H, WS_CLOSE_M = _hhmm(_CFG["ws_close_time"])
DAY2_H, DAY2_M = _hhmm(_CFG["day2_open_time"])
EXIT_H, EXIT_M = _hhmm(_CFG["force_exit_time"])

# ── Reliability (mirrors engine.py) ──────────────────────────────────────────
WS_STALE_SECS = 120
WS_MAX_RECONNECTS = 2
WS_RECONNECT_COOLDOWN_SECS = 300
REST_LTP_SECS = 10
VIX_QUOTE_SECS = 30  # India VIX REST quote throttle
ORDER_RETRIES = 3
ORDER_RETRY_DL = 2
API_RETRIES = 3
API_RETRY_DL = 3
CLOSE_MAX_RETRIES = 5
CLOSE_RETRY_DL = 3
STRATEGY_TIMEOUT_S = 25200  # 7-hour hard wall-clock limit for one run

# ── Runtime files (same contract as engine.py — the tab reads these) ─────────
STATE_FILE = str(STBT_DIR / f"{STRATEGY_ID}_state.json")
ORDER_INTENT_FILE = str(STBT_DIR / f"{STRATEGY_ID}_order_intent.json")
STATUS_FILE = str(STBT_DIR / f"{STRATEGY_ID}_status.json")
HISTORY_FILE = str(STBT_DIR / f"{STRATEGY_ID}_history.json")
JOURNAL_FILE = str(STBT_DIR / f"{STRATEGY_ID}_journal.json")
HISTORY_MAX_RECORDS = 400
JOURNAL_MAX_RECORDS = 2000

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("BTST")

# ─────────────────────────────────────────────────────────────────────────────
#  GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _on_signal(signum, _frame):
    log.warning("Signal %d received — initiating graceful shutdown.", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)

# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFICATIONS  (best-effort, never blocks or breaks trading)
# ─────────────────────────────────────────────────────────────────────────────

_tg_chat_id: int | None = None
_tg_resolved = False


def _resolve_chat_id() -> int | None:
    global _tg_chat_id, _tg_resolved
    if _tg_resolved:
        return _tg_chat_id
    _tg_resolved = True
    if not OWNER_USER_ID:
        log.debug("[NOTIFY] No user_id in config — Telegram alerts unavailable.")
        return None
    try:
        from database.telegram_db import get_telegram_user_by_username

        user = get_telegram_user_by_username(OWNER_USER_ID)
        if user and user.get("telegram_id"):
            _tg_chat_id = int(user["telegram_id"])
        else:
            log.info("[NOTIFY] No linked Telegram account for %s — alerts off.", OWNER_USER_ID)
    except Exception as exc:
        log.warning("[NOTIFY] Telegram chat lookup failed: %s", exc)
    return _tg_chat_id


def _notify(text: str):
    """Fire-and-forget strategy alert to the owner's Telegram (if linked)."""
    if not TELEGRAM_ALERTS:
        return
    chat_id = _resolve_chat_id()
    if not chat_id:
        return

    def _send():
        try:
            from services.telegram_alert_service import telegram_alert_service

            telegram_alert_service.send_alert_sync(chat_id, f"[{UNDERLYING} BTST] {text}")
        except Exception as exc:
            log.warning("[NOTIFY] Telegram send failed: %s", exc)

    threading.Thread(target=_send, daemon=True, name="btst-notify").start()


# ─────────────────────────────────────────────────────────────────────────────
#  OPENALGO CLIENT
# ─────────────────────────────────────────────────────────────────────────────

_api_kwargs = {"api_key": API_KEY, "host": HOST, "verbose": False}
if WS_URL:
    _api_kwargs["ws_url"] = WS_URL
client = api(**_api_kwargs)

# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET  — thread-safe LTP cache + single tick-arrival queue
# ─────────────────────────────────────────────────────────────────────────────

_ltp_cache: dict[str, float] = {}
_ltp_lock = threading.Lock()
_tick_q: queue.Queue = queue.Queue()
_last_tick_ts = 0.0
_ws_reconnects = 0
_last_ws_reconnect_ts = 0.0
_ws_degraded = False
_rest_ltp_cache: dict[str, tuple[float, float]] = {}

_ws_first_tick_logged = False


def _on_quote(data: dict):
    """WebSocket callback — the only writer to _ltp_cache."""
    global _last_tick_ts, _ws_first_tick_logged

    with _ltp_lock:
        _last_tick_ts = time.time()

    if not _ws_first_tick_logged:
        _ws_first_tick_logged = True
        log.info(
            "[WS] First tick payload keys=%s  sample=%s",
            list(data.keys()) if isinstance(data, dict) else type(data),
            str(data)[:200],
        )

    if not isinstance(data, dict):
        return

    sym = data.get("symbol", "")
    payload = data.get("data", data) if isinstance(data.get("data"), dict) else data
    ltp = payload.get("ltp") or payload.get("last_price") or payload.get("lp") or 0.0

    if sym and ltp:
        try:
            ltp_f = float(ltp)
            with _ltp_lock:
                _ltp_cache[sym] = ltp_f
            _update_minute_candle(sym, ltp_f)
            _tick_q.put_nowait(True)
        except (ValueError, TypeError, queue.Full):
            pass


def _drain_tick_queue():
    while True:
        try:
            _tick_q.get_nowait()
        except queue.Empty:
            break


def _wait_for_tick(timeout: float = 1.0):
    try:
        _tick_q.get(timeout=timeout)
    except queue.Empty:
        pass
    _drain_tick_queue()


def ws_ltp(symbol: str) -> float:
    with _ltp_lock:
        return _ltp_cache.get(symbol, 0.0)


def _extract_ltp(resp: dict) -> float:
    if not isinstance(resp, dict):
        return 0.0
    data = resp.get("data", resp)
    if isinstance(data, dict):
        ltp = data.get("ltp") or data.get("last_price") or data.get("lp")
        try:
            return float(ltp or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def rest_ltp(symbol: str) -> float:
    """Throttled REST quote fallback used when WS stalls."""
    now = time.time()
    cached = _rest_ltp_cache.get(symbol)
    if cached and now - cached[1] < REST_LTP_SECS:
        return cached[0]

    try:
        resp = client.quotes(symbol=symbol, exchange=OPT_EXCHANGE)
        ltp = _extract_ltp(resp)
        if ltp > 0:
            _rest_ltp_cache[symbol] = (ltp, now)
            with _ltp_lock:
                _ltp_cache[symbol] = ltp
            _update_minute_candle(symbol, ltp)
            return ltp
        log.warning("[REST-LTP] No LTP for %s: %s", symbol, resp)
    except Exception as exc:
        log.warning("[REST-LTP] Quote failed for %s: %s", symbol, exc)
    return 0.0


def _ws_is_stale() -> bool:
    with _ltp_lock:
        return time.time() - _last_tick_ts > WS_STALE_SECS


def live_ltp(symbol: str) -> float:
    cached = ws_ltp(symbol)
    if cached > 0 and not (_ws_degraded or _ws_is_stale()):
        return cached
    return rest_ltp(symbol) or cached


def ws_subscribe(symbols: list[str]):
    global _last_tick_ts, _ws_first_tick_logged
    instruments = [{"exchange": OPT_EXCHANGE, "symbol": s} for s in symbols]
    _ws_first_tick_logged = False
    client.connect()
    client.subscribe_ltp(instruments, on_data_received=_on_quote)
    with _ltp_lock:
        _last_tick_ts = time.time()
    log.info("[WS] Subscribed (LTP mode): %s", symbols)


def ws_unsubscribe(symbols: list[str]):
    instruments = [{"exchange": OPT_EXCHANGE, "symbol": s} for s in symbols]
    try:
        client.unsubscribe_ltp(instruments)
        client.disconnect()
    except Exception as exc:
        log.warning("[WS] Disconnect warning (ignored): %s", exc)
    log.info("[WS] Disconnected.")


def ws_reconnect_if_stale(symbols: list[str]):
    global _ws_reconnects, _last_ws_reconnect_ts, _ws_degraded

    if not _ws_is_stale():
        return

    if _ws_reconnects >= WS_MAX_RECONNECTS:
        if not _ws_degraded:
            log.error(
                "[WS] Feed silent after %d reconnects — switching to REST LTP fallback.",
                WS_MAX_RECONNECTS,
            )
        _ws_degraded = True
        for symbol in symbols:
            rest_ltp(symbol)
        return

    now = time.time()
    if now - _last_ws_reconnect_ts < WS_RECONNECT_COOLDOWN_SECS:
        for symbol in symbols:
            rest_ltp(symbol)
        return

    _ws_reconnects += 1
    _last_ws_reconnect_ts = now
    log.warning(
        "[WS] Feed silent >%ds — reconnecting (%d/%d).",
        WS_STALE_SECS,
        _ws_reconnects,
        WS_MAX_RECONNECTS,
    )
    ws_unsubscribe(symbols)
    time.sleep(5)
    ws_subscribe(symbols)


# ─────────────────────────────────────────────────────────────────────────────
#  INDIA VIX FILTER
# ─────────────────────────────────────────────────────────────────────────────

_vix_cache: tuple[float, float] | None = None  # (value, fetched_epoch)


def current_vix() -> float:
    """Throttled India VIX quote. Returns 0.0 when unavailable."""
    global _vix_cache
    now = time.time()
    if _vix_cache and now - _vix_cache[1] < VIX_QUOTE_SECS:
        return _vix_cache[0]
    try:
        resp = client.quotes(symbol=VIX_SYMBOL, exchange=VIX_EXCHANGE)
        vix = _extract_ltp(resp)
        if vix > 0:
            _vix_cache = (vix, now)
            return vix
        log.warning("[VIX] No LTP in quote response: %s", resp)
    except Exception as exc:
        log.warning("[VIX] Quote failed: %s", exc)
    return 0.0


def vix_ok() -> bool:
    """Entry gate: India VIX must be at/below VIX_MAX.

    Mirrors the backtest: if VIX data is unavailable, do NOT block the entry
    (the signal fires on price action; VIX is a soft filter)."""
    if VIX_MAX <= 0:
        return True
    vix = current_vix()
    if vix <= 0:
        return True
    if vix <= VIX_MAX:
        return True
    log.info("[VIX] %.2f above max %.2f — entry blocked (signal stays armed).", vix, VIX_MAX)
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  RETRY WRAPPERS
# ─────────────────────────────────────────────────────────────────────────────


def _api_call(fn, label: str = "api") -> dict:
    last_exc = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = fn()
            if isinstance(resp, dict) and resp.get("status") == "success":
                return resp
            log.warning("[%s] Attempt %d — unexpected response: %s", label, attempt, resp)
        except Exception as exc:
            last_exc = exc
            log.warning("[%s] Attempt %d — exception: %s", label, attempt, exc)
        if attempt < API_RETRIES:
            time.sleep(API_RETRY_DL)
    raise RuntimeError(f"'{label}' failed after {API_RETRIES} attempts. Last: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER-INTENT GUARD  (crash-safe duplicate-order protection)
# ─────────────────────────────────────────────────────────────────────────────


def _intent_now() -> str:
    return _now_ist().isoformat(timespec="seconds")


def _save_order_intent(intent: dict):
    tmp = ORDER_INTENT_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(intent, fh, indent=2)
    os.replace(tmp, ORDER_INTENT_FILE)


def _load_order_intent() -> dict | None:
    if not os.path.exists(ORDER_INTENT_FILE):
        return None
    try:
        with open(ORDER_INTENT_FILE) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("[ORDER-GUARD] Could not read pending intent: %s", exc)
        return None


def _clear_order_intent():
    for path in (ORDER_INTENT_FILE, ORDER_INTENT_FILE + ".tmp"):
        if os.path.exists(path):
            os.remove(path)


def _parse_broker_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt).time()
            return datetime.combine(_now_ist().date(), t, tzinfo=IST)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    except ValueError:
        return None


def _response_items(resp: dict, book_type: str) -> list:
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return []
    data = resp.get("data", [])
    if book_type == "orderbook" and isinstance(data, dict):
        return data.get("orders", []) or []
    return data if isinstance(data, list) else []


def _same_order_shape(row: dict, symbol: str, action: str, quantity: int) -> bool:
    if str(row.get("symbol", "")).upper() != symbol.upper():
        return False
    if str(row.get("action", "")).upper() != action.upper():
        return False
    if str(row.get("exchange", OPT_EXCHANGE)).upper() != OPT_EXCHANGE.upper():
        return False
    if str(row.get("product", PRODUCT)).upper() != PRODUCT.upper():
        return False
    try:
        row_qty = int(float(row.get("quantity", 0)))
    except (TypeError, ValueError):
        return False
    return row_qty == int(quantity)


def _is_recent_broker_row(row: dict, since_iso: str, grace_seconds: int = 3) -> bool:
    row_ts = _parse_broker_ts(row.get("timestamp") or row.get("order_timestamp"))
    if not row_ts:
        return False
    since = datetime.fromisoformat(since_iso)
    if not since.tzinfo:
        since = since.replace(tzinfo=IST)
    return row_ts >= since - timedelta(seconds=grace_seconds)


def _find_recent_matching_order(intent: dict) -> str | None:
    symbol = intent["symbol"]
    action = intent["action"]
    quantity = int(intent["quantity"])
    since_iso = intent["created_at"]

    try:
        resp = client.orderbook()
        for row in _response_items(resp, "orderbook"):
            status = str(row.get("order_status", "")).lower()
            if status in {"rejected", "cancelled", "canceled"}:
                continue
            if _same_order_shape(row, symbol, action, quantity) and _is_recent_broker_row(
                row, since_iso
            ):
                oid = row.get("orderid")
                if oid:
                    log.warning("[ORDER-GUARD] Matched pending intent in orderbook: %s", oid)
                    return str(oid)
    except Exception as exc:
        log.warning("[ORDER-GUARD] orderbook reconciliation failed: %s", exc)

    try:
        resp = client.tradebook()
        for row in _response_items(resp, "tradebook"):
            if _same_order_shape(row, symbol, action, quantity) and _is_recent_broker_row(
                row, since_iso
            ):
                oid = row.get("orderid")
                if oid:
                    log.warning("[ORDER-GUARD] Matched pending intent in tradebook: %s", oid)
                    return str(oid)
    except Exception as exc:
        log.warning("[ORDER-GUARD] tradebook reconciliation failed: %s", exc)

    return None


def _reconcile_pending_intent(intent: dict, wait_seconds: int = 8) -> str | None:
    deadline = time.time() + wait_seconds
    while time.time() <= deadline and not _shutdown.is_set():
        oid = _find_recent_matching_order(intent)
        if oid:
            return oid
        time.sleep(1)
    return None


def _place_order(symbol: str, action: str, quantity: int, reason: str = "ORDER") -> str | None:
    """Place a MARKET NRML order with retries guarded by broker reconciliation."""
    intent = {
        "symbol": symbol,
        "action": action,
        "quantity": int(quantity),
        "reason": reason,
        "status": "PENDING",
        "created_at": _intent_now(),
    }

    pending = _load_order_intent()
    if pending and all(
        pending.get(k) == intent[k] for k in ("symbol", "action", "quantity", "reason")
    ):
        log.warning("[ORDER-GUARD] Existing pending intent found; reconciling before new order.")
        oid = _reconcile_pending_intent(pending, wait_seconds=5)
        if oid:
            _clear_order_intent()
            return oid

    for attempt in range(1, ORDER_RETRIES + 1):
        _save_order_intent(intent)
        try:
            resp = client.placeorder(
                strategy=STRATEGY,
                symbol=symbol,
                action=action,
                exchange=OPT_EXCHANGE,
                price_type="MARKET",
                product=PRODUCT,
                quantity=quantity,
            )
            if resp.get("status") == "success":
                oid = resp["orderid"]
                log.info(
                    "[ORDER] %s %s qty=%d id=%s (attempt %d)",
                    action,
                    symbol,
                    quantity,
                    oid,
                    attempt,
                )
                _clear_order_intent()
                return oid
            log.warning("[ORDER] %s %s attempt %d failed: %s", action, symbol, attempt, resp)
        except Exception as exc:
            log.warning("[ORDER] %s %s attempt %d exception: %s", action, symbol, attempt, exc)
        oid = _reconcile_pending_intent(intent)
        if oid:
            _clear_order_intent()
            return oid
        if attempt < ORDER_RETRIES:
            time.sleep(ORDER_RETRY_DL)
    log.error("[ORDER] %s %s FAILED after %d attempts.", action, symbol, ORDER_RETRIES)
    return None


def _place_critical_order(symbol: str, action: str, quantity: int, label: str) -> str | None:
    oid = _place_order(symbol, action, quantity, reason=label)
    if oid:
        return oid
    for attempt in range(1, CLOSE_MAX_RETRIES + 1):
        log.warning(
            "[%s] Critical retry %d/%d for %s %s", label, attempt, CLOSE_MAX_RETRIES, action, symbol
        )
        time.sleep(CLOSE_RETRY_DL)
        oid = _place_order(symbol, action, quantity, reason=label)
        if oid:
            return oid
    log.critical(
        "[%s] *** %s %s FAILED AFTER ALL RETRIES — MANUAL ACTION REQUIRED: %s qty=%d ***",
        label,
        action,
        symbol,
        symbol,
        quantity,
    )
    _notify(
        f"CRITICAL [{label}]: {action} {symbol} qty={quantity} FAILED after all "
        f"retries — MANUAL ACTION REQUIRED at the broker."
    )
    return None


def _smart_flatten(symbol: str, label: str, critical: bool = False) -> tuple[str | None, str]:
    """Reconcile a position to FLAT via placesmartorder(position_size=0).

    Same invariants as engine.py: quantity=0 AND position_size=0 so the
    already-flat case is a true no-op (the broker's degenerate 0/0 branch can
    never open a fresh position). For the long→0 case the broker derives
    SELL abs(current) itself. Returns (orderid, "placed"|"flat"|"failed")."""
    retries = CLOSE_MAX_RETRIES if critical else ORDER_RETRIES
    delay = CLOSE_RETRY_DL if critical else ORDER_RETRY_DL
    for attempt in range(1, retries + 1):
        try:
            resp = client.placesmartorder(
                strategy=STRATEGY,
                symbol=symbol,
                action="SELL",  # required field; real side derived from position sign
                exchange=OPT_EXCHANGE,
                price_type="MARKET",
                product=PRODUCT,
                quantity=0,
                position_size=0,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                msg = str(resp.get("message", "")).lower()
                if "matched" in msg or "no action" in msg:
                    log.warning("[FLATTEN %s] Already flat (%s) — no order placed.", label, symbol)
                    return None, "flat"
                oid = resp.get("orderid")
                if oid:
                    log.info(
                        "[FLATTEN %s] Reconciled %s to flat  id=%s (attempt %d)",
                        label,
                        symbol,
                        oid,
                        attempt,
                    )
                    return str(oid), "placed"
                return None, "placed"
            log.warning(
                "[FLATTEN %s] %s attempt %d unexpected response: %s", label, symbol, attempt, resp
            )
        except Exception as exc:
            log.warning("[FLATTEN %s] %s attempt %d exception: %s", label, symbol, attempt, exc)
        if attempt < retries:
            if _shutdown.wait(delay):
                break
    log.critical(
        "[FLATTEN %s] *** %s could NOT be flattened after %d attempts — MANUAL ACTION REQUIRED ***",
        label,
        symbol,
        retries,
    )
    _notify(
        f"CRITICAL [{label}]: could not flatten {symbol} after {retries} attempts "
        f"— check broker positions NOW (manual action required)."
    )
    return None, "failed"


def _get_fill_price(order_id: str, symbol: str, fallback_ltp: float) -> float:
    try:
        time.sleep(0.5)
        resp = client.tradebook()
        if resp.get("status") == "success":
            for trade in resp.get("data", []):
                if str(trade.get("orderid")) == str(order_id):
                    avg = trade.get("average_price") or trade.get("avgprice")
                    if avg and float(avg) > 0:
                        log.info("[FILL] Actual fill for %s: %.2f", symbol, float(avg))
                        return float(avg)
    except Exception as exc:
        log.warning("[FILL] Could not fetch fill price: %s — using fallback", exc)
    return live_ltp(symbol) or fallback_ltp


# ─────────────────────────────────────────────────────────────────────────────
#  OPTION CHAIN + EXPIRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def get_chain(expiry: str) -> dict:
    return _api_call(
        lambda: client.optionchain(
            underlying=UNDERLYING,
            exchange=INDEX_EXCHANGE,
            expiry_date=expiry,
        ),
        label="optionchain",
    )


def get_lot_size(chain: dict) -> int:
    lot = chain.get("lot_size")
    if lot and int(lot) > 0:
        log.info("[LOT] Lot size from chain: %d", int(lot))
        return int(lot)
    for row in chain.get("chain", []):
        for side in ("ce", "pe"):
            entry = row.get(side, {})
            lot = entry.get("lot_size") or entry.get("lotsize")
            if lot and int(lot) > 0:
                log.info("[LOT] Lot size from chain row: %d", int(lot))
                return int(lot)
    log.warning("[LOT] Could not determine lot size — using default %d", DEFAULT_LOT_SIZE)
    return DEFAULT_LOT_SIZE


def find_itm_ce(chain: dict) -> tuple[str, float]:
    """Return (symbol, ltp) for the ITM-{MONEYNESS} CE strike."""
    label = f"ITM{MONEYNESS}"
    for row in chain.get("chain", []):
        ce = row.get("ce")
        if ce and ce.get("label") == label:
            return ce["symbol"], float(ce["ltp"])
    raise RuntimeError(f"{label} CE not found in option chain response.")


def _expiry_list() -> list[str]:
    resp = _api_call(
        lambda: client.expiry(
            symbol=UNDERLYING,
            exchange=OPT_EXCHANGE,
            instrumenttype="options",
        ),
        label="expiry",
    )
    return resp.get("data", [])


def _to_symbol_fmt(api_date: str) -> str:
    return api_date.replace("-", "")


def _parse_api_expiry(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip().upper(), "%d-%b-%y").date()
    except (ValueError, AttributeError):
        return None


def get_next_expiry_with_dte() -> tuple[str, int]:
    """Nearest strictly-future expiry as (symbol_fmt, days_to_expiry).

    Same future-only rule as engine.py: the broker often keeps the most
    recently expired date in the list until EOD."""
    today = _now_ist().date()
    future = []
    for exp in _expiry_list():
        exp_date = _parse_api_expiry(exp)
        if exp_date and exp_date > today:
            future.append((exp_date, exp))
    if not future:
        raise RuntimeError("No future expiry found in API response.")
    future.sort(key=lambda x: x[0])
    chosen_date, chosen_str = future[0]
    dte = (chosen_date - today).days
    log.info("[EXPIRY] Chosen next expiry: %s (DTE=%d)", chosen_str, dte)
    return _to_symbol_fmt(chosen_str.upper()), dte


def is_expiry_day() -> bool:
    today = _now_ist().date()
    try:
        for exp in _expiry_list():
            exp_date = _parse_api_expiry(exp)
            if exp_date and exp_date == today:
                return True
        return False
    except Exception as exc:
        log.warning("[EXPIRY] Check failed: %s — assuming NOT expiry day.", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  TIMING UTILITIES
# ─────────────────────────────────────────────────────────────────────────────


def _now_ist() -> datetime:
    return datetime.now(IST)


def wait_until(h: int, m: int, label: str = ""):
    now = _now_ist()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    secs = (target - now).total_seconds()
    if secs > 0:
        log.info("[TIME] Waiting %.0fs until %02d:%02d  %s", secs, h, m, label)
        _shutdown.wait(timeout=secs)


def now_past(h: int, m: int) -> bool:
    n = _now_ist()
    return n.hour > h or (n.hour == h and n.minute >= m)


def _weekday_key(dt: datetime | None = None) -> str:
    return (dt or _now_ist()).strftime("%a").lower()[:3]  # 'mon'..'sun'


# ─────────────────────────────────────────────────────────────────────────────
#  1-MINUTE CANDLE CLOSE TRACKING  (signals run on candle closes, SL on ticks)
# ─────────────────────────────────────────────────────────────────────────────

_minute_candles: dict[str, dict] = {}
_closed_candles: dict[str, tuple[str, float]] = {}
_candle_lock = threading.Lock()


def _minute_key(dt: datetime | None = None) -> str:
    dt = dt or _now_ist()
    return dt.replace(second=0, microsecond=0).isoformat()


def _update_minute_candle(symbol: str, ltp: float):
    if not symbol or ltp <= 0:
        return

    minute = _minute_key()
    with _candle_lock:
        cur = _minute_candles.get(symbol)
        if not cur:
            _minute_candles[symbol] = {"minute": minute, "close": float(ltp)}
            return

        if cur["minute"] == minute:
            cur["close"] = float(ltp)
            return

        _closed_candles[symbol] = (cur["minute"], float(cur["close"]))
        _minute_candles[symbol] = {"minute": minute, "close": float(ltp)}


def _get_last_closed_candle(symbol: str) -> tuple[str, float] | None:
    with _candle_lock:
        return _closed_candles.get(symbol)


# True 1-min candle close via the history API (Volrix backtest fidelity: the
# drop/flip triggers evaluate on the official 1-min close, not on whatever the
# last websocket tick happened to be — BFO options tick slowly).

_hist_candle_cache: dict[str, tuple[str, str, float]] = {}
_hist_candle_lock = threading.Lock()


def _parse_candle_ts(value) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        try:
            dt = value.to_pydatetime()
            return dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except Exception:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    try:
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        if num > 1e8:
            return datetime.fromtimestamp(num, tz=IST)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _last_closed_row(df) -> tuple[str, float] | None:
    if df is None or getattr(df, "empty", True):
        return None
    cur_minute = _now_ist().replace(second=0, microsecond=0)
    try:
        if "timestamp" in df.columns:
            rows = zip(df["timestamp"].tolist(), df["close"].tolist())
        else:
            rows = zip(df.index.tolist(), df["close"].tolist())
    except Exception as exc:
        log.warning("[CANDLE-1M] Could not read history rows: %s", exc)
        return None

    best: tuple[datetime, float] | None = None
    for ts, close in rows:
        dt = _parse_candle_ts(ts)
        if dt is None:
            continue
        dt_min = dt.astimezone(IST).replace(second=0, microsecond=0)
        if dt_min >= cur_minute:
            continue
        if best is None or dt_min > best[0]:
            try:
                best = (dt_min, float(close))
            except (TypeError, ValueError):
                continue
    if best is None:
        return None
    return best[0].isoformat(), best[1]


def get_last_closed_1m_candle(symbol: str) -> tuple[str, float] | None:
    cur_min = _minute_key()
    with _hist_candle_lock:
        c = _hist_candle_cache.get(symbol)
        if c and c[0] == cur_min:
            return (c[1], c[2]) if c[1] else None

    today = _now_ist().date().isoformat()
    try:
        df = client.history(
            symbol=symbol,
            exchange=OPT_EXCHANGE,
            interval="1m",
            start_date=today,
            end_date=today,
        )
        result = _last_closed_row(df)
    except Exception as exc:
        log.warning(
            "[CANDLE-1M] history() failed for %s: %s — falling back to WS candle.", symbol, exc
        )
        result = None

    with _hist_candle_lock:
        _hist_candle_cache[symbol] = (
            cur_min,
            result[0] if result else "",
            result[1] if result else 0.0,
        )
    return result


def latest_closed_candle(symbol: str) -> tuple[str, float] | None:
    """Latest closed 1-min candle for the entry/breakeven signals.

    candle_source='WS' (default): the candle is built from live WebSocket
    ticks (`_update_minute_candle` — same feed the SL runs on); the history
    API is only the fallback for minutes where the WS produced no closed
    candle yet (slow-ticking BFO strikes right after subscribe, stale feed).
    candle_source='HISTORY': the broker's official 1-min close is preferred
    and WS is the fallback (StockMock-style, like the STBT re-entry)."""
    if CANDLE_SOURCE == "WS":
        closed = _get_last_closed_candle(symbol)
        if closed is None:
            closed = get_last_closed_1m_candle(symbol)
        return closed
    closed = get_last_closed_1m_candle(symbol)
    if closed is None:
        closed = _get_last_closed_candle(symbol)
    return closed


# ─────────────────────────────────────────────────────────────────────────────
#  CHARGES
# ─────────────────────────────────────────────────────────────────────────────


def _brokerage_for_order(turnover: float) -> float:
    if BROKERAGE_PCT > 0:
        return min(BROKERAGE_PER_ORDER, turnover * BROKERAGE_PCT)
    return BROKERAGE_PER_ORDER


def _option_charges(buy_price: float, sell_price: float, quantity: int) -> dict:
    """Estimate brokerage, exchange, and statutory charges for one option cycle."""
    buy_turnover = max(buy_price, 0.0) * quantity
    sell_turnover = max(sell_price, 0.0) * quantity
    turnover = buy_turnover + sell_turnover

    brokerage = _brokerage_for_order(buy_turnover) + _brokerage_for_order(sell_turnover)
    stt = sell_turnover * STT_OPTION_SELL_RATE
    exchange_txn = turnover * EXCHANGE_TXN_RATE
    sebi = turnover * SEBI_TURNOVER_RATE
    stamp = buy_turnover * STAMP_DUTY_OPTION_BUY_RATE
    gst = (brokerage + exchange_txn) * GST_RATE
    total = brokerage + stt + exchange_txn + sebi + stamp + gst

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "sebi": sebi,
        "stamp": stamp,
        "gst": gst,
        "total": total,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  STATUS / STATE / HISTORY / JOURNAL  (same file contract as engine.py)
# ─────────────────────────────────────────────────────────────────────────────

_PHASE = "STARTING"
_last_status_write = 0.0
STATUS_REFRESH_SECS = 5


def _leg_mtm(leg, ltp: float) -> float:
    """Unrealized P&L of the open LONG leg at the given LTP (0 if unknown)."""
    if ltp <= 0 or leg.entry_price <= 0:
        return 0.0
    return (ltp - leg.entry_price) * leg.quantity


def _set_phase(phase: str, legs: list | None = None, expiry: str = "", message: str = ""):
    global _PHASE
    _PHASE = phase
    _write_status(legs or [], expiry, message)


def _write_status(legs: list, expiry: str, message: str = ""):
    """Atomic status snapshot for the /stbt tab. Same shape as engine.py's
    (hedge always null; leg dicts carry the extra virtual-short fields)."""
    global _last_status_write
    try:
        gross = sum(leg.realized_pnl for leg in legs)
        charges = sum(leg.charges_total for leg in legs)
        mtm = 0.0

        leg_dicts = []
        for leg in legs:
            d = leg.to_dict()
            ltp = ws_ltp(leg.symbol)
            d["ltp"] = round(ltp, 2)
            d["mtm_pnl"] = 0.0
            if leg.state == leg.IN_LONG:
                d["mtm_pnl"] = round(_leg_mtm(leg, ltp), 2)
                mtm += d["mtm_pnl"]
            leg_dicts.append(d)

        net = gross - charges
        payload = {
            "version": VERSION,
            "strategy_id": STRATEGY_ID,
            "strategy_type": "btst",
            "underlying": UNDERLYING,
            "phase": _PHASE,
            "message": message,
            "trade_date": _now_ist().date().isoformat(),
            "expiry": expiry,
            "quantity": legs[0].quantity if legs else 0,
            "main_legs": leg_dicts,
            "hedge": None,
            "gross_pnl": round(gross, 2),
            "charges": round(charges, 2),
            "net_pnl": round(net, 2),
            "mtm_pnl": round(mtm, 2),
            "total_net_pnl": round(net + mtm, 2),
            "max_loss": MAX_LOSS,
            "last_update": _now_ist().isoformat(timespec="seconds"),
        }
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, STATUS_FILE)
        _last_status_write = time.time()
    except Exception as exc:
        log.warning("[STATUS] Could not write status file: %s", exc)


def _maybe_refresh_status(legs: list, expiry: str):
    if time.time() - _last_status_write >= STATUS_REFRESH_SECS:
        _write_status(legs, expiry)


def _total_net(legs: list) -> float:
    """Session net P&L including open-position MTM (live_ltp — kill switch)."""
    gross = sum(leg.realized_pnl for leg in legs)
    charges = sum(leg.charges_total for leg in legs)
    for leg in legs:
        if leg.state == leg.IN_LONG:
            gross += _leg_mtm(leg, live_ltp(leg.symbol))
    return gross - charges


def _append_history(final_phase: str, legs: list, expiry: str):
    if not any(leg.entry_price > 0 for leg in legs):
        log.info("[HISTORY] No trades this session — skipping history record.")
        return
    try:
        records = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, encoding="utf-8") as fh:
                    records = json.load(fh)
                if not isinstance(records, list):
                    records = []
            except (json.JSONDecodeError, OSError):
                records = []

        gross = sum(leg.realized_pnl for leg in legs)
        charges = sum(leg.charges_total for leg in legs)
        leg_recs = [
            {
                "symbol": leg.symbol,
                "opt_type": leg.opt_type,
                "state": leg.state,
                "cycles": 1 if leg.entry_price > 0 else 0,
                "realized_pnl": round(leg.realized_pnl, 2),
                "charges": round(leg.charges_total, 2),
            }
            for leg in legs
        ]

        records.append(
            {
                "trade_date": _now_ist().date().isoformat(),
                "ended_at": _now_ist().isoformat(timespec="seconds"),
                "final_phase": final_phase,
                "expiry": expiry,
                "quantity": legs[0].quantity if legs else 0,
                "legs": leg_recs,
                "hedge": None,
                "gross_pnl": round(gross, 2),
                "charges": round(charges, 2),
                "net_pnl": round(gross - charges, 2),
            }
        )
        records = records[-HISTORY_MAX_RECORDS:]
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        os.replace(tmp, HISTORY_FILE)
        log.info("[HISTORY] Session record appended (%s, net=%.2f)", final_phase, gross - charges)
    except Exception as exc:
        log.warning("[HISTORY] Could not append session record: %s", exc)


def _append_journal(record: dict):
    try:
        rows = []
        if os.path.exists(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, encoding="utf-8") as fh:
                    rows = json.load(fh)
                if not isinstance(rows, list):
                    rows = []
            except (json.JSONDecodeError, OSError):
                rows = []
        rows.append(record)
        rows = rows[-JOURNAL_MAX_RECORDS:]
        tmp = JOURNAL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        os.replace(tmp, JOURNAL_FILE)
    except Exception as exc:
        log.warning("[JOURNAL] Could not append cycle record: %s", exc)


def _journal_cycle(
    leg_label: str,
    reason: str,
    entry: float,
    exit_price: float,
    qty: int,
    gross: float,
    charges: dict,
):
    _append_journal(
        {
            "closed_at": _now_ist().isoformat(timespec="seconds"),
            "trade_date": _now_ist().date().isoformat(),
            "config_id": STRATEGY_ID,
            "underlying": UNDERLYING,
            "leg": leg_label,
            "cycle": 1,
            "reason": reason,
            "entry": round(entry, 2),
            "exit": round(exit_price, 2),
            "qty": qty,
            "gross": round(gross, 2),
            "charges": round(charges.get("total", 0.0), 2),
            "charge_breakdown": {
                k: round(charges.get(k, 0.0), 2)
                for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst")
            },
            "net": round(gross - charges.get("total", 0.0), 2),
        }
    )


def save_state(legs: list, expiry: str):
    payload = {
        "version": VERSION,
        "strategy_type": "btst",
        "trade_date": _now_ist().date().isoformat(),
        "expiry": expiry,
        "main_legs": [l.to_dict() for l in legs],
        "hedge": None,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, STATE_FILE)
    _write_status(legs, expiry)


def load_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("[STATE] File unreadable (%s) — ignoring.", exc)
        return None


def delete_state():
    for path in (STATE_FILE, STATE_FILE + ".tmp"):
        if os.path.exists(path):
            os.remove(path)
    log.info("[STATE] State file removed.")


# ─────────────────────────────────────────────────────────────────────────────
#  FLIP LEG  — state machine for the single ITM CE long
# ─────────────────────────────────────────────────────────────────────────────


class FlipLeg:
    """
    WATCHING    ──(close ≤ ref × (1 − drop%))────────► PAPER_SHORT
    PAPER_SHORT ──(close ≥ v_sl AND VIX ok)──────────► IN_LONG   (real BUY)
    IN_LONG     ──(LTP ≤ entry × (1 − real_sl%))─────► DONE      (SL sell)
    IN_LONG     ──(BE armed, Day-2 close ≤ entry)────► DONE      (BE sell)
    IN_LONG     ──(Day-2 force exit)─────────────────► DONE
    WATCHING / PAPER_SHORT ──(entry window ends)─────► DONE      (no trade)

    Signals evaluate on 1-minute candle CLOSES (backtest fidelity); the real
    stop-loss runs on live ticks (≈ the backtest's bar high/low basis). The
    breakeven stop arms on a close ≥ entry × (1 + be_trigger%) on ANY day but
    only exits on Day 2+ — Day-1 pullbacks to entry are tolerated.
    Max one entry per day — no re-entry after the SL.
    """

    WATCHING = "WATCHING"
    PAPER_SHORT = "PAPER_SHORT"
    IN_LONG = "IN_LONG"
    DONE = "DONE"

    def __init__(self, symbol: str, opt_type: str = "CE", quantity: int = 0):
        self.symbol = symbol
        self.opt_type = opt_type
        self.quantity = quantity
        self.state = self.WATCHING
        self.ref_premium = 0.0
        self.v_entry = 0.0  # paper-short virtual entry
        self.v_sl = 0.0  # paper-short virtual SL = real-BUY trigger
        self.entry_price = 0.0  # real BUY fill
        self.sl_price = 0.0  # real SL = entry × (1 − REAL_SL_PCT/100)
        self.entry_date = ""  # ISO date of the real BUY (drives Day-2 logic)
        self.be_armed = False  # breakeven stop armed (survives overnight)
        self.last_signal_candle = ""  # dedupe candle-close evaluations
        self.realized_pnl = 0.0
        self.charges_total = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "FlipLeg":
        leg = cls(d["symbol"], d.get("opt_type", "CE"), d.get("quantity", 0))
        leg.state = d["state"]
        leg.ref_premium = d.get("ref_premium", 0.0)
        leg.v_entry = d.get("v_entry", 0.0)
        leg.v_sl = d.get("v_sl", 0.0)
        leg.entry_price = d.get("entry_price", 0.0)
        leg.sl_price = d.get("sl_price", 0.0)
        leg.entry_date = d.get("entry_date", "")
        leg.be_armed = bool(d.get("be_armed", False))
        leg.last_signal_candle = d.get("last_signal_candle", "")
        leg.realized_pnl = d.get("realized_pnl", 0.0)
        leg.charges_total = d.get("charges_total", 0.0)
        return leg

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "opt_type": self.opt_type,
            "quantity": self.quantity,
            "state": self.state,
            "ref_premium": self.ref_premium,
            "v_entry": self.v_entry,
            "v_sl": self.v_sl,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "entry_date": self.entry_date,
            "be_armed": self.be_armed,
            "last_signal_candle": self.last_signal_candle,
            "realized_pnl": self.realized_pnl,
            "charges_total": self.charges_total,
        }

    @property
    def is_long(self) -> bool:
        return self.state == self.IN_LONG

    @property
    def is_done(self) -> bool:
        return self.state == self.DONE

    def _log_cycle_pnl(self, exit_price: float, reason: str):
        """Long PnL = (exit − entry) × qty. Updates self.realized_pnl."""
        cycle_pnl = (exit_price - self.entry_price) * self.quantity
        charges = _option_charges(
            buy_price=self.entry_price,
            sell_price=exit_price,
            quantity=self.quantity,
        )
        self.realized_pnl += cycle_pnl
        self.charges_total += charges["total"]
        log.info(
            "[%s FLIP %s] entry=%.2f  exit=%.2f  qty=%d  Gross=%s%.2f  Charges=%.2f  Net=%s%.2f",
            self.opt_type,
            reason,
            self.entry_price,
            exit_price,
            self.quantity,
            "+" if cycle_pnl >= 0 else "",
            cycle_pnl,
            charges["total"],
            "+" if cycle_pnl - charges["total"] >= 0 else "",
            cycle_pnl - charges["total"],
        )
        _journal_cycle(
            self.opt_type,
            reason,
            self.entry_price,
            exit_price,
            self.quantity,
            cycle_pnl,
            charges,
        )

    def on_signal_candle(self, candle_minute: str, close: float, save_cb) -> bool:
        """Evaluate one closed 1-minute candle for the paper-short flip signal.

        Returns True on a state transition. Caller gates the entry window and
        the max-1-entry rule (IN_LONG/DONE legs are never passed here)."""
        if not candle_minute or candle_minute == self.last_signal_candle:
            return False
        self.last_signal_candle = candle_minute

        if close <= 0 or self.ref_premium <= 0:
            return False

        if self.state == self.WATCHING:
            trigger = self.ref_premium * (1 - DROP_PCT / 100.0)
            if close <= trigger:
                self.v_entry = close
                self.v_sl = round(close * (1 + VSL_PCT / 100.0), 2)
                self.state = self.PAPER_SHORT
                log.info(
                    "[%s] PAPER SHORT: close=%.2f ≤ trigger=%.2f (ref=%.2f) — "
                    "virtual SL / real-BUY trigger at %.2f",
                    self.opt_type,
                    close,
                    trigger,
                    self.ref_premium,
                    self.v_sl,
                )
                save_cb()
                return True
            return False

        if self.state == self.PAPER_SHORT:
            if close < self.v_sl:
                return False
            # Paper short stopped out — upward momentum. VIX gate: on a block
            # the signal stays armed and may fire on a later candle.
            if not vix_ok():
                return False
            log.info(
                "[%s] FLIP: close=%.2f ≥ virtual SL %.2f (paper entry %.2f) — buying.",
                self.opt_type,
                close,
                self.v_sl,
                self.v_entry,
            )
            return self._buy(close, save_cb)

        return False

    def _buy(self, ref_ltp: float, save_cb) -> bool:
        oid = _place_order(self.symbol, "BUY", self.quantity, reason=f"{self.opt_type}_FLIP_BUY")
        if not oid:
            log.error("[%s] BUY failed — NOT entering position.", self.opt_type)
            return False
        filled = _get_fill_price(oid, self.symbol, ref_ltp)
        self.entry_price = filled
        self.sl_price = round(filled * (1 - REAL_SL_PCT / 100.0), 2)
        self.entry_date = _now_ist().date().isoformat()
        self.be_armed = False  # fresh position → fresh breakeven state
        self.state = self.IN_LONG
        log.info(
            "[%s] LONG entered  entry=%.2f  sl=%.2f  qty=%d",
            self.opt_type,
            filled,
            self.sl_price,
            self.quantity,
        )
        _notify(
            f"FLIP BUY {self.opt_type} {self.symbol} @ ₹{filled:.2f} "
            f"(SL ₹{self.sl_price:.2f}, qty {self.quantity})"
        )
        save_cb()
        return True

    def on_tick(self, ltp: float, save_cb) -> bool:
        """Live-LTP stop-loss check for the open long."""
        if self.state != self.IN_LONG or ltp <= 0:
            return False
        if ltp > self.sl_price:
            return False
        log.info("[%s] STOP-LOSS: LTP %.2f ≤ SL %.2f — exiting.", self.opt_type, ltp, self.sl_price)
        return self._close_at_market(save_cb, "SL", f"{self.opt_type}-SL")

    def on_be_candle(self, close: float, save_cb) -> bool:
        """Breakeven stop on 1-minute candle closes (backtest fidelity).

        Arms on ANY day once close ≥ entry × (1 + BE_TRIGGER_PCT%). Exits at
        market only on Day 2+ when the armed stop sees close ≤ entry — Day-1
        pullbacks are tolerated (a plain any-day BE stop cost ₹44.5k of profit
        in the backtest; the Day-2-only version cost ₹4.2k and cut max DD 18%).
        """
        if BE_TRIGGER_PCT <= 0:
            return False
        if self.state != self.IN_LONG or close <= 0 or self.entry_price <= 0:
            return False

        if not self.be_armed and close >= self.entry_price * (1 + BE_TRIGGER_PCT / 100.0):
            self.be_armed = True
            log.info(
                "[%s] BREAKEVEN ARMED: close %.2f ≥ entry %.2f × %.2f.",
                self.opt_type,
                close,
                self.entry_price,
                1 + BE_TRIGGER_PCT / 100.0,
            )
            _notify(
                f"Breakeven armed on {self.symbol}: premium touched "
                f"+{BE_TRIGGER_PCT:.0f}% (₹{close:.2f}). Day-2 giveback to entry "
                f"₹{self.entry_price:.2f} will exit."
            )
            save_cb()
            return False

        is_day2 = bool(self.entry_date) and _now_ist().date().isoformat() > self.entry_date
        if self.be_armed and is_day2 and close <= self.entry_price:
            log.info(
                "[%s] BREAKEVEN STOP: Day-2 close %.2f ≤ entry %.2f — dying trade, exiting.",
                self.opt_type,
                close,
                self.entry_price,
            )
            return self._close_at_market(save_cb, "BE", f"{self.opt_type}-BE")
        return False

    def _close_at_market(self, save_cb, reason: str, label: str) -> bool:
        """Close the long. Smart exit reconciles to flat (idempotent, safe on
        state drift); fallback is a plain critical SELL."""
        if self.state != self.IN_LONG:
            return False

        if USE_SMART_EXIT:
            oid, outcome = _smart_flatten(self.symbol, label, critical=(reason != "SL"))
            if outcome == "failed":
                save_cb()  # keep IN_LONG so the operator knows it's still live
                return False
            if outcome == "flat":
                # Closed externally (drift) — book at last known price.
                exit_price = live_ltp(self.symbol) or self.sl_price or self.entry_price
            else:
                exit_price = _get_fill_price(oid, self.symbol, live_ltp(self.symbol))
        else:
            oid = _place_critical_order(self.symbol, "SELL", self.quantity, label=label)
            if not oid:
                save_cb()
                return False
            exit_price = _get_fill_price(oid, self.symbol, live_ltp(self.symbol))

        self._log_cycle_pnl(exit_price, reason)
        self.state = self.DONE
        _notify(
            f"{reason} exit {self.opt_type} {self.symbol} @ ₹{exit_price:.2f} "
            f"(entry ₹{self.entry_price:.2f}, net ₹{self.realized_pnl - self.charges_total:,.2f})"
        )
        save_cb()
        return True

    def force_exit(self, save_cb) -> bool:
        if self.state != self.IN_LONG:
            return False
        log.info("[%s] Forced exit at market.", self.opt_type)
        return self._close_at_market(save_cb, "FORCED", f"{self.opt_type}-FORCED")


# ─────────────────────────────────────────────────────────────────────────────
#  BROKER RECONCILIATION  (Day-2 startup — same approach as engine.py v3.5)
# ─────────────────────────────────────────────────────────────────────────────


def _broker_net_qty(symbol: str) -> int | None:
    try:
        resp = client.openposition(
            strategy=STRATEGY, symbol=symbol, exchange=OPT_EXCHANGE, product=PRODUCT
        )
        if isinstance(resp, dict) and resp.get("status") == "success":
            return int(float(resp.get("quantity", 0) or 0))
        log.warning("[RECONCILE] openposition non-success for %s: %s", symbol, resp)
        return None
    except Exception as exc:
        log.warning("[RECONCILE] openposition failed for %s: %s", symbol, exc)
        return None


def _reconcile_with_broker(legs: list, save_cb) -> None:
    """Mark externally-closed longs DONE at Day-2 start (no order placed)."""
    changed = False
    for leg in legs:
        if leg.state != FlipLeg.IN_LONG:
            continue
        net = _broker_net_qty(leg.symbol)
        if net is None:
            log.info(
                "[RECONCILE] %s %s: broker lookup failed — keeping IN_LONG.",
                leg.opt_type,
                leg.symbol,
            )
            continue
        if net == 0:
            log.warning(
                "[RECONCILE] %s %s: engine=IN_LONG but broker FLAT — closed "
                "externally. Marking DONE (no order placed).",
                leg.opt_type,
                leg.symbol,
            )
            _notify(
                f"Startup reconcile: {leg.opt_type} {leg.symbol} already flat at broker "
                "(closed externally) — leg marked closed, not monitored."
            )
            leg.state = FlipLeg.DONE
            changed = True
        else:
            log.info(
                "[RECONCILE] %s %s: broker net=%d — position confirmed, monitoring.",
                leg.opt_type,
                leg.symbol,
                net,
            )
    if changed:
        save_cb()


def _kill_session(legs: list, expiry: str, save_cb, total: float) -> None:
    """Max-loss kill switch: close the long at market and end the session."""
    log.critical(
        "[KILL] Session net %.2f breached max loss ₹%.0f — force-exiting.",
        total,
        MAX_LOSS,
    )
    _notify(
        f"KILL SWITCH: net ₹{total:,.2f} breached max loss ₹{MAX_LOSS:,.0f} — "
        f"closing position at market."
    )

    for leg in legs:
        if leg.is_long:
            leg.force_exit(save_cb)

    open_legs = [leg for leg in legs if leg.is_long]

    try:
        ws_unsubscribe([leg.symbol for leg in legs])
    except Exception:
        pass

    _append_history("KILLED", legs, expiry)
    _set_phase(
        "KILLED",
        legs,
        expiry,
        message=f"Max loss ₹{MAX_LOSS:,.0f} hit — session terminated",
    )

    if not open_legs:
        delete_state()
        _notify("Kill switch complete — position closed.")
    else:
        save_cb()
        log.critical("[KILL] *** Exit FAILED — position remains at broker. MANUAL ACTION. ***")
        _notify("KILL SWITCH WARNING: exit FAILED — check broker positions NOW.")


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY-DAY FILTERS
# ─────────────────────────────────────────────────────────────────────────────


def entry_allowed_today() -> tuple[bool, str]:
    """Apply the pattern filters: weekday, DTE window, expiry day.

    Returns (allowed, human-readable reason when blocked)."""
    wd = _weekday_key()
    if ENTRY_WEEKDAYS and wd not in ENTRY_WEEKDAYS:
        return False, f"No entries on {_now_ist().strftime('%A')} (weekend theta filter)"
    try:
        _, dte = get_next_expiry_with_dte()
    except RuntimeError as exc:
        return False, f"Expiry lookup failed: {exc}"
    if dte < max(DTE_MIN, 1):
        # dte==0 cannot happen via get_next_expiry (strictly future), but a
        # same-week Thursday start with expiry tomorrow gives dte=1 — allowed.
        return False, f"DTE {dte} below minimum {DTE_MIN}"
    if dte > DTE_MAX:
        return False, f"DTE {dte} above maximum {DTE_MAX} (theta filter)"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
#  DAY-1
# ─────────────────────────────────────────────────────────────────────────────


def run_day1():
    """
    ref time (11:00)   chain snapshot → ITM-N CE reference premium
    entry window       candle-close paper-short flip signal; real SL on ticks
    ws close (15:29)   disconnect; persist state if long
    """
    strategy_start = time.monotonic()

    _set_phase("DAY1_WAIT", message=f"Waiting for {REF_H:02d}:{REF_M:02d} reference snapshot")
    wait_until(REF_H, REF_M, "Day-1 reference snapshot")
    if _shutdown.is_set():
        return

    log.info("=" * 60)
    log.info("DAY-1  %02d:%02d  %s BTST Flip Session Starting", REF_H, REF_M, UNDERLYING)
    log.info("=" * 60)

    expiry, dte = get_next_expiry_with_dte()
    log.info("Trading expiry: %s (DTE=%d)", expiry, dte)

    chain = get_chain(expiry)
    lot_size = get_lot_size(chain)
    quantity = lot_size * LOT_MULTIPLIER
    log.info("Lot size=%d  multiplier=%d  quantity=%d", lot_size, LOT_MULTIPLIER, quantity)
    log.info(
        "%s LTP=%.2f  ATM=%s", UNDERLYING, chain.get("underlying_ltp"), chain.get("atm_strike")
    )

    ce_sym, ce_ref = find_itm_ce(chain)
    log.info("ITM-%d CE: %-30s ref=%.2f", MONEYNESS, ce_sym, ce_ref)

    leg = FlipLeg(ce_sym, "CE", quantity)
    leg.ref_premium = ce_ref
    legs = [leg]

    def _save():
        save_state(legs, expiry)

    _set_phase(
        "DAY1_LIVE",
        legs,
        expiry,
        message=(
            f"Watching for {DROP_PCT:.0f}% drop → paper short → "
            f"+{VSL_PCT:.0f}% flip buy ({ENTRY_START_H:02d}:{ENTRY_START_M:02d}"
            f"–{ENTRY_END_H:02d}:{ENTRY_END_M:02d})"
        ),
    )

    ws_subscribe([ce_sym])
    _wait_for_tick(timeout=30)
    log.info("[WS] Feed live. Waiting for the paper-short flip signal...")

    entry_start = _now_ist().replace(
        hour=ENTRY_START_H, minute=ENTRY_START_M, second=0, microsecond=0
    )
    # The candle labeled entry_end closes one minute later; allow one more
    # minute for the history API to publish it before declaring no-trade.
    window_deadline = _now_ist().replace(
        hour=ENTRY_END_H, minute=ENTRY_END_M, second=0, microsecond=0
    ) + timedelta(minutes=2)
    _last_status_log = 0.0

    while not _shutdown.is_set():
        if time.monotonic() - strategy_start > STRATEGY_TIMEOUT_S:
            log.error("Strategy wall-clock timeout (%ds) — aborting.", STRATEGY_TIMEOUT_S)
            _shutdown.set()
            break

        # Signal window closed and nothing held → done for the day.
        if leg.state in (FlipLeg.WATCHING, FlipLeg.PAPER_SHORT) and _now_ist() >= window_deadline:
            log.info(
                "Entry window closed (%02d:%02d) with no fill — no trade today.",
                ENTRY_END_H,
                ENTRY_END_M,
            )
            leg.state = FlipLeg.DONE
            break

        if leg.is_done:
            break

        if leg.is_long and now_past(WS_CLOSE_H, WS_CLOSE_M):
            break  # persist + disconnect below

        ws_reconnect_if_stale([ce_sym])
        _wait_for_tick(timeout=1)

        # Real SL on live ticks (bar-low fidelity, conservative); breakeven
        # ARMS on candle closes (its exit branch is Day-2-gated, inert today).
        if leg.is_long:
            ltp = live_ltp(ce_sym)
            if ltp > 0:
                leg.on_tick(ltp, _save)
            if leg.is_long:
                closed = latest_closed_candle(ce_sym)
                if closed:
                    leg.on_be_candle(closed[1], _save)
        # Flip signal on closed 1-minute candles inside the entry window.
        elif leg.state in (FlipLeg.WATCHING, FlipLeg.PAPER_SHORT):
            closed = latest_closed_candle(ce_sym)
            if closed:
                candle_minute, candle_close = closed
                try:
                    candle_dt = datetime.fromisoformat(candle_minute)
                except ValueError:
                    candle_dt = None
                if candle_dt is not None and candle_dt >= entry_start:
                    end_gate = candle_dt.replace(
                        hour=ENTRY_END_H, minute=ENTRY_END_M, second=0, microsecond=0
                    )
                    if candle_dt <= end_gate:
                        leg.on_signal_candle(candle_minute, candle_close, _save)

        _maybe_refresh_status(legs, expiry)

        if MAX_LOSS > 0 and leg.is_long:
            total = _total_net(legs)
            if total <= -MAX_LOSS:
                _kill_session(legs, expiry, _save, total)
                return

        now_ts = time.time()
        if now_ts - _last_status_log >= 120:
            log.info(
                "CE %s=%.2f [%s]  ref=%.2f  v_entry=%.2f  v_sl=%.2f",
                ce_sym,
                live_ltp(ce_sym),
                leg.state,
                leg.ref_premium,
                leg.v_entry,
                leg.v_sl,
            )
            _last_status_log = now_ts

    # ── ws close — disconnect + persist if a long is carried overnight ───────
    if leg.is_long:
        wait_until(WS_CLOSE_H, WS_CLOSE_M, "WS close")
        # SL may still hit between here and close on the last cached ticks.
        ltp = live_ltp(ce_sym)
        if ltp > 0 and leg.is_long:
            leg.on_tick(ltp, _save)

    ws_unsubscribe([ce_sym])

    if leg.is_long:
        _save()
        log.info("[STATE] Persisted for Day-2 (long %s @ %.2f)", leg.symbol, leg.entry_price)
        _set_phase(
            "DAY1_DONE",
            legs,
            expiry,
            message="Day-1 complete — long carried overnight, Day-2 exit next session",
        )
        _notify(
            f"Carrying overnight: LONG {leg.symbol} @ ₹{leg.entry_price:.2f} "
            f"(SL ₹{leg.sl_price:.2f}). Day-2 force exit "
            f"{EXIT_H:02d}:{EXIT_M:02d}."
        )
    else:
        delete_state()
        if leg.entry_price > 0:
            # Entered and stopped out same day.
            _append_history("DONE", legs, expiry)
            _notify(f"Day-1 closed flat — session over. Net P&L: ₹{_total_net(legs):,.2f}")
            _set_phase("DONE", legs, expiry, message="Stopped out Day-1 — session finished")
        else:
            _set_phase("DONE", legs, expiry, message="No signal today — session finished flat")

    log.info("=" * 60)
    log.info("DAY-1  Complete.")
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
#  DAY-2
# ─────────────────────────────────────────────────────────────────────────────


def run_day2():
    """
    process start      broker reconcile (pre-open safe)
    day2 open (09:16)  WS subscribe → SL monitoring
    force exit (10:30) SELL remaining long at market → cleanup
    """
    state = load_state()
    if not state:
        log.info("[DAY-2] No state file — nothing to exit.")
        return

    legs = [FlipLeg.from_dict(d) for d in state.get("main_legs", [])]
    expiry = state.get("expiry", "")

    def _save():
        save_state(legs, expiry)

    _reconcile_with_broker(legs, _save)
    active = [leg for leg in legs if leg.is_long]

    if not active:
        log.info("[DAY-2] Position already closed (reconciled flat at startup).")
        _append_history("DONE", legs, expiry)
        delete_state()
        _set_phase("DONE", legs, expiry, message="Nothing open — session complete")
        return

    _set_phase(
        "DAY2",
        legs,
        expiry,
        message=f"Waiting for {DAY2_H:02d}:{DAY2_M:02d} Day-2 open",
    )
    wait_until(DAY2_H, DAY2_M, "Day-2 start (skip first-minute spike)")
    if _shutdown.is_set():
        return

    log.info("=" * 60)
    log.info("DAY-2  %02d:%02d  %s BTST Flip Exit Session", DAY2_H, DAY2_M, UNDERLYING)
    log.info("=" * 60)

    symbols = [leg.symbol for leg in active]
    ws_subscribe(symbols)
    _wait_for_tick(timeout=30)

    for leg in active:
        log.info(
            "[%s] Loaded state=%s  entry=%.2f  sl=%.2f",
            leg.opt_type,
            leg.state,
            leg.entry_price,
            leg.sl_price,
        )

    log.info("[DAY-2] Monitoring SL until %02d:%02d forced exit...", EXIT_H, EXIT_M)

    _last_status_log = 0.0

    while not _shutdown.is_set():
        if now_past(EXIT_H, EXIT_M):
            log.info("[DAY-2] %02d:%02d — proceeding to forced exit.", EXIT_H, EXIT_M)
            break

        if all(leg.is_done for leg in active):
            log.info("[DAY-2] Position closed before forced exit.")
            break

        ws_reconnect_if_stale(symbols)
        _wait_for_tick(timeout=1)

        for leg in active:
            if leg.is_long and not _shutdown.is_set():
                ltp = live_ltp(leg.symbol)
                if ltp > 0:
                    leg.on_tick(ltp, _save)
                # Breakeven stop: Day-2 giveback to entry = dying trade.
                if leg.is_long:
                    closed = latest_closed_candle(leg.symbol)
                    if closed:
                        leg.on_be_candle(closed[1], _save)

        _maybe_refresh_status(legs, expiry)

        if MAX_LOSS > 0 and any(leg.is_long for leg in legs):
            total = _total_net(legs)
            if total <= -MAX_LOSS:
                _kill_session(legs, expiry, _save, total)
                return

        now_ts = time.time()
        if now_ts - _last_status_log >= 120:
            log.info(
                "  ".join(f"{leg.opt_type} {live_ltp(leg.symbol):.2f}[{leg.state}]" for leg in active)
            )
            _last_status_log = now_ts

    for leg in active:
        leg.force_exit(_save)

    ws_unsubscribe(symbols)

    open_legs = [leg for leg in legs if leg.is_long]
    if not open_legs:
        delete_state()
    else:
        log.critical(
            "[DAY-2] *** State file PRESERVED — position remains at broker. "
            "MANUAL ACTION REQUIRED: %s ***",
            [f"{l.opt_type}:{l.symbol}" for l in open_legs],
        )
        _notify(
            "CRITICAL: Day-2 exit incomplete — position remains at the broker. "
            "MANUAL ACTION REQUIRED."
        )
    session_net = _total_net(legs)
    _append_history("DONE", legs, expiry)
    _set_phase("DONE", legs, expiry, message="Session finished")
    _notify(f"Session finished. Net P&L: ₹{session_net:,.2f}")
    log.info("=" * 60)
    log.info("DAY-2  Complete. %s BTST Flip session finished.", UNDERLYING)
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def _state_has_work(state: dict | None) -> bool:
    if not state:
        return False
    return any(leg.get("state") == FlipLeg.IN_LONG for leg in state.get("main_legs", []))


def _state_entry_date(state: dict) -> str:
    """Entry date of the held position. save_state stamps trade_date with the
    SAVE date (today on every Day-2 persist), so day routing must use the
    leg's own entry_date; trade_date is only the legacy fallback."""
    for leg in state.get("main_legs", []):
        if leg.get("state") == FlipLeg.IN_LONG and leg.get("entry_date"):
            return str(leg["entry_date"])
    return str(state.get("trade_date") or "")


def main():
    if not API_KEY:
        log.error(
            "OPENALGO_API_KEY not injected — generate an API key at /apikey "
            "and start this strategy through the /stbt tab. Aborting."
        )
        return
    if not HOST:
        log.error("OPENALGO_HOST / HOST_SERVER not set — aborting.")
        return

    log.info("=" * 60)
    log.info(
        "%s BTST Flip engine v%s  |  id=%s  |  ITM-%d CE  |  lots=%d  |  "
        "drop=%.1f%%  vsl=%.1f%%  sl=%.1f%%  vix≤%.1f  dte=%d–%d  |  host=%s",
        UNDERLYING,
        VERSION,
        STRATEGY_ID,
        MONEYNESS,
        LOT_MULTIPLIER,
        DROP_PCT,
        VSL_PCT,
        REAL_SL_PCT,
        VIX_MAX,
        DTE_MIN,
        DTE_MAX,
        HOST,
    )
    log.info("=" * 60)
    _set_phase("STARTING", message="Engine starting")

    try:
        expiry_day = is_expiry_day()
        state = load_state()

        if state and not _state_has_work(state):
            log.warning("[STATE] Loaded state has no open long — discarding as junk.")
            delete_state()
            state = None

        log.info("Expiry day : %s", "YES — no new entries today" if expiry_day else "No")
        log.info("Saved state: %s", state.get("trade_date") if state else "None")

        today_iso = _now_ist().date().isoformat()

        # Position entered on a PRIOR day → Day-2 exit first. Routing keys on
        # the leg's entry_date (see _state_entry_date) so a mid-Day-2 restart
        # still runs the 10:30 force exit.
        if state and _state_entry_date(state) != today_iso:
            log.info("Prior-day position found → Day-2 exit first.")
            run_day2()
            state = None  # deleted inside run_day2

        # Position entered TODAY (mid-day restart while long): re-entering
        # run_day1 is wrong (window math), so just monitor the SL until
        # ws-close and re-persist for tomorrow's Day-2.
        if state and _state_entry_date(state) == today_iso:
            log.info("Today's position found (mid-day restart) → resuming SL monitoring.")
            _resume_same_day(state)
            return

        # Fresh Day-1 — apply the entry filters first.
        if expiry_day:
            log.info("Expiry day — no new entries (cannot hold overnight).")
            _set_phase("EXPIRY_DAY", message="Expiry day — no new entries")
            return

        allowed, reason = entry_allowed_today()
        if not allowed:
            log.info("Entry filters block today: %s", reason)
            _set_phase("NO_ENTRY", message=reason)
            return

        run_day1()

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting cleanly.")
    except Exception as exc:
        log.exception("Fatal unhandled exception.")
        _set_phase("ERROR", message=f"Engine crashed: {exc}")
        _notify(
            f"ENGINE ERROR: {exc} — check the strategy log. If a position is "
            f"open, verify it at the broker."
        )


def _resume_same_day(state: dict):
    """Mid-day restart while holding today's long: reconcile, monitor the SL
    until ws-close, then persist for Day-2 (mirrors the tail of run_day1)."""
    legs = [FlipLeg.from_dict(d) for d in state.get("main_legs", [])]
    expiry = state.get("expiry", "")

    def _save():
        save_state(legs, expiry)

    _reconcile_with_broker(legs, _save)
    active = [leg for leg in legs if leg.is_long]
    if not active:
        log.info("[RESUME] Position already closed — session complete.")
        _append_history("DONE", legs, expiry)
        delete_state()
        _set_phase("DONE", legs, expiry, message="Nothing open — session complete")
        return

    symbols = [leg.symbol for leg in active]
    ws_subscribe(symbols)
    _wait_for_tick(timeout=30)
    _set_phase("DAY1_LIVE", legs, expiry, message="Resumed — monitoring stop-loss")
    log.info("[RESUME] Monitoring SL until %02d:%02d WS close.", WS_CLOSE_H, WS_CLOSE_M)

    while not _shutdown.is_set():
        if now_past(WS_CLOSE_H, WS_CLOSE_M):
            break
        if all(leg.is_done for leg in active):
            break
        ws_reconnect_if_stale(symbols)
        _wait_for_tick(timeout=1)
        for leg in active:
            if leg.is_long:
                ltp = live_ltp(leg.symbol)
                if ltp > 0:
                    leg.on_tick(ltp, _save)
                if leg.is_long:
                    closed = latest_closed_candle(leg.symbol)
                    if closed:
                        leg.on_be_candle(closed[1], _save)
        _maybe_refresh_status(legs, expiry)
        if MAX_LOSS > 0 and any(leg.is_long for leg in legs):
            total = _total_net(legs)
            if total <= -MAX_LOSS:
                _kill_session(legs, expiry, _save, total)
                return

    ws_unsubscribe(symbols)

    if any(leg.is_long for leg in legs):
        _save()
        _set_phase("DAY1_DONE", legs, expiry, message="Long carried overnight — Day-2 exit next")
    else:
        _append_history("DONE", legs, expiry)
        delete_state()
        _set_phase("DONE", legs, expiry, message="Stopped out — session finished")


if __name__ == "__main__":
    main()
