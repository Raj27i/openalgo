"""
BTST Flip Engine Logic Test Suite

Tests the FlipLeg paper-short → real-buy state machine, the VIX gate, the
long-side stop-loss, the entry-day filters, and the smart-flatten exit
invariants for strategies/stbt/btst_engine.py WITHOUT a live broker.

The openalgo SDK client and every order/quote call is stubbed; the engine
module is imported with a throwaway STRATEGY_ID + config file.

Usage:
    uv run pytest test/test_btst_flip_logic.py -v
"""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STBT_DIR = REPO_ROOT / "strategies" / "stbt"
TEST_ID = "btst_test_logic"


@pytest.fixture(scope="module")
def engine():
    """Import btst_engine with stubbed openalgo + throwaway config."""
    config_path = STBT_DIR / f"{TEST_ID}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "underlying": "SENSEX",
                "strategy_type": "btst",
                "drop_pct": 5.0,
                "vsl_pct": 20.0,
                "real_sl_pct": 30.0,
                "vix_max": 18.0,
                "dte_min": 1,
                "dte_max": 3,
                "entry_weekdays": ["mon", "tue", "wed", "thu"],
                "telegram_alerts": False,
            }
        ),
        encoding="utf-8",
    )

    os.environ["STRATEGY_ID"] = TEST_ID
    fake_openalgo = MagicMock()
    fake_openalgo.api.return_value = MagicMock()
    saved = sys.modules.get("openalgo")
    sys.modules["openalgo"] = fake_openalgo
    sys.modules.pop("strategies.stbt.btst_engine", None)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    mod = importlib.import_module("strategies.stbt.btst_engine")

    yield mod

    # Cleanup runtime artifacts + restore module registry.
    if saved is not None:
        sys.modules["openalgo"] = saved
    else:
        sys.modules.pop("openalgo", None)
    for suffix in ("_config.json", "_state.json", "_status.json", "_history.json",
                   "_journal.json", "_order_intent.json"):
        p = STBT_DIR / f"{TEST_ID}{suffix}"
        p.unlink(missing_ok=True)
        Path(str(p) + ".tmp").unlink(missing_ok=True)


