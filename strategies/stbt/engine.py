#!/usr/bin/env python
# =============================================================================
#  STBT ENGINE  v3.0.0  — config-driven generic engine for the /stbt tab
#  (evolution of SENSEX STBT v2; trading logic unchanged)
#
#  Strategy : Sell Today Buy Tomorrow — short ITM-2 CE + PE overnight,
#             hedged on the naked leg with a cheap OTM option near the
#             configured hedge target premium.
#
#  WHAT CHANGED vs v2
#  ──────────────────
#  • ALL tunables (underlying, entry drop %, SL %, re-entry, hedge target,
#    lot multiplier, session times, charge rates) load from
#    strategies/stbt/{STRATEGY_ID}_config.json — no code edits to retune.
#  • Underlying is configurable (SENSEX/BANKEX → BFO, NIFTY/BANKNIFTY/... → NFO).
#  • Credentials come ONLY from the environment injected by the Strategy
#    Manager (OPENALGO_API_KEY, OPENALGO_HOST/HOST_SERVER, WEBSOCKET_URL).
#    No hardcoded fallbacks — the process exits loudly if they are missing.
#  • Writes strategies/stbt/{STRATEGY_ID}_status.json (atomic) on every
#    state change so the /stbt tab can render live leg status and P&L.
#  • State / order-intent / status files are keyed by STRATEGY_ID so several
#    STBT configs can run side by side.
#
#  STRATEGY RULES  (unchanged from v2; times/percentages now from config)
#  ───────────────
#  DAY-1  entry time (default 11:00)
#    1. Fetch option chain → identify ITM-2 CE and PE symbols and snapshot
#       their premiums as reference prices.
#    2. Subscribe WebSocket on both symbols.
#    3. On every live WS tick:
#         • Premium drops ≥ entry_drop_pct below reference → SELL (NRML)
#         • LTP ≥ entry_price × (1 + sl_pct/100)           → BUY back (SL)
#         • After SL: re-entry per reentry_method (candle close / LTP)
#
#  DAY-1  hedge time (default 15:26) — overnight hedge on a single naked leg.
#  DAY-1  ws close (default 15:29)  — disconnect WS; persist state.
#  DAY-2  open (default 09:16)      — sell hedge, resume SL monitoring.
#  DAY-2  force exit (default 10:30) — close remaining shorts; cleanup.
#  EXPIRY DAY — skip Day-1; run Day-2 exit only if prior state exists.
#
#  RUN SETUP — managed automatically by the /stbt tab (blueprints/stbt.py):
#  it writes the config JSON, registers a launcher with the /python host,
#  and schedules it 09:10–16:00 Mon–Fri.
# =============================================================================

import os
import json
import time
import signal
import threading
import logging
import queue
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from openalgo import api

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  — loaded from strategies/stbt/{STRATEGY_ID}_config.json
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "3.1.0"

IST = ZoneInfo("Asia/Kolkata")

STBT_DIR = Path(__file__).resolve().parent

# STRATEGY_ID is injected by the /python Strategy Manager subprocess launcher.
STRATEGY_ID = os.getenv("STRATEGY_ID", "").strip()
if not STRATEGY_ID:
    raise SystemExit(
        "[STBT] STRATEGY_ID env var missing — this engine must be launched "
        "through the OpenAlgo Strategy Manager (via the /stbt tab)."
    )

# Underlying → (index exchange for the option chain, options exchange).
INDEX_MAP = {
    "SENSEX":     ("BSE_INDEX", "BFO"),
    "BANKEX":     ("BSE_INDEX", "BFO"),
    "NIFTY":      ("NSE_INDEX", "NFO"),
    "BANKNIFTY":  ("NSE_INDEX", "NFO"),
    "FINNIFTY":   ("NSE_INDEX", "NFO"),
    "MIDCPNIFTY": ("NSE_INDEX", "NFO"),
}

_CONFIG_FILE = STBT_DIR / f"{STRATEGY_ID}_config.json"

