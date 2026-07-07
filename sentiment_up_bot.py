#!/usr/bin/env python3
"""
Polymarket Rapid Momentum Scalper — Final Combined Bot
========================================================
Built using only the pieces that have actually proven out across this whole
project, not speculative ideas layered back in:

  - Direction is decided SOLELY by delta-from-price-to-beat (BTC's real price
    vs the current window's own open) — this was the one genuine fix that
    corrected a real, confirmed flaw in an earlier bot (betting on a small
    local wiggle instead of the bigger picture). No local-only momentum
    signal is used here at all.
  - Buy price is NOT capped at a fixed ceiling — this bot is explicitly meant
    to catch momentum already in progress, which can mean buying at $0.80,
    not just near $0.50. Ceiling is relative: observed price + small buffer,
    just enough to actually get filled.
  - Sell mechanics reuse the proven resting-order pattern: instantly rest a
    sell at entry + PROFIT_MARGIN the moment a buy confirms, force-exit if
    bracket order (take-profit and stop-loss placed simultaneously) — the ultimate backstop is BRACKET_TIMEOUT_SECONDS.
  - Whole-share flooring, the balance-safety fallback, and the crash-safety
    None-price guard are all carried over unchanged.

Runs continuously throughout the ENTIRE 5-minute window (not just at open),
watching for a real delta signal and acting on it, up to MAX_TRADES_PER_WINDOW
times per window.

IMPORTANT — read before running live:
  This combines proven mechanics into a new configuration (much thinner
  margin, much shorter force-exit, no buy ceiling, more entries per window)
  that has NOT itself been validated with real data. Each PIECE is proven;
  this SPECIFIC COMBINATION is not. Run --dry-run for a meaningful sample
  before ever using --live.

Usage:
  python breakthrough_bot.py --dry-run
  python breakthrough_bot.py --live --amount 2
"""

import time
import json
import csv
import argparse
import threading
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

SYMBOLS = {"BTC": "BTCUSDT"}
MARKETS = {
    "btc-updown-5m": "BTC",
}

MIN_DELTA_PCT_TO_TRUST = 0.01   # same validated starting point from the momentum bot — filters pure noise
INVERT_SIGNAL = False           # Reverted per explicit request — betting opposite the delta lost too,
                                  # which is real, useful evidence (see explanation), not a dead end.
                                  # (e.g. a $0.01 delta on a $60k+ asset) while still catching real moves.
BUY_CEILING_BUFFER = 0.02        # willing to pay up to (observed price + this) — NOT a fixed cap, since this
                                  # bot is meant to catch momentum already in progress, which can mean buying
                                  # at $0.80, not just near $0.50.
BUY_TIMEOUT_SEC    = 2.0

PROFIT_MARGIN      = 0.15        # take-profit target — raised to 0.15 per explicit request, aiming to win
                                    # big and lose small rather than a thin, spread-vulnerable margin
STOP_LOSS_MARGIN   = 0.06        # WIDENED from 0.02 — real data showed several stop-losses triggering in
                                    # under 2 seconds, consistent with the old $0.02 gap sitting inside normal
                                    # bid-ask spread rather than reflecting real adverse movement. NOTE: for a
                                    # pure random walk with no directional edge, ANY take-profit/stop-loss ratio
                                    # has ~zero expected value in theory (a wider reward just means a lower win
                                    # rate, not higher profitability) — widening the stop specifically targets
                                    # spread noise, not the ratio itself.
                                    # instead of riding it down further
BRACKET_TIMEOUT_SECONDS = 60     # ultimate backstop only — if NEITHER bracket level is reached this long
                                    # after buying, force-exit at whatever price is available

MAX_TRADES_PER_WINDOW = 8        # raised from 6 per explicit request
MONITOR_INTERVAL      = 1.0      # how often to check for a new entry opportunity throughout the window

POLL_INTERVAL_SLOW = 0.5

# ─── UTILITIES ───────────────────────────────────────────────────────────────

_print_lock = threading.Lock()

def ts_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg, crypto=""):
    prefix = f"[{crypto}] " if crypto else ""
    with _print_lock:
        print(f"[{ts_str()}] {prefix}{msg}", flush=True)

def now_unix():
    return time.time()


