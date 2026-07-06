#!/usr/bin/env python3
"""
Polymarket Sentiment-Gated Up Scalper
========================================
Always buys UP, same as the original Always-Up bot — but instead of entering
every single window, it wakes WAKE_BEFORE_SECONDS before the CURRENT window
closes and runs two checks on the real BTC price to decide whether the
UPCOMING window is actually worth entering:

1. MARKET SENTIMENT (first SENTIMENT_WINDOW_SEC of the observation): counts
   how many times BTC's price, measured against the CURRENT window's own
   price-to-beat, reverses direction — a "dip" is a local peak followed by a
   decline, a "surge" is a local trough followed by a rise. If dips clearly
   outnumber surges (or vice versa) by at least SENTIMENT_MIN_DIFFERENCE,
   that's read as the market's collective sentiment heading into the new
   window. If the difference is too small, sentiment is read as uncertain.

2. TRAILING CONSISTENCY (final CONSISTENCY_WINDOW_SEC before window close):
   checks whether the price is STILL moving up in the final stretch, not
   reversing right as the new window is about to begin — since a consistent
   downward move right at the boundary is a real warning sign even if the
   broader sentiment was Up.

Only enters if BOTH read Up. If either is Down or Uncertain, that window is
skipped entirely — this bot is not designed to force an entry.

Buy/sell mechanics are otherwise the same as proven on the other bots:
target $0.50, ceiling $0.52, resting sell at entry+$0.05, force-exit if
unfilled 10 seconds after buying.

IMPORTANT — read before running live:
  This is a genuinely new, untested hypothesis about market sentiment
  predicting the next window's direction. It has NOT been validated with
  real data. Run --dry-run for a meaningful sample before ever using --live.

Usage:
  python sentiment_up_bot.py --dry-run
  python sentiment_up_bot.py --live --amount 2
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

BUY_TARGET_PRICE  = 0.50
BUY_CEILING_PRICE = 0.52
BUY_TIMEOUT_SEC   = 3.0
PROFIT_MARGIN     = 0.05   # sell trigger = entry price + this
TRADE_AGE_CAP_SECONDS = 10  # force-exit if unfilled this many seconds after buying

WAKE_BEFORE_SECONDS     = 60   # start analyzing this many seconds before the CURRENT window closes
CONSISTENCY_WINDOW_SEC  = 10   # final N seconds of the observation used for the trailing consistency check
SENTIMENT_WINDOW_SEC    = WAKE_BEFORE_SECONDS - CONSISTENCY_WINDOW_SEC  # the remaining 50s used for dip/surge counting
SENTIMENT_MIN_DIFFERENCE = 2   # minimum |surges - dips| to be read as confident, not uncertain — a starting
                                 # hypothesis, not a calibrated number. Needs real data to validate.
SAMPLE_INTERVAL_SEC     = 1.0  # how often to sample BTC price during the 60s observation

POLL_INTERVAL_SLOW = 1.0

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
    """Fetches the real 'price to beat' for a given window — the price at the moment it opened."""
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


def detect_turning_points(samples: list) -> tuple:
    """
    samples: list of (timestamp, value) where value = btc_price - price_to_beat.
    A 'dip' is a local peak followed by a decline (price was rising, then
    started falling). A 'surge' is a local trough followed by a rise (price
    was falling, then started rising). Returns (dips, surges) counts.
    """
    dips, surges = 0, 0
    if len(samples) < 3:
        return dips, surges
    values = [v for _, v in samples]
    direction = None
    for i in range(1, len(values)):
        if values[i] > values[i - 1]:
            new_direction = "up"
        elif values[i] < values[i - 1]:
            new_direction = "down"
        else:
            continue
        if direction is not None and new_direction != direction:
            if direction == "up" and new_direction == "down":
                dips += 1
            elif direction == "down" and new_direction == "up":
                surges += 1
        direction = new_direction
    return dips, surges


def analyze_pre_window(symbol: str, current_window_close_ts: float, stop_event: threading.Event, crypto: str) -> dict:
    """
    Runs during the last WAKE_BEFORE_SECONDS of the CURRENT window, before it
    closes, to decide whether to buy Up in the UPCOMING window.
    """
    result = {"enter": False, "reason": "", "dips": 0, "surges": 0, "sentiment": None, "consistency_trend": None}

    current_window_start_ts = int(current_window_close_ts) - 300
    price_to_beat = get_window_open_price(symbol, current_window_start_ts)
    if price_to_beat is None:
        result["reason"] = "could not fetch current window's price-to-beat — skipping"
        return result

    samples = []
    while now_unix() < current_window_close_ts:
        if stop_event.is_set():
            result["reason"] = "stopped during observation"
            return result
        price = get_binance_price(symbol)
        if price is not None:
            samples.append((now_unix(), price - price_to_beat))
        time.sleep(SAMPLE_INTERVAL_SEC)

    if len(samples) < 5:
        result["reason"] = f"insufficient samples collected ({len(samples)}) — skipping"
        return result

    cutoff = current_window_close_ts - CONSISTENCY_WINDOW_SEC
    sentiment_samples   = [(t, v) for t, v in samples if t < cutoff]
    consistency_samples = [(t, v) for t, v in samples if t >= cutoff]

    dips, surges = detect_turning_points(sentiment_samples)
    diff = surges - dips
    if diff >= SENTIMENT_MIN_DIFFERENCE:
        sentiment = "Up"
    elif -diff >= SENTIMENT_MIN_DIFFERENCE:
        sentiment = "Down"
    else:
        sentiment = "Uncertain"

    consistency_trend = None
    if len(consistency_samples) >= 2:
        first_val, last_val = consistency_samples[0][1], consistency_samples[-1][1]
        consistency_trend = "Up" if last_val > first_val else ("Down" if last_val < first_val else "Flat")

    result["dips"], result["surges"], result["sentiment"], result["consistency_trend"] = dips, surges, sentiment, consistency_trend
    result["enter"] = (sentiment == "Up") and (consistency_trend == "Up")
    result["reason"] = (f"dips={dips} surges={surges} diff={diff:+d} sentiment={sentiment} "
                         f"| trailing {CONSISTENCY_WINDOW_SEC}s trend={consistency_trend}")
    return result


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
    "timestamp", "bot_name", "mode", "crypto", "slug",
    "dips", "surges", "sentiment", "consistency_trend", "entered",
    "buy_result", "buy_price", "buy_shares",
    "sell_result", "sell_price", "pnl_usd", "notes",
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

class SentimentUpBot:
    def __init__(self, dry_run: bool, amount: float):
        self.dry_run  = dry_run
        self.amount   = amount
        self.bot_name = os.getenv("BOT_NAME", "sentiment_up_bot")
        self.mode_str = "dry_run" if dry_run else "live"
        self.stop_event = threading.Event()
        self.trades = []
        self.trades_lock = threading.Lock()
        self.logger = TradeLogger(self.bot_name)

        self.client = None
        if not dry_run:
            self._init_client()

        log("=" * 70)
        log(f"Sentiment-Gated Up Scalper | {self.mode_str.upper()} | ${amount:.2f}/trade | bot_name={self.bot_name}")
        log(f"Buy: target ${BUY_TARGET_PRICE} ceiling ${BUY_CEILING_PRICE} | Sell: entry + ${PROFIT_MARGIN} | "
            f"force-exit after {TRADE_AGE_CAP_SECONDS}s unfilled")
        log(f"Pre-window analysis: wake {WAKE_BEFORE_SECONDS}s before close | "
            f"sentiment window {SENTIMENT_WINDOW_SEC}s | consistency window {CONSISTENCY_WINDOW_SEC}s | "
            f"min sentiment difference {SENTIMENT_MIN_DIFFERENCE}")
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

    def _attempt_buy(self, token: str, crypto: str) -> dict:
        MIN_SHARES = 5  # CONFIRMED via a real live API error on the other bots in this project

        if self.dry_run:
            deadline = now_unix() + BUY_TIMEOUT_SEC
            last_seen_price = None
            while now_unix() < deadline:
                book = get_order_book(token)
                price, size = best_ask(book)
                if price is not None:
                    last_seen_price = price
                if price is not None and price <= BUY_CEILING_PRICE:
                    shares = max(MIN_SHARES, round(self.amount / price))
                    log(f"[DRY] BUY would fill: ask ${price:.3f} (size {size})", crypto)
                    return {"result": "bought", "price": price, "shares": shares}
                time.sleep(0.1)
            price_info = f"last ask seen ${last_seen_price:.3f}" if last_seen_price is not None else "no asks seen"
            log(f"[DRY] BUY missed: no ask <= ${BUY_CEILING_PRICE} ({price_info})", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
        size = max(MIN_SHARES, round(self.amount / BUY_CEILING_PRICE))
        try:
            resp = self.client.create_and_post_order(
                OrderArgsV2(token_id=token, price=BUY_CEILING_PRICE, size=size, side=Side.BUY),
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
                    return {"result": "bought", "price": BUY_CEILING_PRICE, "shares": real_balance}
            except Exception as e:
                log(f"⚠️ Final balance safety-check failed ({e})", crypto)
            log(f"❌ BUY timed out with no confirmed fill after {BUY_TIMEOUT_SEC}s", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        log(f"✅ BUY confirmed: {last_known_size} shares at ceiling ${BUY_CEILING_PRICE}, order {order_id[:16]}...", crypto)
        return {"result": "bought", "price": BUY_CEILING_PRICE, "shares": last_known_size}

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

        sell_trigger = round(buy_price + PROFIT_MARGIN, 4)
        log(f"Sell trigger: ${sell_trigger} (bought ${buy_price} + ${PROFIT_MARGIN})", crypto)
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

            from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
            try:
                resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=token, price=sell_trigger, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC,
                )
                sell_order_id = resp.get("orderID", "")
                log(f"Resting SELL placed at ${sell_trigger}, order {sell_order_id[:16]}...", crypto)
            except Exception as e:
                log(f"⚠️ Could not place resting sell ({e}) — forcing exit immediately", crypto)
                exit_result = self._force_exit(token, shares, crypto)
                pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
                return {**exit_result, "pnl_usd": pnl, "notes": "resting sell placement failed"}

            last_known_sold = 0.0
            while now_unix() - buy_time < TRADE_AGE_CAP_SECONDS:
                try:
                    detail = self.client.get_order(sell_order_id)
                except Exception:
                    detail = None
                if detail is None:
                    last_known_sold = shares
                    break
                try:
                    current_sold = float(detail.get("size_matched", 0))
                    if current_sold > last_known_sold:
                        last_known_sold = current_sold
                except (TypeError, ValueError):
                    pass
                time.sleep(POLL_INTERVAL_SLOW)

            if last_known_sold >= shares:
                pnl = round((sell_trigger - buy_price) * shares, 4)
                return {"result": "sold", "price": sell_trigger, "pnl_usd": pnl, "notes": "sold via resting order"}

            try:
                self.client.cancel_order(OrderPayload(orderID=sell_order_id))
            except Exception:
                pass
            remaining = round(shares - last_known_sold, 4)
            if remaining < 1:
                pnl = round((sell_trigger - buy_price) * last_known_sold, 4)
                return {"result": "sold", "price": sell_trigger, "pnl_usd": pnl, "notes": "dust remainder left"}
            exit_result = self._force_exit(token, int(remaining), crypto)
            sold_pnl = round((sell_trigger - buy_price) * last_known_sold, 4)
            exit_pnl = round((exit_result["price"] - buy_price) * int(remaining), 4) if exit_result["price"] is not None else -round(buy_price * int(remaining), 4)
            return {**exit_result, "pnl_usd": round(sold_pnl + exit_pnl, 4), "notes": "partial via resting order + force-exit"}

        # DRY-RUN
        while now_unix() - buy_time < TRADE_AGE_CAP_SECONDS:
            book = get_order_book(token)
            price, size = best_bid(book)
            if price is not None and price >= sell_trigger and size >= shares:
                log(f"[DRY] SELL would fill: bid ${price:.3f}", crypto)
                pnl = round((price - buy_price) * shares, 4)
                return {"result": "sold", "price": price, "pnl_usd": pnl, "notes": "sold"}
            time.sleep(POLL_INTERVAL_SLOW)

        log(f"⏰ {TRADE_AGE_CAP_SECONDS}s unfilled — force-exiting at best price", crypto)
        exit_result = self._force_exit(token, shares, crypto)
        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
        return {**exit_result, "pnl_usd": pnl, "notes": "force-exit"}

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

    def _asset_loop(self, slug_prefix: str):
        crypto = MARKETS[slug_prefix]
        symbol = SYMBOLS.get(crypto)
        while not self.stop_event.is_set():
            upcoming_start = next_window_start(now_unix())
            current_window_close_ts = upcoming_start  # the current window closes exactly when the next one opens
            wake_at = current_window_close_ts - WAKE_BEFORE_SECONDS

            while now_unix() < wake_at and not self.stop_event.is_set():
                time.sleep(1)
            if self.stop_event.is_set():
                break

            log(f"Waking {WAKE_BEFORE_SECONDS}s before window close to analyze — "
                f"upcoming window starts {datetime.fromtimestamp(upcoming_start, tz=timezone.utc).strftime('%H:%M:%S')} UTC", crypto)

            analysis = analyze_pre_window(symbol, current_window_close_ts, self.stop_event, crypto)
            log(f"Analysis: {analysis['reason']}", crypto)

            if not analysis["enter"]:
                log("Skipping upcoming window — sentiment/consistency did not both confirm Up", crypto)
                self._record({
                    "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                    "slug": f"{slug_prefix}-{upcoming_start}", "dips": analysis["dips"], "surges": analysis["surges"],
                    "sentiment": analysis["sentiment"], "consistency_trend": analysis["consistency_trend"],
                    "entered": False, "buy_result": "skipped", "buy_price": "", "buy_shares": "",
                    "sell_result": "n/a", "sell_price": "", "pnl_usd": 0, "notes": analysis["reason"],
                })
                time.sleep(2)
                continue

            # Confirmed Up — wait for the new window to actually open, then buy immediately
            while now_unix() < upcoming_start:
                time.sleep(0.01)

            market = None
            find_deadline = now_unix() + 3
            while now_unix() < find_deadline:
                market = get_window_market(slug_prefix, upcoming_start)
                if market:
                    break
                time.sleep(0.1)
            if not market:
                log("Could not find the upcoming market in time — skipping this window", crypto)
                time.sleep(2)
                continue

            log("Entering Up — sentiment and consistency both confirmed", crypto)
            buy_info = self._attempt_buy(market["up_token"], crypto)
            row = {
                "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                "slug": market["slug"], "dips": analysis["dips"], "surges": analysis["surges"],
                "sentiment": analysis["sentiment"], "consistency_trend": analysis["consistency_trend"],
                "entered": True, "buy_result": buy_info["result"], "buy_price": buy_info["price"],
                "buy_shares": buy_info["shares"],
            }
            if buy_info["result"] != "bought":
                row.update({"sell_result": "n/a", "sell_price": "", "pnl_usd": 0, "notes": "no buy fill"})
                self._record(row)
                time.sleep(2)
                continue

            sell_info = self._watch_for_sell(market["up_token"], buy_info["price"], buy_info["shares"], crypto)
            row.update({
                "sell_result": sell_info["result"], "sell_price": sell_info["price"],
                "pnl_usd": sell_info["pnl_usd"], "notes": sell_info["notes"],
            })
            self._record(row)
            time.sleep(2)

    def _record(self, row: dict):
        with self.trades_lock:
            self.trades.append(row)
        self.logger.write(row)
        pnl = row.get("pnl_usd", 0)
        sign = "+" if isinstance(pnl, (int, float)) and pnl >= 0 else ""
        log(f"RECORDED: entered={row['entered']} | buy={row['buy_result']}@{row['buy_price']} | "
            f"sell={row['sell_result']}@{row['sell_price']} | pnl={sign}${pnl}", row["crypto"])

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
        entered = [t for t in trades if t["entered"]]
        bought  = [t for t in entered if t["buy_result"] == "bought"]
        sold    = [t for t in bought if t["sell_result"] == "sold"]
        total_pnl = sum(float(t["pnl_usd"] or 0) for t in trades)
        log("-" * 70)
        log(f"SUMMARY — {len(trades)} windows evaluated, {len(entered)} entered, {len(trades)-len(entered)} skipped")
        log(f"  Buy fills: {len(bought)}/{len(entered)}")
        log(f"  Sold at margin: {len(sold)}")
        log(f"  Total PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
        log("-" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Sentiment-Gated Up Scalper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--amount", type=float, default=2.0)
    args = parser.parse_args()

    bot = SentimentUpBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