_CONFIG_DEFAULTS = {
    "underlying": "SENSEX",
    "product": "NRML",              # overnight — must NOT be MIS
    "default_lot_size": 20,         # fallback if API doesn't return lot size
    "lot_multiplier": 1,
    "entry_drop_pct": 5.0,          # % drop from reference to trigger SELL
    "sl_pct": 20.0,                 # stop-loss = entry × (1 + sl_pct/100)
    "max_reentries": 1,
    "reentry_method": "CANDLE_CLOSE",   # CANDLE_CLOSE = StockMock style | LTP
    "reentry_candle_source": "HISTORY",  # HISTORY = 1m candle via history API | WS
    "history_interval": "1m",
    "allow_day2_reentry": True,
    "check_next_day_after": "09:16",
    "hedge_target_premium": 20.0,   # buy OTM strike whose LTP is closest to this
    "max_loss": 0.0,                # session kill switch in ₹ (0 = disabled)
    "telegram_alerts": True,        # strategy-level Telegram notifications
    "user_id": "",                  # owner username (written by blueprints/stbt.py)
    "entry_time": "11:00",
    "hedge_time": "15:26",
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
        raise SystemExit(f"[STBT] Config file not found: {_CONFIG_FILE}")
    # utf-8-sig: tolerate a BOM from hand-edited configs on Windows.
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
# HOST_SERVER (from .env, e.g. https://yourdomain) takes priority: the host
# injects OPENALGO_HOST with a 127.0.0.1:5000 fallback that is wrong on
# production gunicorn+nginx deployments (nothing listens on that port there).
API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
HOST    = (os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST") or "").strip()
WS_URL  = os.getenv("WEBSOCKET_URL", "").strip() or None

# ── Instrument ───────────────────────────────────────────────────────────────
UNDERLYING = str(_CFG["underlying"]).upper()
if UNDERLYING not in INDEX_MAP:
    raise SystemExit(f"[STBT] Unsupported underlying {UNDERLYING!r} — "
                     f"supported: {sorted(INDEX_MAP)}")
INDEX_EXCHANGE, OPT_EXCHANGE = INDEX_MAP[UNDERLYING]
STRATEGY = f"{UNDERLYING}_STBT"

# ── Position sizing ──────────────────────────────────────────────────────────
PRODUCT          = str(_CFG["product"]).upper()
DEFAULT_LOT_SIZE = int(_CFG["default_lot_size"])
LOT_MULTIPLIER   = int(_CFG["lot_multiplier"])
# QUANTITY is computed at runtime via get_lot_size() to avoid stale hardcodes

# ── Strategy parameters ──────────────────────────────────────────────────────
ENTRY_DROP_PCT = float(_CFG["entry_drop_pct"])
SL_PCT         = float(_CFG["sl_pct"])
MAX_REENTRIES  = int(_CFG["max_reentries"])

REENTRY_METHOD        = str(_CFG["reentry_method"]).upper()
REENTRY_CANDLE_SOURCE = str(_CFG["reentry_candle_source"]).upper()
HISTORY_INTERVAL      = str(_CFG["history_interval"])
ALLOW_DAY2_REENTRY    = bool(_CFG["allow_day2_reentry"])
CHECK_NEXT_DAY_AFTER_H, CHECK_NEXT_DAY_AFTER_M = _hhmm(_CFG["check_next_day_after"])

HEDGE_TARGET_PREMIUM = float(_CFG["hedge_target_premium"])
MAX_LOSS             = float(_CFG["max_loss"])       # 0 = kill switch disabled
TELEGRAM_ALERTS      = bool(_CFG["telegram_alerts"])
OWNER_USER_ID        = str(_CFG["user_id"] or "")

# Charges / brokerage estimate.
BROKERAGE_PER_ORDER        = float(_CFG["brokerage_per_order"])
BROKERAGE_PCT              = float(_CFG["brokerage_pct"])
STT_OPTION_SELL_RATE       = float(_CFG["stt_option_sell_rate"])
BSE_OPTION_TXN_RATE        = float(_CFG["exchange_txn_rate"])
SEBI_TURNOVER_RATE         = float(_CFG["sebi_turnover_rate"])
STAMP_DUTY_OPTION_BUY_RATE = float(_CFG["stamp_duty_option_buy_rate"])
GST_RATE                   = float(_CFG["gst_rate"])

# ── Timing  (24-hour, IST) ───────────────────────────────────────────────────
ENTRY_H,    ENTRY_M    = _hhmm(_CFG["entry_time"])       # snapshot + WS subscribe
HEDGE_H,    HEDGE_M    = _hhmm(_CFG["hedge_time"])       # overnight hedge placement
WS_CLOSE_H, WS_CLOSE_M = _hhmm(_CFG["ws_close_time"])    # WS disconnect
DAY2_H,     DAY2_M     = _hhmm(_CFG["day2_open_time"])   # Day-2 open (skip spike)
EXIT_H,     EXIT_M     = _hhmm(_CFG["force_exit_time"])  # forced exit

# ── Reliability ──────────────────────────────────────────────────────────────
WS_STALE_SECS       = 120   # reconnect WebSocket if silent this long
                             # (BFO options tick slower than index futures)
WS_MAX_RECONNECTS   = 2     # avoid websocket thread leaks on unstable VPS feeds
WS_RECONNECT_COOLDOWN_SECS = 300
REST_LTP_SECS       = 10    # REST quote fallback throttle per symbol
ORDER_RETRIES       = 3     # attempts per order before giving up
ORDER_RETRY_DL      = 2     # seconds between order retries
API_RETRIES         = 3     # attempts per REST call
API_RETRY_DL        = 3     # seconds between REST retries
CLOSE_MAX_RETRIES   = 5     # extra retries for critical exit orders
CLOSE_RETRY_DL      = 3     # seconds between close retries
STRATEGY_TIMEOUT_S  = 25200 # 7-hour hard wall-clock limit for one run

# ── Runtime files (keyed by STRATEGY_ID so multiple configs coexist) ─────────
STATE_FILE        = str(STBT_DIR / f"{STRATEGY_ID}_state.json")
ORDER_INTENT_FILE = str(STBT_DIR / f"{STRATEGY_ID}_order_intent.json")
STATUS_FILE       = str(STBT_DIR / f"{STRATEGY_ID}_status.json")
HISTORY_FILE      = str(STBT_DIR / f"{STRATEGY_ID}_history.json")
HISTORY_MAX_RECORDS = 400

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING  — stdout is captured by the Strategy Manager into per-run log file
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("STBT")

# ─────────────────────────────────────────────────────────────────────────────
#  GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _on_signal(signum, _frame):
    log.warning("Signal %d received — initiating graceful shutdown.", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT,  _on_signal)

# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFICATIONS  (best-effort, never blocks or breaks trading)
# ─────────────────────────────────────────────────────────────────────────────
#
# Reuses the platform's Telegram machinery directly: the alert service is a
# plain httpx call to the Telegram Bot API with the token read from the DB, so
# it works fine from this subprocess (env + cwd are inherited from the host).
# Order-level alerts already fire on every placeorder; these add strategy
# context (entries, SL, hedge, kill switch, session summary).

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

            telegram_alert_service.send_alert_sync(
                chat_id, f"[{UNDERLYING} STBT] {text}"
            )
        except Exception as exc:
            log.warning("[NOTIFY] Telegram send failed: %s", exc)

    threading.Thread(target=_send, daemon=True, name="stbt-notify").start()


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

_ltp_cache    : dict[str, float] = {}
_ltp_lock     = threading.Lock()
_tick_q       : queue.Queue = queue.Queue()
_last_tick_ts = 0.0                 # epoch time of most recent tick
_ws_reconnects = 0
_last_ws_reconnect_ts = 0.0
_ws_degraded = False
_rest_ltp_cache: dict[str, tuple[float, float]] = {}


_ws_first_tick_logged = False


def _on_quote(data: dict):
    """WebSocket callback — the only writer to _ltp_cache.

    Updates _last_tick_ts on EVERY fire (even if LTP parse fails) so the
    stale-reconnect watchdog can't thrash when the broker's payload shape
    differs from what we expect.
    """
    global _last_tick_ts, _ws_first_tick_logged

    # Always mark feed as alive — prevents reconnect loops on unexpected shapes
    with _ltp_lock:
        _last_tick_ts = time.time()

    # One-time debug log so we can see the raw shape in production
    if not _ws_first_tick_logged:
        _ws_first_tick_logged = True
        log.info("[WS] First tick payload keys=%s  sample=%s",
                 list(data.keys()) if isinstance(data, dict) else type(data),
                 str(data)[:200])

    if not isinstance(data, dict):
        return

    sym = data.get("symbol", "")
    # Try common shapes in order: data.data.ltp, data.ltp, data.lp
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
    """Block until a tick arrives or timeout expires, then drain extras."""
    try:
        _tick_q.get(timeout=timeout)
    except queue.Empty:
        pass
    _drain_tick_queue()


def ws_ltp(symbol: str) -> float:
    """Read latest cached LTP (0.0 if not yet received)."""
    with _ltp_lock:
        return _ltp_cache.get(symbol, 0.0)


def _extract_ltp(resp: dict) -> float:
    """Extract LTP from common OpenAlgo quote response shapes."""
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
    """Throttled REST quote fallback used when WS stalls on cloud hosts."""
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
    """Prefer websocket LTP; use REST quote when WS is stale/degraded."""
    cached = ws_ltp(symbol)
    if cached > 0 and not (_ws_degraded or _ws_is_stale()):
        return cached
    return rest_ltp(symbol) or cached


def ws_subscribe(symbols: list[str]):
    """Subscribe via LTP mode — matches examples/python/stoploss_target_example.py.
    LTP mode is mode-1 (lighter payload, more reliable than mode-2 quote+depth
    for the BFO option feed)."""
    global _last_tick_ts, _ws_first_tick_logged
    instruments = [{"exchange": OPT_EXCHANGE, "symbol": s} for s in symbols]
    _ws_first_tick_logged = False   # re-arm one-time debug log per connect
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
    """If WS has been silent for WS_STALE_SECS, reconnect."""
    global _ws_reconnects, _last_ws_reconnect_ts, _ws_degraded

    if not _ws_is_stale():
        return

    if _ws_reconnects >= WS_MAX_RECONNECTS:
        if not _ws_degraded:
            log.error("[WS] Feed silent after %d reconnects — switching to REST LTP fallback.",
                      WS_MAX_RECONNECTS)
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
    log.warning("[WS] Feed silent >%ds — reconnecting (%d/%d).",
                WS_STALE_SECS, _ws_reconnects, WS_MAX_RECONNECTS)
    ws_unsubscribe(symbols)
    time.sleep(5)
    ws_subscribe(symbols)

# ─────────────────────────────────────────────────────────────────────────────
#  RETRY WRAPPERS
# ─────────────────────────────────────────────────────────────────────────────

def _api_call(fn, label: str = "api") -> dict:
    """Call fn() up to API_RETRIES times; require 'status' == 'success'."""
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
    """Parse common OpenAlgo order/trade timestamps into IST-aware datetimes."""
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
    """Find a recent broker-side order/trade that matches a pending intent."""
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
            if (_same_order_shape(row, symbol, action, quantity)
                    and _is_recent_broker_row(row, since_iso)):
                oid = row.get("orderid")
                if oid:
                    log.warning("[ORDER-GUARD] Matched pending intent in orderbook: %s", oid)
                    return str(oid)
    except Exception as exc:
        log.warning("[ORDER-GUARD] orderbook reconciliation failed: %s", exc)

    try:
        resp = client.tradebook()
        for row in _response_items(resp, "tradebook"):
            if (_same_order_shape(row, symbol, action, quantity)
                    and _is_recent_broker_row(row, since_iso)):
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
    if pending and all(pending.get(k) == intent[k] for k in ("symbol", "action", "quantity", "reason")):
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
                log.info("[ORDER] %s %s qty=%d id=%s (attempt %d)",
                         action, symbol, quantity, oid, attempt)
                _clear_order_intent()
                return oid
            log.warning("[ORDER] %s %s attempt %d failed: %s",
                        action, symbol, attempt, resp)
        except Exception as exc:
            log.warning("[ORDER] %s %s attempt %d exception: %s",
                        action, symbol, attempt, exc)
        oid = _reconcile_pending_intent(intent)
        if oid:
            _clear_order_intent()
            return oid
        if attempt < ORDER_RETRIES:
            time.sleep(ORDER_RETRY_DL)
    log.error("[ORDER] %s %s FAILED after %d attempts.", action, symbol, ORDER_RETRIES)
    return None


def _place_critical_order(symbol: str, action: str, quantity: int, label: str) -> str | None:
    """Order with CLOSE_MAX_RETRIES extra attempts. For force-exits / hedge sell."""
    oid = _place_order(symbol, action, quantity, reason=label)
    if oid:
        return oid
    for attempt in range(1, CLOSE_MAX_RETRIES + 1):
        log.warning("[%s] Critical retry %d/%d for %s %s",
                    label, attempt, CLOSE_MAX_RETRIES, action, symbol)
        time.sleep(CLOSE_RETRY_DL)
        oid = _place_order(symbol, action, quantity, reason=label)
        if oid:
            return oid
    log.critical("[%s] *** %s %s FAILED AFTER ALL RETRIES — "
                 "MANUAL ACTION REQUIRED: %s qty=%d ***",
                 label, action, symbol, symbol, quantity)
    _notify(f"CRITICAL [{label}]: {action} {symbol} qty={quantity} FAILED after all "
            f"retries — MANUAL ACTION REQUIRED at the broker.")
    return None


def _get_fill_price(order_id: str, symbol: str, fallback_ltp: float) -> float:
    """Fetch actual fill price from tradebook; fall back to WS LTP / fallback."""
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
#  OPTION CHAIN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_chain(expiry: str) -> dict:
    """Fetch full SENSEX option chain for the given expiry (with retries)."""
    return _api_call(
        lambda: client.optionchain(
            underlying=UNDERLYING,
            exchange=INDEX_EXCHANGE,
            expiry_date=expiry,
        ),
        label="optionchain",
    )


def get_lot_size(chain: dict) -> int:
    """Extract lot size from chain response; fall back to DEFAULT_LOT_SIZE."""
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


def find_itm2(chain: dict) -> tuple[str, float, str, float]:
    """Return (ce_symbol, ce_ltp, pe_symbol, pe_ltp) for ITM-2 strikes."""
    ce_sym = ce_ltp = pe_sym = pe_ltp = None
    for row in chain.get("chain", []):
        ce = row.get("ce")
        pe = row.get("pe")
        if ce and ce.get("label") == "ITM2" and ce_sym is None:
            ce_sym = ce["symbol"]
            ce_ltp = float(ce["ltp"])
        if pe and pe.get("label") == "ITM2" and pe_sym is None:
            pe_sym = pe["symbol"]
            pe_ltp = float(pe["ltp"])
        if ce_sym and pe_sym:
            break
    if not ce_sym or not pe_sym:
        raise RuntimeError("ITM-2 CE or PE not found in option chain response.")
    return ce_sym, ce_ltp, pe_sym, pe_ltp


def find_hedge_strike(chain: dict, opt_type: str) -> tuple[str | None, float | None]:
    """Return OTM (symbol, ltp) whose LTP is closest to HEDGE_TARGET_PREMIUM."""
    best_sym  = None
    best_ltp  = None
    best_diff = float("inf")
    for row in chain.get("chain", []):
        leg = row.get("ce") if opt_type == "CE" else row.get("pe")
        if not leg:
            continue
        if not str(leg.get("label", "")).startswith("OTM"):
            continue
        ltp  = float(leg.get("ltp") or 0)
        diff = abs(ltp - HEDGE_TARGET_PREMIUM)
        if diff < best_diff:
            best_diff = diff
            best_sym  = leg["symbol"]
            best_ltp  = ltp
    if best_sym is None:
        log.warning("[HEDGE] No OTM %s strike found for ₹%.0f target.",
                    opt_type, HEDGE_TARGET_PREMIUM)
    return best_sym, best_ltp

# ─────────────────────────────────────────────────────────────────────────────
#  EXPIRY HELPERS  (live API — no weekday assumption)
# ─────────────────────────────────────────────────────────────────────────────

def _expiry_list() -> list[str]:
    """Return SENSEX BFO option expiry dates, e.g. ['25-APR-25', '02-MAY-25']."""
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
    """'25-APR-25'  →  '25APR25'  (OpenAlgo option symbol format)."""
    return api_date.replace("-", "")


def _parse_api_expiry(s: str) -> date | None:
    """Parse '25-APR-25' → date(2025, 4, 25). Returns None on unexpected format."""
    try:
        return datetime.strptime(s.strip().upper(), "%d-%b-%y").date()
    except (ValueError, AttributeError):
        return None


def get_next_expiry() -> str:
    """Nearest STRICTLY FUTURE expiry in symbol format.

    The broker often keeps the most recently expired date in the list until
    EOD, so filtering by 'not equal to today' isn't enough — we must compare
    as dates and require exp > today.
    """
    today = _now_ist().date()
    future = []
    for exp in _expiry_list():
        exp_date = _parse_api_expiry(exp)
        if exp_date and exp_date > today:
            future.append((exp_date, exp))
    if not future:
        raise RuntimeError("No future expiry found in API response.")
    future.sort(key=lambda x: x[0])   # earliest future date first
    chosen_date, chosen_str = future[0]
    log.info("[EXPIRY] Chosen next expiry: %s (%s)", chosen_str, chosen_date.isoformat())
    return _to_symbol_fmt(chosen_str.upper())


def is_expiry_day() -> bool:
    """True if today matches a SENSEX options expiry date per the API."""
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
#  STATE PERSISTENCE  (atomic write via tmp + replace)
# ─────────────────────────────────────────────────────────────────────────────

# Current lifecycle phase, surfaced to the /stbt tab via the status file.
# DAY1_WAIT → DAY1_LIVE → HEDGED → DAY1_DONE → DAY2 → DONE (or EXPIRY_DAY).
_PHASE = "STARTING"


def _set_phase(phase: str, main_legs: list | None = None, hedge=None,
               expiry: str = "", message: str = ""):
    """Update the lifecycle phase and refresh the status file for the UI."""
    global _PHASE
    _PHASE = phase
    _write_status(main_legs or [], hedge, expiry, message)


def _leg_mtm(leg, ltp: float) -> float:
    """Unrealized P&L of an open short leg at the given LTP (0 if unknown)."""
    if ltp <= 0 or leg.entry_price <= 0:
        return 0.0
    return (leg.entry_price - ltp) * leg.quantity


def _hedge_mtm(hedge, ltp: float) -> float:
    """Unrealized P&L of an open (long) hedge at the given LTP."""
    if ltp <= 0 or hedge.buy_price <= 0:
        return 0.0
    return (ltp - hedge.buy_price) * hedge.quantity


def _write_status(main_legs: list, hedge, expiry: str, message: str = ""):
    """Atomically write the live status snapshot consumed by the /stbt tab.

    Best-effort: a status-write failure must never take down the trading
    loop, so all exceptions are swallowed after a log line. LTPs come from
    the tick cache only (ws_ltp) — never REST — so this stays cheap.
    """
    global _last_status_write
    try:
        gross = sum(leg.realized_pnl for leg in main_legs)
        charges = sum(getattr(leg, "charges_total", 0.0) for leg in main_legs)
        mtm = 0.0

        leg_dicts = []
        for leg in main_legs:
            d = leg.to_dict()
            ltp = ws_ltp(leg.symbol)
            d["ltp"] = round(ltp, 2)
            d["mtm_pnl"] = 0.0
            if leg.state == leg.IN_SHORT:
                d["mtm_pnl"] = round(_leg_mtm(leg, ltp), 2)
                mtm += d["mtm_pnl"]
            leg_dicts.append(d)

        hedge_dict = None
        if hedge:
            gross += hedge.realized_pnl
            charges += getattr(hedge, "charges_total", 0.0)
            hedge_dict = hedge.to_dict()
            ltp = ws_ltp(hedge.symbol)
            hedge_dict["ltp"] = round(ltp, 2)
            hedge_dict["mtm_pnl"] = 0.0
            if hedge.state == hedge.OPEN:
                hedge_dict["mtm_pnl"] = round(_hedge_mtm(hedge, ltp), 2)
                mtm += hedge_dict["mtm_pnl"]

        net = gross - charges
        payload = {
            "version"      : VERSION,
            "strategy_id"  : STRATEGY_ID,
            "underlying"   : UNDERLYING,
            "phase"        : _PHASE,
            "message"      : message,
            "trade_date"   : _now_ist().date().isoformat(),
            "expiry"       : expiry,
            "quantity"     : main_legs[0].quantity if main_legs else 0,
            "main_legs"    : leg_dicts,
            "hedge"        : hedge_dict,
            "gross_pnl"    : round(gross, 2),
            "charges"      : round(charges, 2),
            "net_pnl"      : round(net, 2),
            "mtm_pnl"      : round(mtm, 2),
            "total_net_pnl": round(net + mtm, 2),
            "max_loss"     : MAX_LOSS,
            "last_update"  : _now_ist().isoformat(timespec="seconds"),
        }
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, STATUS_FILE)
        _last_status_write = time.time()
    except Exception as exc:
        log.warning("[STATUS] Could not write status file: %s", exc)


_last_status_write = 0.0
STATUS_REFRESH_SECS = 5


def _maybe_refresh_status(main_legs: list, hedge, expiry: str):
    """Throttled periodic status write so open-position MTM ticks live in
    the UI even when no state transition happens."""
    if time.time() - _last_status_write >= STATUS_REFRESH_SECS:
        _write_status(main_legs, hedge, expiry)


def _total_net(main_legs: list, hedge) -> float:
    """Session net P&L including open-position MTM. Used by the kill switch,
    so LTPs use live_ltp (REST fallback allowed — correctness over cost)."""
    gross = sum(leg.realized_pnl for leg in main_legs)
    charges = sum(getattr(leg, "charges_total", 0.0) for leg in main_legs)
    for leg in main_legs:
        if leg.state == leg.IN_SHORT:
            gross += _leg_mtm(leg, live_ltp(leg.symbol))
    if hedge:
        gross += hedge.realized_pnl
        charges += getattr(hedge, "charges_total", 0.0)
        if hedge.state == hedge.OPEN:
            gross += _hedge_mtm(hedge, live_ltp(hedge.symbol))
    return gross - charges


def _append_history(final_phase: str, main_legs: list, hedge, expiry: str):
    """Append one completed-session record to the per-config history file.

    Called only when a session truly ends (DONE / KILLED / same-day close),
    so realized figures equal the session totals. Best-effort like the
    status writer."""
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

        gross = sum(leg.realized_pnl for leg in main_legs)
        charges = sum(getattr(leg, "charges_total", 0.0) for leg in main_legs)
        legs = [
            {
                "symbol": leg.symbol,
                "opt_type": leg.opt_type,
                "state": leg.state,
                "cycles": leg.reentries + (1 if leg.entry_price > 0 else 0),
                "realized_pnl": round(leg.realized_pnl, 2),
                "charges": round(getattr(leg, "charges_total", 0.0), 2),
            }
            for leg in main_legs
        ]
        hedge_rec = None
        if hedge:
            gross += hedge.realized_pnl
            charges += getattr(hedge, "charges_total", 0.0)
            hedge_rec = {
                "symbol": hedge.symbol,
                "opt_type": hedge.opt_type,
                "state": hedge.state,
                "buy_price": round(hedge.buy_price, 2),
                "realized_pnl": round(hedge.realized_pnl, 2),
                "charges": round(getattr(hedge, "charges_total", 0.0), 2),
            }

        records.append(
            {
                "trade_date": _now_ist().date().isoformat(),
                "ended_at": _now_ist().isoformat(timespec="seconds"),
                "final_phase": final_phase,
                "expiry": expiry,
                "quantity": main_legs[0].quantity if main_legs else 0,
                "legs": legs,
                "hedge": hedge_rec,
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
        log.info("[HISTORY] Session record appended (%s, net=%.2f)",
                 final_phase, gross - charges)
    except Exception as exc:
        log.warning("[HISTORY] Could not append session record: %s", exc)


def _kill_session(main_legs: list, hedge, expiry: str, save_cb, total: float) -> None:
    """Max-loss kill switch: close everything at market and end the session."""
    log.critical("[KILL] Session net %.2f breached max loss ₹%.0f — "
                 "force-exiting ALL positions.", total, MAX_LOSS)
    _notify(f"KILL SWITCH: net ₹{total:,.2f} breached max loss ₹{MAX_LOSS:,.0f} — "
            f"closing all positions at market.")

    for leg in main_legs:
        if leg.is_short:
            leg.force_exit(save_cb)
    if hedge and not hedge.is_done:
        hedge.exit_at_market(save_cb)

    open_legs = [leg for leg in main_legs if leg.is_short]
    hedge_open = hedge is not None and not hedge.is_done

    try:
        ws_unsubscribe([leg.symbol for leg in main_legs])
    except Exception:
        pass

    _log_pnl_summary(main_legs, hedge, label="KILLED")
    _append_history("KILLED", main_legs, hedge, expiry)
    _set_phase("KILLED", main_legs, hedge, expiry,
               message=f"Max loss ₹{MAX_LOSS:,.0f} hit — session terminated")

    if not open_legs and not hedge_open:
        delete_state()
        _notify("Kill switch complete — all positions closed.")
    else:
        save_cb()
        log.critical("[KILL] *** Some exits FAILED — positions remain at broker. "
                     "MANUAL ACTION REQUIRED. ***")
        _notify("KILL SWITCH WARNING: some exits FAILED — check broker "
                "positions NOW (manual action required).")


def save_state(main_legs: list, hedge, expiry: str):
    payload = {
        "version"    : VERSION,
        "trade_date" : _now_ist().date().isoformat(),
        "expiry"     : expiry,
        "main_legs"  : [l.to_dict() for l in main_legs],
        "hedge"      : hedge.to_dict() if hedge else None,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, STATE_FILE)
    # Keep the UI snapshot in lockstep with every state persist.
    _write_status(main_legs, hedge, expiry)


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
#  TIMING UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(IST)


def wait_until(h: int, m: int, label: str = ""):
    """Sleep until HH:MM IST today. Wakes early if _shutdown is set."""
    now = _now_ist()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    secs = (target - now).total_seconds()
    if secs > 0:
        log.info("[TIME] Waiting %.0fs until %02d:%02d  %s", secs, h, m, label)
        _shutdown.wait(timeout=secs)


def now_past(h: int, m: int) -> bool:
    n = _now_ist()
    return n.hour > h or (n.hour == h and n.minute >= m)


# ─────────────────────────────────────────────────────────────────────────────
#  STOCKMOCK-STYLE 1-MINUTE CANDLE CLOSE TRACKING FOR RE-ENTRY
# ─────────────────────────────────────────────────────────────────────────────

_minute_candles: dict[str, dict] = {}
_closed_candles: dict[str, tuple[str, float]] = {}
_candle_lock = threading.Lock()


def _minute_key(dt: datetime | None = None) -> str:
    dt = dt or _now_ist()
    return dt.replace(second=0, microsecond=0).isoformat()


def _update_minute_candle(symbol: str, ltp: float):
    """Update per-symbol 1-minute candle close from WS/REST LTP.

    We only need close for StockMock-style re-entry. SL still runs on every
    latest LTP tick/quote.
    """
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

        # Minute changed: previous minute is now closed.
        _closed_candles[symbol] = (cur["minute"], float(cur["close"]))
        _minute_candles[symbol] = {"minute": minute, "close": float(ltp)}


def _get_last_closed_candle(symbol: str) -> tuple[str, float] | None:
    with _candle_lock:
        return _closed_candles.get(symbol)


# ── TRUE 1-minute candle close via OpenAlgo history API ──────────────────────
# StockMock/Quantiply RE-ENTRY evaluates on the official 1-min candle CLOSE, not
# on whatever the last websocket tick happened to be. For slow-ticking BFO
# options the WS-derived close above is unreliable, so the re-entry decision is
# driven by the broker's real 1-min candle fetched here.

_hist_candle_cache: dict[str, tuple[str, str, float]] = {}  # symbol -> (poll_min, candle_min, close)
_hist_candle_lock  = threading.Lock()


def _parse_candle_ts(value) -> datetime | None:
    """Parse a history() row timestamp (epoch s/ms, ISO string, or pandas
    Timestamp / datetime) into an IST-aware datetime. Returns None if unparseable."""
    if value is None:
        return None
    # pandas Timestamp (duck-typed to avoid importing pandas here)
    if hasattr(value, "to_pydatetime"):
        try:
            dt = value.to_pydatetime()
            return dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except Exception:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    # numeric epoch (seconds or milliseconds)
    try:
        num = float(value)
        if num > 1e12:        # milliseconds
            num /= 1000.0
        if num > 1e8:         # plausible epoch seconds
            return datetime.fromtimestamp(num, tz=IST)
    except (TypeError, ValueError):
        pass
    # ISO / "YYYY-MM-DD HH:MM:SS" string
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _last_closed_row(df) -> tuple[str, float] | None:
    """From a history() DataFrame, return (candle_minute_iso, close) for the
    latest FULLY-CLOSED 1-min candle, excluding the still-forming current minute."""
    if df is None or getattr(df, "empty", True):
        return None
    cur_minute = _now_ist().replace(second=0, microsecond=0)
    try:
        if "timestamp" in df.columns:
            rows = zip(df["timestamp"].tolist(), df["close"].tolist())
        else:
            rows = zip(df.index.tolist(), df["close"].tolist())
    except Exception as exc:
        log.warning("[REENTRY-1M] Could not read history rows: %s", exc)
        return None

    best: tuple[datetime, float] | None = None
    for ts, close in rows:
        dt = _parse_candle_ts(ts)
        if dt is None:
            continue
        dt_min = dt.astimezone(IST).replace(second=0, microsecond=0)
        if dt_min >= cur_minute:
            continue          # skip in-progress / future candle
        if best is None or dt_min > best[0]:
            try:
                best = (dt_min, float(close))
            except (TypeError, ValueError):
                continue
    if best is None:
        return None
    return best[0].isoformat(), best[1]


def get_last_closed_1m_candle(symbol: str) -> tuple[str, float] | None:
    """Latest CLOSED 1-min candle (candle_minute_iso, close) from the history API.

    Cached per current minute so we hit history() at most once per symbol per
    minute. Returns None on empty/error so the caller can fall back to the
    WS-derived candle.
    """
    cur_min = _minute_key()
    with _hist_candle_lock:
        c = _hist_candle_cache.get(symbol)
        if c and c[0] == cur_min:
            return (c[1], c[2]) if c[1] else None

    today = _now_ist().date().isoformat()
    try:
        df = client.history(symbol=symbol, exchange=OPT_EXCHANGE,
                            interval=HISTORY_INTERVAL, start_date=today, end_date=today)
        result = _last_closed_row(df)
    except Exception as exc:
        log.warning("[REENTRY-1M] history() failed for %s: %s — falling back to WS candle.",
                    symbol, exc)
        result = None

    with _hist_candle_lock:
        _hist_candle_cache[symbol] = (cur_min,
                                      result[0] if result else "",
                                      result[1] if result else 0.0)
    if result:
        log.info("[REENTRY-1M] %s last-closed 1m candle %s close=%.2f",
                 symbol, result[0], result[1])
    return result


def _day2_reentry_time_allowed(trade_date: str | None) -> bool:
    """StockMock-style next-day condition check gate for positional trades."""
    if not trade_date:
        return True

    today = _now_ist().date().isoformat()
    if str(trade_date) == today:
        return True

    if not ALLOW_DAY2_REENTRY:
        return False

    start = _now_ist().replace(
        hour=CHECK_NEXT_DAY_AFTER_H,
        minute=CHECK_NEXT_DAY_AFTER_M,
        second=0,
        microsecond=0,
    )
    return _now_ist() >= start


def _is_trading_day() -> bool:
    today = _now_ist()
    if today.weekday() >= 5:
        log.warning("Today is %s — market closed.", today.strftime("%A"))
        return False
    return True


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
    exchange_txn = turnover * BSE_OPTION_TXN_RATE
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
#  MAIN LEG  — state machine for one ITM-2 short option
# ─────────────────────────────────────────────────────────────────────────────

class MainLeg:
    """
    WATCHING ──(5% drop)──────► IN_SHORT
    IN_SHORT ──(LTP ≥ SL)─────► SL_HIT
    SL_HIT   ──(LTP ≤ entry)──► IN_SHORT  (one re-entry)
    SL_HIT   ──(reentries↑)───► DONE
    IN_SHORT ──(force exit)───► DONE
    """

    WATCHING = "WATCHING"
    IN_SHORT = "IN_SHORT"
    SL_HIT   = "SL_HIT"
    DONE     = "DONE"

    def __init__(self, symbol: str, opt_type: str, quantity: int = 0):
        self.symbol       = symbol
        self.opt_type     = opt_type
        self.quantity     = quantity
        self.state        = self.WATCHING
        self.ref_premium  = 0.0
        self.entry_price  = 0.0
        self.sl_price     = 0.0
        self.reentries    = 0
        self.last_reentry_candle = ""  # prevents duplicate candle-close re-entry checks
        self.realized_pnl = 0.0   # cumulative across all cycles for this leg
        self.charges_total = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "MainLeg":
        leg               = cls(d["symbol"], d["opt_type"], d.get("quantity", 0))
        leg.state         = d["state"]
        leg.ref_premium   = d["ref_premium"]
        leg.entry_price   = d["entry_price"]
        leg.sl_price      = d["sl_price"]
        leg.reentries     = d["reentries"]
        leg.last_reentry_candle = d.get("last_reentry_candle", "")
        leg.realized_pnl  = d.get("realized_pnl", 0.0)
        leg.charges_total = d.get("charges_total", 0.0)
        return leg

    def to_dict(self) -> dict:
        return {
            "symbol"      : self.symbol,
            "opt_type"    : self.opt_type,
            "quantity"    : self.quantity,
            "state"       : self.state,
            "ref_premium" : self.ref_premium,
            "entry_price" : self.entry_price,
            "sl_price"    : self.sl_price,
            "reentries"   : self.reentries,
            "last_reentry_candle": self.last_reentry_candle,
            "realized_pnl": self.realized_pnl,
            "charges_total": self.charges_total,
        }

    def _log_cycle_pnl(self, exit_price: float, reason: str):
        """Short PnL = (entry - exit) × qty. Updates self.realized_pnl."""
        cycle_num = self.reentries + 1     # 1 = first sell, 2 = re-entry
        cycle_pnl = (self.entry_price - exit_price) * self.quantity
        charges = _option_charges(
            buy_price=exit_price,
            sell_price=self.entry_price,
            quantity=self.quantity,
        )
        self.realized_pnl += cycle_pnl
        self.charges_total += charges["total"]
        log.info("[%s CYCLE %d %s] entry=%.2f  exit=%.2f  qty=%d  "
                 "Gross=%s%.2f  Charges=%.2f  Net=%s%.2f  "
                 "(leg gross=%s%.2f  charges=%.2f)",
                 self.opt_type, cycle_num, reason,
                 self.entry_price, exit_price, self.quantity,
                 "+" if cycle_pnl >= 0 else "", cycle_pnl,
                 charges["total"],
                 "+" if cycle_pnl - charges["total"] >= 0 else "",
                 cycle_pnl - charges["total"],
                 "+" if self.realized_pnl >= 0 else "", self.realized_pnl,
                 self.charges_total)

    def on_tick(self, ltp: float, save_cb, allow_reentry_on_ltp: bool = True) -> bool:
        """Evaluate latest LTP.

        Entry and SL can run on live LTP. Re-entry can be disabled here when
        REENTRY_METHOD='CANDLE_CLOSE' so it follows StockMock/Quantiply timing.
        """
        if self.state == self.WATCHING:
            return self._try_entry(ltp, save_cb)
        if self.state == self.IN_SHORT:
            return self._check_sl(ltp, save_cb)
        if self.state == self.SL_HIT and allow_reentry_on_ltp:
            return self._try_reentry(ltp, save_cb, source="LTP")
        return False

    def on_candle_close(self, candle_minute: str, close_price: float, save_cb) -> bool:
        """StockMock/Quantiply-style re-entry check on 1-minute candle close."""
        if self.state != self.SL_HIT:
            return False
        if not candle_minute or candle_minute == self.last_reentry_candle:
            return False
        self.last_reentry_candle = candle_minute
        return self._try_reentry(close_price, save_cb, source="CANDLE_CLOSE")

    def _try_entry(self, ltp: float, save_cb) -> bool:
        if self.ref_premium <= 0 or ltp <= 0:
            return False
        drop = (self.ref_premium - ltp) / self.ref_premium * 100
        if drop < ENTRY_DROP_PCT:
            return False
        log.info("[%s] Entry trigger: %.2f%% drop  ref=%.2f  ltp=%.2f",
                 self.opt_type, drop, self.ref_premium, ltp)
        return self._sell(ltp, save_cb)

    def _try_reentry(self, ltp: float, save_cb, source: str = "LTP") -> bool:
        if self.reentries >= MAX_REENTRIES:
            self.state = self.DONE
            log.info("[%s] Re-entry limit reached → DONE.", self.opt_type)
            save_cb()
            return False
        if ltp > self.entry_price:
            return False
        log.info("[%s] Re-entry trigger (%s): price=%.2f ≤ entry=%.2f",
                 self.opt_type, source, ltp, self.entry_price)
        if self._sell(ltp, save_cb):
            self.reentries += 1
            save_cb()
            return True
        return False

    def _sell(self, ref_ltp: float, save_cb) -> bool:
        oid = _place_order(self.symbol, "SELL", self.quantity, reason=f"{self.opt_type}_ENTRY")
        if not oid:
            log.error("[%s] SELL failed — NOT entering position.", self.opt_type)
            return False
        filled           = _get_fill_price(oid, self.symbol, ref_ltp)
        self.entry_price = filled
        self.sl_price    = round(filled * (1 + SL_PCT / 100), 2)
        self.state       = self.IN_SHORT
        log.info("[%s] SHORT entered  entry=%.2f  sl=%.2f  qty=%d",
                 self.opt_type, filled, self.sl_price, self.quantity)
        _notify(f"SHORT {self.opt_type} {self.symbol} @ ₹{filled:.2f} "
                f"(SL ₹{self.sl_price:.2f}, qty {self.quantity})")
        save_cb()
        return True

    def _check_sl(self, ltp: float, save_cb) -> bool:
        if ltp < self.sl_price:
            return False
        log.warning("[%s] SL HIT  ltp=%.2f ≥ sl=%.2f",
                    self.opt_type, ltp, self.sl_price)
        oid = _place_order(self.symbol, "BUY", self.quantity, reason=f"{self.opt_type}_SL")
        if not oid:
            log.error("[%s] SL BUY failed — retrying next tick.", self.opt_type)
            return False
        exit_price = _get_fill_price(oid, self.symbol, ltp)
        self._log_cycle_pnl(exit_price, "SL")
        self.state = self.SL_HIT
        log.info("[%s] Short covered.  Re-entries remaining: %d",
                 self.opt_type, MAX_REENTRIES - self.reentries)
        _notify(f"SL HIT {self.opt_type} {self.symbol}: covered @ ₹{exit_price:.2f} "
                f"(re-entries left: {MAX_REENTRIES - self.reentries})")
        save_cb()
        return True

    def force_exit(self, save_cb) -> bool:
        """Market BUY to close short with critical-level retries."""
        if self.state != self.IN_SHORT:
            self.state = self.DONE
            save_cb()
            return True
        log.info("[%s] Force-exit at %02d:%02d.", self.opt_type, EXIT_H, EXIT_M)
        oid = _place_critical_order(
            self.symbol, "BUY", self.quantity,
            label=f"{self.opt_type}-FORCE_EXIT",
        )
        if oid:
            exit_price = _get_fill_price(
                oid, self.symbol, live_ltp(self.symbol) or self.entry_price)
            self._log_cycle_pnl(exit_price, "FORCE")
            self.state = self.DONE
            _notify(f"Force-exited {self.opt_type} {self.symbol} @ ₹{exit_price:.2f}")
            save_cb()
            return True
        save_cb()   # keep IN_SHORT so operator knows position is still live
        return False

    @property
    def is_done(self)  -> bool: return self.state == self.DONE

    @property
    def is_short(self) -> bool: return self.state == self.IN_SHORT


# ─────────────────────────────────────────────────────────────────────────────
#  HEDGE LEG  — cheap OTM bought 03:26 PM Day-1, sold 09:16 AM Day-2
# ─────────────────────────────────────────────────────────────────────────────

class HedgeLeg:
    OPEN = "OPEN"
    DONE = "DONE"

    def __init__(self, symbol: str, opt_type: str, buy_price: float = 0.0,
                 quantity: int = 0):
        self.symbol       = symbol
        self.opt_type     = opt_type
        self.quantity     = quantity
        self.state        = self.OPEN
        self.buy_price    = buy_price
        self.realized_pnl = 0.0
        self.charges_total = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "HedgeLeg":
        h              = cls(d["symbol"], d["opt_type"], d.get("buy_price", 0.0),
                             d.get("quantity", 0))
        h.state        = d["state"]
        h.realized_pnl = d.get("realized_pnl", 0.0)
        h.charges_total = d.get("charges_total", 0.0)
        return h

    def to_dict(self) -> dict:
        return {
            "symbol"      : self.symbol,
            "opt_type"    : self.opt_type,
            "quantity"    : self.quantity,
            "state"       : self.state,
            "buy_price"   : self.buy_price,
            "realized_pnl": self.realized_pnl,
            "charges_total": self.charges_total,
        }

    def exit_at_market(self, save_cb) -> bool:
        if self.state != self.OPEN:
            return True
        log.info("[HEDGE-%s] Selling at market  symbol=%s  bought@%.2f",
                 self.opt_type, self.symbol, self.buy_price)
        oid = _place_critical_order(
            self.symbol, "SELL", self.quantity,
            label=f"HEDGE-{self.opt_type}",
        )
        if oid:
            # Hedge is LONG → PnL = (sell - buy) × qty
            exit_price = _get_fill_price(
                oid, self.symbol, live_ltp(self.symbol) or self.buy_price)
            cycle_pnl  = (exit_price - self.buy_price) * self.quantity
            charges = _option_charges(
                buy_price=self.buy_price,
                sell_price=exit_price,
                quantity=self.quantity,
            )
            self.realized_pnl = cycle_pnl
            self.charges_total = charges["total"]
            log.info("[HEDGE-%s CYCLE] buy=%.2f  sell=%.2f  qty=%d  "
                     "Gross=%s%.2f  Charges=%.2f  Net=%s%.2f",
                     self.opt_type, self.buy_price, exit_price, self.quantity,
                     "+" if cycle_pnl >= 0 else "", cycle_pnl,
                     charges["total"],
                     "+" if cycle_pnl - charges["total"] >= 0 else "",
                     cycle_pnl - charges["total"])
            self.state = self.DONE
            _notify(f"Hedge sold: {self.symbol} @ ₹{exit_price:.2f}")
            save_cb()
            return True
        save_cb()
        return False

    @property
    def is_done(self) -> bool: return self.state == self.DONE


# ─────────────────────────────────────────────────────────────────────────────
#  PNL SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def _log_pnl_summary(main_legs: list, hedge, label: str):
    """Aggregate realized PnL and estimated charges across all legs + hedge."""
    parts = []
    gross_total = 0.0
    charges_total = 0.0
    for leg in main_legs:
        charges = getattr(leg, "charges_total", 0.0)
        parts.append(f"{leg.opt_type} gross={leg.realized_pnl:+.2f} charges={charges:.2f}")
        gross_total += leg.realized_pnl
        charges_total += charges
    if hedge:
        charges = getattr(hedge, "charges_total", 0.0)
        parts.append(f"HEDGE-{hedge.opt_type} gross={hedge.realized_pnl:+.2f} charges={charges:.2f}")
        gross_total += hedge.realized_pnl
        charges_total += charges
    net_total = gross_total - charges_total
    log.info("[%s PnL] %s  ->  Gross=%+.2f  Charges=%.2f  Net=%+.2f",
             label, "  ".join(parts), gross_total, charges_total, net_total)


# ─────────────────────────────────────────────────────────────────────────────
#  DAY-1
# ─────────────────────────────────────────────────────────────────────────────

def run_day1():
    """
    11:00 AM  REST snapshot → WS subscribe → tick-driven entry/SL loop
    03:26 PM  hedge naked leg (if exactly one open)
    03:29 PM  WS disconnect + atomic state persist
    """

    strategy_start = time.monotonic()

    _set_phase("DAY1_WAIT", message=f"Waiting for {ENTRY_H:02d}:{ENTRY_M:02d} entry window")
    wait_until(ENTRY_H, ENTRY_M, "Day-1 entry window")
    if _shutdown.is_set():
        return

    log.info("=" * 60)
    log.info("DAY-1  %02d:%02d  %s STBT Session Starting", ENTRY_H, ENTRY_M, UNDERLYING)
    log.info("=" * 60)

    expiry = get_next_expiry()
    log.info("Trading expiry: %s", expiry)

    chain = get_chain(expiry)

    lot_size = get_lot_size(chain)
    quantity = lot_size * LOT_MULTIPLIER
    log.info("Lot size=%d  multiplier=%d  quantity=%d",
             lot_size, LOT_MULTIPLIER, quantity)

    log.info("SENSEX LTP=%.2f  ATM=%s",
             chain.get("underlying_ltp"), chain.get("atm_strike"))

    ce_sym, ce_ref, pe_sym, pe_ref = find_itm2(chain)
    log.info("ITM-2 CE: %-30s ref=%.2f", ce_sym, ce_ref)
    log.info("ITM-2 PE: %-30s ref=%.2f", pe_sym, pe_ref)

    ce_leg = MainLeg(ce_sym, "CE", quantity)
    pe_leg = MainLeg(pe_sym, "PE", quantity)
    ce_leg.ref_premium = ce_ref
    pe_leg.ref_premium = pe_ref

    main_legs              = [ce_leg, pe_leg]
    hedge: HedgeLeg | None = None

    def _save():
        save_state(main_legs, hedge, expiry)

    _set_phase("DAY1_LIVE", main_legs, hedge, expiry,
               message=f"Monitoring for {ENTRY_DROP_PCT:.0f}% entry trigger")

    ws_subscribe([ce_sym, pe_sym])
    _wait_for_tick(timeout=30)
    log.info("[WS] Feed live. Monitoring for %.0f%% entry trigger...",
             ENTRY_DROP_PCT)

    _last_status_log = 0.0

    while not _shutdown.is_set():

        if time.monotonic() - strategy_start > STRATEGY_TIMEOUT_S:
            log.error("Strategy wall-clock timeout (%ds) — aborting.", STRATEGY_TIMEOUT_S)
            _shutdown.set()
            break

        if now_past(HEDGE_H, HEDGE_M):
            log.info("03:26 PM reached — exiting monitor loop.")
            break

        if all(leg.is_done for leg in main_legs):
            log.info("Both legs DONE before hedge window.")
            break

        ws_reconnect_if_stale([ce_sym, pe_sym])

        _wait_for_tick(timeout=1)

        for leg in main_legs:
            if not leg.is_done and not _shutdown.is_set():
                ltp = live_ltp(leg.symbol)
                if ltp > 0:
                    leg.on_tick(
                        ltp,
                        _save,
                        allow_reentry_on_ltp=(REENTRY_METHOD.upper() == "LTP"),
                    )

                    if (REENTRY_METHOD.upper() == "CANDLE_CLOSE"
                            and leg.state == MainLeg.SL_HIT
                            and leg.reentries < MAX_REENTRIES):
                        closed = (get_last_closed_1m_candle(leg.symbol)
                                  if REENTRY_CANDLE_SOURCE.upper() == "HISTORY" else None)
                        if closed is None:
                            closed = _get_last_closed_candle(leg.symbol)   # WS fallback
                        if closed:
                            candle_minute, candle_close = closed
                            leg.on_candle_close(candle_minute, candle_close, _save)

        _maybe_refresh_status(main_legs, hedge, expiry)

        if MAX_LOSS > 0 and any(leg.is_short for leg in main_legs):
            total = _total_net(main_legs, hedge)
            if total <= -MAX_LOSS:
                _kill_session(main_legs, hedge, expiry, _save, total)
                return

        now_ts = time.time()
        if now_ts - _last_status_log >= 120:
            log.info("CE %s=%.2f [%s]  |  PE %s=%.2f [%s]",
                     ce_sym, live_ltp(ce_sym), ce_leg.state,
                     pe_sym, live_ltp(pe_sym), pe_leg.state)
            _last_status_log = now_ts

    # ── 03:26 PM — overnight hedge ────────────────────────────────────────
    if not _shutdown.is_set():
        open_shorts = [leg for leg in main_legs if leg.is_short]

        if len(open_shorts) == 1:
            naked = open_shorts[0]
            log.info("-" * 60)
            log.info("03:26 PM  %s leg is NAKED — placing overnight hedge.",
                     naked.opt_type)
            log.info("-" * 60)

            hedge_chain  = get_chain(expiry)
            h_sym, h_ltp = find_hedge_strike(hedge_chain, naked.opt_type)

            if h_sym:
                log.info("[HEDGE] BUY %s  ltp≈₹%.2f  (target ₹%.0f)",
                         h_sym, h_ltp, HEDGE_TARGET_PREMIUM)
                oid = _place_order(h_sym, "BUY", quantity, reason=f"HEDGE_{naked.opt_type}_BUY")
                if oid:
                    fill_price = _get_fill_price(oid, h_sym, h_ltp or 0.0)
                    hedge = HedgeLeg(h_sym, naked.opt_type,
                                     buy_price=fill_price, quantity=quantity)
                    log.info("[HEDGE-%s] Placed: %s  paid≈₹%.2f",
                             naked.opt_type, h_sym, fill_price)
                    _save()
                    _set_phase("HEDGED", main_legs, hedge, expiry,
                               message=f"Overnight hedge {h_sym} placed")
                    _notify(f"Overnight hedge bought: {h_sym} @ ₹{fill_price:.2f} "
                            f"(naked {naked.opt_type} covered)")
                else:
                    log.error("[HEDGE] Order failed — overnight position is UNHEDGED.")
            else:
                log.warning("[HEDGE] No suitable strike found — UNHEDGED overnight.")

        elif len(open_shorts) == 2:
            log.info("03:26 PM  Both CE + PE open (strangle). No hedge needed.")
        else:
            log.info("03:26 PM  No open shorts. No hedge needed.")

    # ── 03:29 PM — disconnect WS + persist state ──────────────────────────
    wait_until(WS_CLOSE_H, WS_CLOSE_M, "WS close")
    ws_unsubscribe([ce_sym, pe_sym])

    # Only persist state if there is something for Day-2 to actually do.
    # Saving "empty" state (no shorts, no hedge) is worse than no state:
    # it misroutes the next run into Day-2 exit mode instead of Day-1.
    has_open_shorts = any(leg.is_short for leg in main_legs)
    has_hedge       = hedge is not None and not hedge.is_done
    if has_open_shorts or has_hedge:
        _save()
        log.info("[STATE] Persisted for Day-2 (shorts=%d  hedge=%s)",
                 sum(1 for l in main_legs if l.is_short),
                 hedge.symbol if hedge else "none")
    else:
        delete_state()   # clean up any leftover file so Day-1 runs tomorrow
        log.info("[STATE] No open positions — skipping save. "
                 "Next run will go to Day-1.")
        # Session ended flat on Day-1 itself — record it now (no Day-2 run).
        _append_history("DONE", main_legs, hedge, expiry)
        _notify(f"Day-1 closed flat — session over. "
                f"Net P&L: ₹{_total_net(main_legs, hedge):,.2f}")

    _log_pnl_summary(main_legs, hedge, label="DAY-1 REALIZED")

    _set_phase("DAY1_DONE", main_legs, hedge, expiry,
               message="Day-1 complete — Day-2 exit runs next session")
    log.info("=" * 60)
    log.info("DAY-1  Complete. State persisted to %s", STATE_FILE)
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
#  DAY-2
# ─────────────────────────────────────────────────────────────────────────────

def run_day2():
    """
    09:16 AM  sell hedge at market → WS subscribe main legs → SL monitoring
    10:30 AM  force-exit all remaining shorts → cleanup
    """

    state = load_state()
    if not state:
        log.info("[DAY-2] No state file — nothing to exit.")
        return

    main_legs = [MainLeg.from_dict(d) for d in state.get("main_legs", [])]
    hedge     = HedgeLeg.from_dict(state["hedge"]) if state.get("hedge") else None
    expiry    = state["expiry"]
    trade_date = state.get("trade_date")
    active    = [leg for leg in main_legs if not leg.is_done]

    if not active and (hedge is None or hedge.is_done):
        log.info("[DAY-2] All positions already closed.")
        delete_state()
        return

    def _save():
        save_state(main_legs, hedge, expiry)

    _set_phase("DAY2", main_legs, hedge, expiry,
               message=f"Waiting for {DAY2_H:02d}:{DAY2_M:02d} Day-2 open")
    wait_until(DAY2_H, DAY2_M, "Day-2 start (skip first-minute spike)")
    if _shutdown.is_set():
        return

    log.info("=" * 60)
    log.info("DAY-2  %02d:%02d  %s STBT Exit Session", DAY2_H, DAY2_M, UNDERLYING)
    log.info("=" * 60)

    # Step 1 — sell hedge at market (REST, no WS needed yet)
    if hedge and not hedge.is_done:
        hedge.exit_at_market(_save)

    if not active:
        log.info("[DAY-2] No main legs to monitor — session complete.")
        if hedge is None or hedge.is_done:
            delete_state()
        else:
            log.critical("[DAY-2] *** Hedge %s still OPEN (sell failed). "
                         "State file PRESERVED — MANUAL ACTION REQUIRED. ***",
                         hedge.symbol)
        return

    # Step 2 — WS subscribe for main legs
    main_symbols = [leg.symbol for leg in active]
    ws_subscribe(main_symbols)
    _wait_for_tick(timeout=30)

    for leg in active:
        log.info("[%s] Loaded state=%s  entry=%.2f  sl=%.2f  re-entries_used=%d",
                 leg.opt_type, leg.state, leg.entry_price, leg.sl_price, leg.reentries)

    log.info("[DAY-2] Monitoring until %02d:%02d forced exit...", EXIT_H, EXIT_M)

    _last_status_log = 0.0

    # Step 3 — SL monitoring loop
    while not _shutdown.is_set():

        if now_past(EXIT_H, EXIT_M):
            log.info("[DAY-2] %02d:%02d — proceeding to forced exit.", EXIT_H, EXIT_M)
            break

        if all(leg.is_done for leg in active):
            log.info("[DAY-2] All legs closed before forced exit.")
            break

        ws_reconnect_if_stale(main_symbols)

        _wait_for_tick(timeout=1)

        for leg in active:
            if not leg.is_done and not _shutdown.is_set():
                ltp = live_ltp(leg.symbol)
                if ltp > 0:
                    # SL stays live/tick-based. Re-entry is handled separately
                    # by candle close when REENTRY_METHOD='CANDLE_CLOSE'.
                    leg.on_tick(
                        ltp,
                        _save,
                        allow_reentry_on_ltp=(REENTRY_METHOD.upper() == "LTP"),
                    )

                    if (REENTRY_METHOD.upper() == "CANDLE_CLOSE"
                            and leg.state == MainLeg.SL_HIT
                            and leg.reentries < MAX_REENTRIES
                            and _day2_reentry_time_allowed(trade_date)):
                        closed = (get_last_closed_1m_candle(leg.symbol)
                                  if REENTRY_CANDLE_SOURCE.upper() == "HISTORY" else None)
                        if closed is None:
                            closed = _get_last_closed_candle(leg.symbol)   # WS fallback
                        if closed:
                            candle_minute, candle_close = closed
                            leg.on_candle_close(candle_minute, candle_close, _save)

        _maybe_refresh_status(main_legs, hedge, expiry)

        if MAX_LOSS > 0 and any(leg.is_short for leg in main_legs):
            total = _total_net(main_legs, hedge)
            if total <= -MAX_LOSS:
                _kill_session(main_legs, hedge, expiry, _save, total)
                return

        now_ts = time.time()
        if now_ts - _last_status_log >= 120:
            log.info("  ".join(
                f"{leg.opt_type} {live_ltp(leg.symbol):.2f}[{leg.state}]"
                for leg in active
            ))
            _last_status_log = now_ts

    # Step 4 — force-exit remaining open shorts
    log.info("-" * 60)
    for leg in active:
        leg.force_exit(_save)

    ws_unsubscribe(main_symbols)

    _log_pnl_summary(main_legs, hedge, label="FINAL TOTAL")

    open_legs = [leg for leg in main_legs if not leg.is_done]
    hedge_open = hedge is not None and not hedge.is_done
    if not open_legs and not hedge_open:
        delete_state()
    else:
        log.critical(
            "[DAY-2] *** State file PRESERVED — open positions remain at broker. "
            "MANUAL ACTION REQUIRED.  shorts_open=%s  hedge_open=%s ***",
            [f"{l.opt_type}:{l.symbol}" for l in open_legs] or "none",
            hedge.symbol if hedge_open else "none",
        )
        _notify("CRITICAL: Day-2 exits incomplete — open positions remain at the "
                "broker. MANUAL ACTION REQUIRED.")
    session_net = _total_net(main_legs, hedge)
    _append_history("DONE", main_legs, hedge, expiry)
    _set_phase("DONE", main_legs, hedge, expiry, message="Session finished")
    _notify(f"Session finished. Net P&L: ₹{session_net:,.2f}")
    log.info("=" * 60)
    log.info("DAY-2  Complete. %s STBT session finished.", UNDERLYING)
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _state_has_work(state: dict | None) -> bool:
    """True only if the loaded state has real positions to act on.

    Protects against garbage state files from earlier crashes / bad expiries
    where legs were saved in WATCHING with entry_price=0.
    """
    if not state:
        return False
    has_short = any(leg.get("state") == MainLeg.IN_SHORT
                    for leg in state.get("main_legs", []))
    has_reentry_pending = any(
        leg.get("state") == MainLeg.SL_HIT
        and int(leg.get("reentries", 0)) < MAX_REENTRIES
        for leg in state.get("main_legs", [])
    )
    hedge     = state.get("hedge")
    has_hedge = bool(hedge and hedge.get("state") == HedgeLeg.OPEN)
    return has_short or has_reentry_pending or has_hedge


def main():
    if not API_KEY:
        log.error("OPENALGO_API_KEY not injected — generate an API key at /apikey "
                  "and start this strategy through the /stbt tab. Aborting.")
        return
    if not HOST:
        log.error("OPENALGO_HOST / HOST_SERVER not set — aborting.")
        return

    log.info("=" * 60)
    log.info("%s STBT engine v%s  |  id=%s  |  lots=%d  |  hedge≈₹%.0f  |  host=%s",
             UNDERLYING, VERSION, STRATEGY_ID, LOT_MULTIPLIER, HEDGE_TARGET_PREMIUM, HOST)
    log.info("=" * 60)
    _set_phase("STARTING", message="Engine starting")

    try:
        expiry_day = is_expiry_day()
        state      = load_state()

        # Discard empty/junk state so we don't misroute to Day-2 with no work
        if state and not _state_has_work(state):
            log.warning("[STATE] Loaded state has no open shorts or hedge — "
                        "discarding as junk and running Day-1 fresh.")
            delete_state()
            state = None

        log.info("Expiry day : %s", "YES — no new positions today" if expiry_day else "No")
        log.info("Saved state: %s", state.get("trade_date") if state else "None")

        # Expiry day → exit prior positions only
        if expiry_day:
            if state:
                log.info("Expiry day → running Day-2 exit only.")
                run_day2()
            else:
                log.info("Expiry day, no open state. Nothing to do.")
                _set_phase("EXPIRY_DAY", message="Expiry day — no new positions")
            return

        today_iso = _now_ist().date().isoformat()

        # Prior-day state → exit first, then fresh Day-1 today
        if state and state.get("trade_date") != today_iso:
            log.info("Prior-day state found → Day-2 exit first.")
            run_day2()
            state = None   # state deleted inside run_day2

        # Today's state (mid-day restart) → resume Day-2 monitoring
        if state and state.get("trade_date") == today_iso:
            log.info("Today's state found → resuming Day-2 monitoring.")
            run_day2()
        else:
            # Fresh start → Day-1; Day-2 runs tomorrow morning after restart
            run_day1()

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting cleanly.")
    except Exception as exc:
        log.exception("Fatal unhandled exception.")
        _set_phase("ERROR", message=f"Engine crashed: {exc}")
        _notify(f"ENGINE ERROR: {exc} — check the strategy log. If positions are "
                f"open, verify them at the broker.")


if __name__ == "__main__":
    main()