def get_binance_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=2)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def get_window_open_price(symbol: str, window_ts: int) -> float | None:
    """Fetches the real 'price to beat' — BTC's price at the moment this window opened."""
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "startTime": window_ts * 1000, "limit": 1},
            timeout=3,
        )
        r.raise_for_status()
        candles = r.json()
        if candles:
            return float(candles[0][1])
        return None
    except Exception:
        return None


def get_window_market(slug_prefix: str, start_ts: int) -> dict | None:
    slug = f"{slug_prefix}-{start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        event = data[0]
    except Exception:
        return None
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    try:
        outcomes       = json.loads(market.get("outcomes", "[]"))
        clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
    except Exception:
        return None
    if len(outcomes) < 2 or len(clob_token_ids) < 2:
        return None
    tokens = dict(zip(outcomes, clob_token_ids))
    if "Down" not in tokens or "Up" not in tokens:
        return None
    return {
        "slug": slug, "crypto": MARKETS[slug_prefix], "start_ts": start_ts, "close_ts": start_ts + 300,
        "down_token": tokens["Down"], "up_token": tokens["Up"],
        "condition_id": market.get("conditionId", ""), "title": event.get("title", ""),
    }


def get_order_book(token_id: str) -> dict:
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=2)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def best_ask(book: dict):
    asks = book.get("asks", [])
    if not asks:
        return None, None
    cheapest = min(asks, key=lambda a: float(a["price"]))
    return float(cheapest["price"]), float(cheapest["size"])


def best_bid(book: dict):
    bids = book.get("bids", [])
    if not bids:
        return None, None
    highest = max(bids, key=lambda b: float(b["price"]))
    return float(highest["price"]), float(highest["size"])