@pytest.fixture()
def leg(engine, monkeypatch):
    """Fresh FlipLeg with quiet notify/journal and a permissive VIX."""
    monkeypatch.setattr(engine, "_notify", lambda *_a, **_k: None)
    monkeypatch.setattr(engine, "_journal_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr(engine, "vix_ok", lambda: True)
    l = engine.FlipLeg("SENSEX25JUL80000CE", "CE", 20)
    l.ref_premium = 1000.0
    return l


def _noop_save():
    pass


# ---------------------------------------------------------------------------
# Paper-short arming
# ---------------------------------------------------------------------------


class TestPaperShort:
    def test_no_arm_above_trigger(self, engine, leg):
        # 4% drop — below the 5% trigger
        assert leg.on_signal_candle("m1", 960.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.WATCHING

    def test_arms_at_trigger(self, engine, leg):
        # exactly 5% drop → paper short at that close, v_sl = close × 1.20
        assert leg.on_signal_candle("m1", 950.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.PAPER_SHORT
        assert leg.v_entry == 950.0
        assert leg.v_sl == pytest.approx(1140.0)

    def test_candle_dedupe(self, engine, leg):
        # the same candle minute must never be evaluated twice
        assert leg.on_signal_candle("m1", 950.0, _noop_save) is True
        leg.state = engine.FlipLeg.WATCHING  # pretend nothing happened
        assert leg.on_signal_candle("m1", 100.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.WATCHING

    def test_zero_ref_never_arms(self, engine, leg):
        leg.ref_premium = 0.0
        assert leg.on_signal_candle("m1", 500.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.WATCHING


# ---------------------------------------------------------------------------
# Flip buy (virtual SL stop-out → real long)
# ---------------------------------------------------------------------------


def _arm(leg):
    leg.on_signal_candle("m-arm", 950.0, _noop_save)
    assert leg.state == leg.PAPER_SHORT


class TestFlipBuy:
    def test_buy_on_virtual_sl(self, engine, leg, monkeypatch):
        _arm(leg)
        monkeypatch.setattr(engine, "_place_order", lambda *a, **k: "OID-1")
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 1141.0)
        assert leg.on_signal_candle("m2", 1140.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.IN_LONG
        assert leg.entry_price == 1141.0
        # 30% SL below the buy premium
        assert leg.sl_price == pytest.approx(798.70)

    def test_no_buy_below_virtual_sl(self, engine, leg):
        _arm(leg)
        assert leg.on_signal_candle("m2", 1139.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.PAPER_SHORT

    def test_vix_blocks_but_signal_stays_armed(self, engine, leg, monkeypatch):
        _arm(leg)
        monkeypatch.setattr(engine, "vix_ok", lambda: False)
        assert leg.on_signal_candle("m2", 1150.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.PAPER_SHORT  # armed, not dead

        # VIX cools → a LATER candle at/above v_sl fires the buy
        monkeypatch.setattr(engine, "vix_ok", lambda: True)
        monkeypatch.setattr(engine, "_place_order", lambda *a, **k: "OID-2")
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 1150.0)
        assert leg.on_signal_candle("m3", 1150.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_failed_buy_keeps_paper_short(self, engine, leg, monkeypatch):
        _arm(leg)
        monkeypatch.setattr(engine, "_place_order", lambda *a, **k: None)
        assert leg.on_signal_candle("m2", 1150.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.PAPER_SHORT


# ---------------------------------------------------------------------------
# TICK trigger mode — same levels, fires on live ticks
# ---------------------------------------------------------------------------


class TestTickTriggerMode:
    def test_tick_arm_and_flip(self, engine, leg, monkeypatch):
        # arm on the first tick at/below the 5% drop level
        assert leg.on_signal_tick(960.0, _noop_save) is False  # above trigger
        assert leg.on_signal_tick(950.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.PAPER_SHORT
        assert leg.v_sl == pytest.approx(1140.0)
        # flip fires the instant a tick touches the virtual SL (no candle wait)
        monkeypatch.setattr(engine, "_place_order", lambda *a, **k: "OID-T")
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 1140.5)
        assert leg.on_signal_tick(1140.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.IN_LONG
        assert leg.entry_price == 1140.5

    def test_tick_arm_and_flip_never_same_tick(self, engine, leg):
        # arming tick sets v_sl 20% above itself — cannot also be >= v_sl
        assert leg.on_signal_tick(950.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.PAPER_SHORT
        assert leg.state != engine.FlipLeg.IN_LONG

    def test_tick_vix_block_keeps_armed(self, engine, leg, monkeypatch):
        leg.on_signal_tick(950.0, _noop_save)
        monkeypatch.setattr(engine, "vix_ok", lambda: False)
        assert leg.on_signal_tick(1150.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.PAPER_SHORT
        monkeypatch.setattr(engine, "vix_ok", lambda: True)
        monkeypatch.setattr(engine, "_place_order", lambda *a, **k: "OID-T2")
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 1151.0)
        assert leg.on_signal_tick(1150.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_tick_be_arm_and_day2_exit(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        # BE arms on a tick, not just candle closes
        leg.on_be_price(1300.0, _noop_save)
        assert leg.be_armed is True
        # Day-1 tick giveback tolerated
        assert leg.on_be_price(995.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG
        # Day-2 tick giveback exits
        leg.entry_date = "2020-01-01"
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: ("OID-B", "placed"))
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 999.0)
        assert leg.on_be_price(1000.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.DONE


# ---------------------------------------------------------------------------
# Long-side stop-loss and exits
# ---------------------------------------------------------------------------


def _go_long(engine, leg, monkeypatch, fill=1000.0):
    _arm(leg)
    monkeypatch.setattr(engine, "_place_order", lambda *a, **k: "OID-E")
    monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: fill)
    leg.on_signal_candle("m-buy", max(fill, leg.v_sl), _noop_save)
    assert leg.state == leg.IN_LONG


class TestLongExit:
    def test_sl_direction_is_downward(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        assert leg.sl_price == pytest.approx(700.0)
        # Above SL: nothing happens (a short engine would have exited here)
        assert leg.on_tick(1300.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_sl_exit_books_long_pnl(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: ("OID-X", "placed"))
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 699.0)
        assert leg.on_tick(700.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.DONE
        # Long PnL = (exit − entry) × qty = (699 − 1000) × 20
        assert leg.realized_pnl == pytest.approx(-6020.0)

    def test_force_exit_profit(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: ("OID-F", "placed"))
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 1200.0)
        assert leg.force_exit(_noop_save) is True
        assert leg.state == engine.FlipLeg.DONE
        assert leg.realized_pnl == pytest.approx(4000.0)

    def test_already_flat_drift_recovery(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: (None, "flat"))
        monkeypatch.setattr(engine, "live_ltp", lambda *_a: 750.0)
        assert leg.force_exit(_noop_save) is True
        assert leg.state == engine.FlipLeg.DONE  # no order fired, state closed

    def test_failed_flatten_stays_long(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: (None, "failed"))
        assert leg.force_exit(_noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG  # operator must see it live


# ---------------------------------------------------------------------------
# Day-2-only breakeven stop (armed at +be_trigger_pct)
# ---------------------------------------------------------------------------


class TestBreakevenStop:
    def test_arms_at_trigger_close(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        assert leg.be_armed is False
        # +29% close: not armed yet
        assert leg.on_be_price(1290.0, _noop_save) is False
        assert leg.be_armed is False
        # +30% close: armed
        leg.on_be_price(1300.0, _noop_save)
        assert leg.be_armed is True

    def test_day1_pullback_tolerated(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        leg.on_be_price(1300.0, _noop_save)  # arm (entry_date == today)
        assert leg.be_armed is True
        # Same-day giveback to entry: NO exit (Day-1 pullbacks are normal)
        assert leg.on_be_price(990.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_day2_giveback_exits(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        leg.on_be_price(1300.0, _noop_save)  # arm on Day-1
        leg.entry_date = "2020-01-01"  # simulate Day-2 (entry was yesterday)
        monkeypatch.setattr(engine, "_smart_flatten", lambda *a, **k: ("OID-BE", "placed"))
        monkeypatch.setattr(engine, "_get_fill_price", lambda *_a: 998.0)
        assert leg.on_be_price(1000.0, _noop_save) is True
        assert leg.state == engine.FlipLeg.DONE

    def test_day2_no_exit_when_not_armed(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        leg.entry_date = "2020-01-01"  # Day-2, never touched +30%
        assert leg.on_be_price(1000.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_day2_above_entry_no_exit(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        leg.be_armed = True
        leg.entry_date = "2020-01-01"
        assert leg.on_be_price(1001.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_disabled_when_zero(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        monkeypatch.setattr(engine, "BE_TRIGGER_PCT", 0.0)
        leg.entry_date = "2020-01-01"
        assert leg.on_be_price(1300.0, _noop_save) is False
        assert leg.be_armed is False
        assert leg.on_be_price(900.0, _noop_save) is False
        assert leg.state == engine.FlipLeg.IN_LONG

    def test_fresh_buy_resets_be(self, engine, leg, monkeypatch):
        _go_long(engine, leg, monkeypatch, fill=1000.0)
        assert leg.be_armed is False
        assert leg.entry_date != ""  # stamped at buy time


# ---------------------------------------------------------------------------
# State routing + entry-day filters
# ---------------------------------------------------------------------------


class TestStateAndFilters:
    def test_state_has_work_only_for_long(self, engine):
        assert engine._state_has_work(None) is False
        assert engine._state_has_work({"main_legs": [{"state": "WATCHING"}]}) is False
        assert engine._state_has_work({"main_legs": [{"state": "PAPER_SHORT"}]}) is False
        assert engine._state_has_work({"main_legs": [{"state": "IN_LONG"}]}) is True

    def test_leg_roundtrip_serialization(self, engine):
        leg = engine.FlipLeg("SYM", "CE", 20)
        leg.state = engine.FlipLeg.PAPER_SHORT
        leg.ref_premium = 1000.0
        leg.v_entry = 950.0
        leg.v_sl = 1140.0
        leg.be_armed = True
        leg.entry_date = "2026-07-10"
        restored = engine.FlipLeg.from_dict(leg.to_dict())
        assert restored.to_dict() == leg.to_dict()

    def test_state_entry_date_prefers_leg(self, engine):
        # save_state stamps trade_date with the SAVE date — routing must use
        # the leg's entry_date so a mid-Day-2 restart still force-exits.
        state = {
            "trade_date": "2026-07-11",  # saved during Day-2
            "main_legs": [
                {"state": "IN_LONG", "entry_date": "2026-07-10"},
            ],
        }
        assert engine._state_entry_date(state) == "2026-07-10"
        # Legacy state without entry_date falls back to trade_date
        state_legacy = {"trade_date": "2026-07-11", "main_legs": [{"state": "IN_LONG"}]}
        assert engine._state_entry_date(state_legacy) == "2026-07-11"

    def test_dte_filter(self, engine, monkeypatch):
        monkeypatch.setattr(engine, "_weekday_key", lambda *_a: "tue")
        monkeypatch.setattr(
            engine, "get_next_expiry_with_dte", lambda: ("25JUL25", 5)
        )
        allowed, reason = engine.entry_allowed_today()
        assert allowed is False
        assert "DTE 5" in reason

        monkeypatch.setattr(
            engine, "get_next_expiry_with_dte", lambda: ("25JUL25", 2)
        )
        allowed, _ = engine.entry_allowed_today()
        assert allowed is True

    def test_friday_filter(self, engine, monkeypatch):
        monkeypatch.setattr(engine, "_weekday_key", lambda *_a: "fri")
        allowed, reason = engine.entry_allowed_today()
        assert allowed is False
        assert "Friday" in reason or "entries" in reason

    def test_candle_source_ws_first(self, engine, monkeypatch):
        # Default WS mode: entry/BE candles come from the live tick feed;
        # the history API is only the fallback when WS has no closed candle.
        monkeypatch.setattr(engine, "CANDLE_SOURCE", "WS")
        monkeypatch.setattr(engine, "_get_last_closed_candle", lambda *_a: ("m-ws", 101.0))
        monkeypatch.setattr(
            engine, "get_last_closed_1m_candle", lambda *_a: ("m-hist", 999.0)
        )
        assert engine.latest_closed_candle("SYM") == ("m-ws", 101.0)

        # WS has nothing yet → history fallback
        monkeypatch.setattr(engine, "_get_last_closed_candle", lambda *_a: None)
        assert engine.latest_closed_candle("SYM") == ("m-hist", 999.0)

        # HISTORY mode prefers the official close
        monkeypatch.setattr(engine, "CANDLE_SOURCE", "HISTORY")
        monkeypatch.setattr(engine, "_get_last_closed_candle", lambda *_a: ("m-ws", 101.0))
        assert engine.latest_closed_candle("SYM") == ("m-hist", 999.0)

    def test_vix_gate_fails_open(self, engine, monkeypatch):
        # No VIX data available → do not block (backtest parity)
        monkeypatch.setattr(engine, "current_vix", lambda: 0.0)
        assert engine.vix_ok() is True
        monkeypatch.setattr(engine, "current_vix", lambda: 17.9)
        assert engine.vix_ok() is True
        monkeypatch.setattr(engine, "current_vix", lambda: 18.1)
        assert engine.vix_ok() is False