def next_window_start(now: float) -> int:
    return int((now // 300) + 1) * 300


# ─── PERSISTENT CSV LOG ──────────────────────────────────────────────────────

CSV_FIELDS = [
    "timestamp", "bot_name", "mode", "crypto", "slug", "trade_num_this_window",
    "delta_side", "delta_value", "delta_pct",
    "buy_result", "buy_price", "buy_shares", "buy_elapsed_ms", "spread_at_buy",
    "sell_result", "sell_price", "seconds_to_sell", "pnl_usd", "notes",
]

class TradeLogger:
    def __init__(self, bot_name: str):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.csv")
        self.lock = threading.Lock()
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_FIELDS)

    def write(self, row: dict):
        row = {**{k: "" for k in CSV_FIELDS}, **row}
        with self.lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([row[k] for k in CSV_FIELDS])


# ─── CORE BOT ────────────────────────────────────────────────────────────────

class BreakthroughBot:
    def __init__(self, dry_run: bool, amount: float):
        self.dry_run  = dry_run
        self.amount   = amount
        self.bot_name = os.getenv("BOT_NAME", "breakthrough_bot")
        self.mode_str = "dry_run" if dry_run else "live"
        self.stop_event = threading.Event()
        self.trades = []
        self.trades_lock = threading.Lock()
        self.logger = TradeLogger(self.bot_name)

        self.client = None
        if not dry_run:
            self._init_client()

        log("=" * 70)
        log(f"Rapid Momentum Scalper | {self.mode_str.upper()} | ${amount:.2f}/trade | bot_name={self.bot_name}")
        log(f"Direction: delta-from-price-to-beat only (min {MIN_DELTA_PCT_TO_TRUST}% to trust)"
            + (" | ⚠️ INVERT_SIGNAL=True — betting AGAINST the delta signal (test mode)" if INVERT_SIGNAL else ""))
        log(f"Buy: observed price + ${BUY_CEILING_BUFFER} buffer (no fixed ceiling) | timeout {BUY_TIMEOUT_SEC}s")
        log(f"Sell: bracket order — take-profit entry+${PROFIT_MARGIN} | stop-loss entry-${STOP_LOSS_MARGIN} | "
            f"backstop timeout {BRACKET_TIMEOUT_SECONDS}s | "
            f"max {MAX_TRADES_PER_WINDOW} trades/window")
        log(f"Trade log: {self.logger.path}")
        log("=" * 70)

    def _init_client(self):
        from py_clob_client_v2 import ClobClient, AssetType, BalanceAllowanceParams
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
        self.client = ClobClient(
            host=CLOB_API, key=os.environ["POLY_PRIVATE_KEY"], chain_id=137,
            signature_type=signature_type, funder=os.environ["POLY_PROXY_WALLET"],
        )
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.client.update_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=signature_type,
        ))

    # ── BUY ──────────────────────────────────────────────────────────────────

    def _attempt_buy(self, token: str, observed_price: float, crypto: str) -> dict:
        ceiling = round(observed_price + BUY_CEILING_BUFFER, 4)
        MIN_SHARES = 5  # CONFIRMED via a real live API error on the other bots in this project

        if self.dry_run:
            book = get_order_book(token)
            price, size = best_ask(book)
            if price is not None and price <= ceiling:
                shares = max(MIN_SHARES, round(self.amount / price))
                log(f"[DRY] BUY would fill: ask ${price:.3f} (size {size})", crypto)
                return {"result": "bought", "price": price, "shares": shares}
            log(f"[DRY] BUY missed: no ask <= ${ceiling}", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
        size = max(MIN_SHARES, round(self.amount / ceiling))
        try:
            resp = self.client.create_and_post_order(
                OrderArgsV2(token_id=token, price=ceiling, size=size, side=Side.BUY),
                order_type=OrderType.GTC,
            )
        except Exception as e:
            log(f"❌ BUY order failed to submit: {e}", crypto)
            return {"result": "error", "price": None, "shares": 0}

        order_id = resp.get("orderID", "")
        deadline = now_unix() + BUY_TIMEOUT_SEC
        last_known_size = 0.0
        while now_unix() < deadline:
            try:
                detail = self.client.get_order(order_id)
            except Exception:
                detail = None
            if detail is None:
                break
            try:
                current_size = float(detail.get("size_matched", 0))
                if current_size > last_known_size:
                    last_known_size = current_size
            except (TypeError, ValueError):
                pass
            time.sleep(0.25)

        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception:
            pass

        if last_known_size <= 0:
            try:
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams
                bal_resp = self.client.get_balance_allowance(BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token,
                    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                ))
                real_balance = float(bal_resp.get("balance", 0)) / 1_000_000
                if real_balance >= 0.5:
                    log(f"⚠️ get_order() showed no fill, but balance check found {real_balance} shares — correcting course", crypto)
                    return {"result": "bought", "price": ceiling, "shares": real_balance}
            except Exception as e:
                log(f"⚠️ Final balance safety-check failed ({e})", crypto)
            log(f"❌ BUY timed out with no confirmed fill after {BUY_TIMEOUT_SEC}s", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        log(f"✅ BUY confirmed: {last_known_size} shares at ceiling ${ceiling}, order {order_id[:16]}...", crypto)
        return {"result": "bought", "price": ceiling, "shares": last_known_size}

    # ── SELL ─────────────────────────────────────────────────────────────────

    def _watch_for_sell(self, token: str, buy_price: float, raw_shares: float, crypto: str) -> dict:
        shares = int(raw_shares)
        if shares != raw_shares:
            log(f"⚠️ Buy partially filled: held {raw_shares}, flooring to {shares} whole shares", crypto)
        if shares < 1:
            log("⚠️ Partial fill left less than 1 whole share — forcing immediate exit", crypto)
            exit_result = self._force_exit(token, raw_shares, crypto)
            pnl = -round(buy_price * raw_shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "sub-1-share partial fill"}

        # BRACKET ORDER: take-profit and stop-loss placed at the same time,
        # right after buying. Whichever the price reaches first determines
        # the outcome — a real win capped losses, or a small, controlled
        # loss if the market moves the other way. This replaces the old
        # single-target + blind-timeout design.
        take_profit_price = round(buy_price + PROFIT_MARGIN, 4)
        stop_loss_price   = round(buy_price - STOP_LOSS_MARGIN, 4)
        log(f"Bracket: take-profit ${take_profit_price} (+${PROFIT_MARGIN}) | "
            f"stop-loss ${stop_loss_price} (-${STOP_LOSS_MARGIN})", crypto)
        buy_time = now_unix()

        if not self.dry_run:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            try:
                self.client.update_balance_allowance(BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token,
                    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                ))
            except Exception as e:
                log(f"⚠️ Could not sync conditional balance ({e})", crypto)

            from py_clob_client_v2 import OrderArgsV2, MarketOrderArgsV2, Side, OrderType, OrderPayload

            # ONLY the take-profit is placed as a real resting order. This is
            # safe — it only ever fills if the market genuinely reaches that
            # price, since no rational buyer pays MORE than the current fair
            # price just to trade with us.
            #
            # REAL BUG FIXED HERE: the stop-loss must NOT be placed as a
            # resting order the same way. A resting sell below the current
            # price is a standing, always-executable bargain — any buyer
            # scanning the book would snap it up immediately, even while the
            # price is genuinely rising, not falling. Polymarket's client
            # library has no native conditional/stop order type (confirmed:
            # no "stop" order type exists in py_clob_client_v2), so instead
            # we watch the real price ourselves and only submit a real sell
            # the INSTANT it actually reaches the stop level — never before.
            tp_order_id = None
            try:
                tp_resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=token, price=take_profit_price, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC,
                )
                tp_order_id = tp_resp.get("orderID", "")
                log(f"Take-profit resting order placed at ${take_profit_price}, order {tp_order_id[:12]}...", crypto)
            except Exception as e:
                log(f"⚠️ Could not place take-profit order ({e}) — forcing exit immediately", crypto)
                exit_result = self._force_exit(token, shares, crypto)
                pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
                return {**exit_result, "pnl_usd": pnl, "notes": "take-profit placement failed"}

            deadline = buy_time + BRACKET_TIMEOUT_SECONDS
            while now_unix() < deadline:
                # Check take-profit fill
                try:
                    detail = self.client.get_order(tp_order_id)
                except Exception:
                    detail = None
                if detail is not None:
                    try:
                        filled = float(detail.get("size_matched", 0))
                    except (TypeError, ValueError):
                        filled = 0
                    if filled >= shares:
                        pnl = round((take_profit_price - buy_price) * shares, 4)
                        return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                                "notes": "take_profit hit"}

                # Check the REAL current price against the stop-loss level —
                # only submit a real sell order once it's actually been reached.
                book = get_order_book(token)
                current_bid, current_bid_size = best_bid(book)
                if current_bid is not None and current_bid <= stop_loss_price:
                    log(f"Stop-loss level reached (bid ${current_bid:.3f} <= ${stop_loss_price}) — "
                        f"cancelling take-profit and exiting now", crypto)
                    try:
                        self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                    except Exception:
                        pass
                    try:
                        resp = self.client.create_and_post_market_order(
                            MarketOrderArgsV2(token_id=token, amount=shares, side=Side.SELL),
                            order_type=OrderType.FAK,
                        )
                        status = str(resp.get("status", "")).lower()
                        exit_price = None
                        if status == "matched":
                            try:
                                cost = float(resp.get("makingAmount", 0)) / 1_000_000
                                candidate_price = round(cost / shares, 4) if shares else None
                                # SANITY CHECK: this field's meaning for a SELL order is
                                # NOT independently confirmed (only validated for BUY
                                # orders elsewhere in this project). A valid price here
                                # must be between 0 and 1. If it isn't, don't trust it —
                                # log the raw response so this can be verified against
                                # the real account rather than silently recording a
                                # wrong number.
                                if candidate_price is not None and 0 < candidate_price < 1:
                                    exit_price = candidate_price
                                else:
                                    log(f"⚠️ Parsed sell price ${candidate_price} looks invalid — "
                                        f"NOT trusting it. Raw response: {resp}", crypto)
                            except Exception as e:
                                log(f"⚠️ Could not parse sell fill price ({e}). Raw response: {resp}", crypto)
                        if exit_price is None:
                            # Fall back to the real current bid we just observed —
                            # a genuine estimate, clearly labeled as such below,
                            # rather than a possibly-wrong parsed number.
                            exit_price = current_bid
                            price_is_estimate = True
                            log(f"Using observed bid ${current_bid} as an ESTIMATE — verify against your real account", crypto)
                        else:
                            price_is_estimate = False
                    except Exception as e:
                        log(f"⚠️ Stop-loss market sell failed ({e}) — falling back to force-exit", crypto)
                        exit_result = self._force_exit(token, shares, crypto)
                        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
                        return {**exit_result, "pnl_usd": pnl, "notes": "stop-loss trigger, fallback force-exit"}
                    pnl = round((exit_price - buy_price) * shares, 4)
                    notes = "stop_loss hit (price ESTIMATED, not exchange-confirmed — verify against real account)" if price_is_estimate else "stop_loss hit"
                    return {"result": "sold_stop_loss", "price": exit_price, "pnl_usd": pnl, "notes": notes}

                time.sleep(POLL_INTERVAL_SLOW)

            # Neither triggered within the backstop timeout — cancel the resting
            # take-profit order (the stop-loss was never a real resting order,
            # so there's nothing else to cancel) and force-exit.
            try:
                self.client.cancel_order(OrderPayload(orderID=tp_order_id))
            except Exception:
                pass
            log(f"⏰ Neither bracket level reached within {BRACKET_TIMEOUT_SECONDS}s — force-exiting", crypto)
            exit_result = self._force_exit(token, shares, crypto)
            pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "bracket timeout, force-exit"}

        # DRY-RUN
        while now_unix() - buy_time < BRACKET_TIMEOUT_SECONDS:
            book = get_order_book(token)
            bid_price, bid_size = best_bid(book)
            if bid_price is not None and bid_size >= shares:
                elapsed = round(now_unix() - buy_time, 1)
                if bid_price >= take_profit_price:
                    log(f"[DRY] Take-profit hit: bid ${bid_price:.3f} at {elapsed}s", crypto)
                    pnl = round((take_profit_price - buy_price) * shares, 4)
                    return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                            "notes": "take_profit hit", "seconds_to_sell": elapsed}
                elif bid_price <= stop_loss_price:
                    log(f"[DRY] Stop-loss hit: bid ${bid_price:.3f} at {elapsed}s", crypto)
                    pnl = round((stop_loss_price - buy_price) * shares, 4)
                    return {"result": "sold_stop_loss", "price": stop_loss_price, "pnl_usd": pnl,
                            "notes": "stop_loss hit", "seconds_to_sell": elapsed}
            time.sleep(POLL_INTERVAL_SLOW)

        log(f"⏰ Neither bracket level reached within {BRACKET_TIMEOUT_SECONDS}s — force-exiting at best price", crypto)
        exit_result = self._force_exit(token, shares, crypto)
        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
        return {**exit_result, "pnl_usd": pnl, "notes": "bracket timeout, force-exit", "seconds_to_sell": BRACKET_TIMEOUT_SECONDS}

    def _force_exit(self, token: str, shares: float, crypto: str) -> dict:
        if self.dry_run:
            book = get_order_book(token)
            price, size = best_bid(book)
            if price is None:
                log("[DRY] No bids at all for force-exit — total loss this trade", crypto)
                return {"result": "no_bids", "price": None}
            log(f"[DRY] Force-exit would fill at ${price:.3f}", crypto)
            return {"result": "exited", "price": price}

        from py_clob_client_v2 import MarketOrderArgsV2, Side, OrderType
        try:
            resp = self.client.create_and_post_market_order(
                MarketOrderArgsV2(token_id=token, amount=shares, side=Side.SELL),
                order_type=OrderType.FAK,
            )
        except Exception as e:
            log(f"⚠️ Force-exit order failed: {e}", crypto)
            return {"result": "error", "price": None}
        status = str(resp.get("status", "")).lower()
        if status == "matched":
            try:
                cost = float(resp.get("makingAmount", 0)) / 1_000_000
                exit_price = round(cost / shares, 4) if shares else None
            except Exception:
                exit_price = None
            return {"result": "exited", "price": exit_price}
        return {"result": "unmatched", "price": None}

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────

    def _monitor_window(self, slug_prefix: str, start_ts: int):
        crypto = MARKETS[slug_prefix]
        close_ts = start_ts + 300
        symbol = SYMBOLS.get(crypto)

        market = None
        find_deadline = now_unix() + 5
        while now_unix() < find_deadline:
            market = get_window_market(slug_prefix, start_ts)
            if market:
                break
            time.sleep(0.5)
        if not market:
            log(f"Could not find market for window starting {start_ts} — skipping entire window", crypto)
            return

        window_open_price = get_window_open_price(symbol, start_ts) if symbol else None
        if window_open_price:
            log(f"Price to beat this window: ${window_open_price:,.2f}", crypto)
        else:
            log("Could not fetch price-to-beat — skipping entire window (no reliable direction signal without it)", crypto)
            return

        trades_this_window = 0
        while now_unix() < close_ts and trades_this_window < MAX_TRADES_PER_WINDOW:
            if self.stop_event.is_set():
                return

            current_btc_price = get_binance_price(symbol) if symbol else None
            if current_btc_price is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            delta_value = current_btc_price - window_open_price
            delta_pct = abs(delta_value) / window_open_price * 100
            delta_side = "Up" if delta_value > 0 else "Down"

            if delta_pct < MIN_DELTA_PCT_TO_TRUST:
                time.sleep(MONITOR_INTERVAL)
                continue

            raw_delta_side = delta_side
            if INVERT_SIGNAL:
                delta_side = "Down" if raw_delta_side == "Up" else "Up"

            token = market["up_token"] if delta_side == "Up" else market["down_token"]
            book = get_order_book(token)
            observed_price, _ = best_ask(book)
            if observed_price is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            # Log the real spread at this exact moment — directly tests whether
            # stop-losses are being triggered by real movement or just spread noise.
            observed_bid, _ = best_bid(book)
            spread_at_buy = round(observed_price - observed_bid, 4) if observed_bid is not None else None

            trades_this_window += 1
            invert_note = f" [INVERTED from {raw_delta_side}]" if INVERT_SIGNAL else ""
            log(f"Delta signal (trade {trades_this_window}/{MAX_TRADES_PER_WINDOW}): "
                f"{delta_value:+.2f} ({delta_pct:.4f}%) -> buying {delta_side}{invert_note} @ ~${observed_price} "
                f"(spread: ${spread_at_buy})", crypto)

            buy_info = self._attempt_buy(token, observed_price, crypto)
            row = {
                "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                "slug": market["slug"], "trade_num_this_window": trades_this_window,
                "delta_side": delta_side, "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                "buy_result": buy_info["result"], "buy_price": buy_info["price"], "buy_shares": buy_info["shares"],
                "spread_at_buy": spread_at_buy,
            }

            if buy_info["result"] != "bought":
                row.update({"sell_result": "n/a", "sell_price": "", "pnl_usd": 0, "notes": "no buy fill"})
                self._record(row)
                time.sleep(MONITOR_INTERVAL)
                continue

            sell_info = self._watch_for_sell(token, buy_info["price"], buy_info["shares"], crypto)
            row.update({
                "sell_result": sell_info["result"], "sell_price": sell_info["price"],
                "seconds_to_sell": sell_info.get("seconds_to_sell", ""),
                "pnl_usd": sell_info["pnl_usd"], "notes": sell_info["notes"],
            })
            self._record(row)
            time.sleep(MONITOR_INTERVAL)

    def _record(self, row: dict):
        with self.trades_lock:
            self.trades.append(row)
        self.logger.write(row)
        pnl = row.get("pnl_usd", 0)
        sign = "+" if isinstance(pnl, (int, float)) and pnl >= 0 else ""
        log(f"RECORDED: side={row['delta_side']} | buy={row['buy_result']}@{row['buy_price']} | "
            f"sell={row['sell_result']}@{row['sell_price']} | pnl={sign}${pnl}", row["crypto"])

    def _asset_loop(self, slug_prefix: str):
        crypto = MARKETS[slug_prefix]
        while not self.stop_event.is_set():
            start_ts = next_window_start(now_unix())
            while now_unix() < start_ts and not self.stop_event.is_set():
                time.sleep(1)
            if self.stop_event.is_set():
                break
            log(f"Monitoring window starting {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC", crypto)
            try:
                self._monitor_window(slug_prefix, start_ts)
            except Exception as e:
                log(f"⚠️ Unhandled error this window: {e}", crypto)
            time.sleep(2)

    def run(self):
        threads = [threading.Thread(target=self._asset_loop, args=(prefix,), daemon=True) for prefix in MARKETS]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Stopping...")
            self.stop_event.set()
            self._print_summary()

    def _print_summary(self):
        with self.trades_lock:
            trades = list(self.trades)
        bought      = [t for t in trades if t["buy_result"] == "bought"]
        take_profit = [t for t in bought if t["sell_result"] == "sold_take_profit"]
        stop_loss   = [t for t in bought if t["sell_result"] == "sold_stop_loss"]
        total_pnl = sum(float(t["pnl_usd"] or 0) for t in trades)
        log("-" * 70)
        log(f"SUMMARY — {len(trades)} signals, {len(bought)} buy fills")
        log(f"  Take-profit hits: {len(take_profit)}")
        log(f"  Stop-loss hits: {len(stop_loss)}")
        log(f"  Total PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
        log("-" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Rapid Momentum Scalper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--amount", type=float, default=2.0)
    args = parser.parse_args()

    bot = BreakthroughBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
