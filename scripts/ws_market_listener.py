#!/usr/bin/env python3
"""
WebSocket market listener for Polymarket.
Subscribes to live price feeds for target markets and triggers entries
when the price moves into the edge window before the market adjusts.

Triggered by: new AIFS/GRIB model run detected (cache refresh)
Layer: supplementary — runs alongside batch scan, not replacing it

Usage:
    python3 ws_market_listener.py --market-ids <id1,id2> --thresholds 0.25,0.30
"""

import asyncio
import json
import sys
import time
import argparse
from collections import defaultdict
from datetime import datetime, timezone

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Connection config
RECONNECT_DELAY = 3
MAX_RETRIES = 3
SUBSCRIPTION_TIMEOUT = 120  # seconds to wait for edge opportunity


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket WebSocket market listener")
    p.add_argument("--market-ids", required=True, help="Comma-separated market IDs to watch")
    p.add_argument("--thresholds", required=True, help="Comma-separated entry price thresholds (float, 0.0-1.0)")
    p.add_argument("--sides", default="", help="Comma-separated sides: yes/no for each market")
    p.add_argument("--questions", default="", help="Comma-separated question labels for logging")
    p.add_argument("--timeout", type=int, default=SUBSCRIPTION_TIMEOUT, help="Seconds to listen before exiting")
    p.add_argument("--min-edge", type=float, default=0.25, help="Minimum edge to trigger entry (default 0.25)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


async def subscribe(ws, market_ids: list):
    """Send subscription message for all target market IDs."""
    subscribe_msg = {
        "type": "subscribe",
        "channel": "market",
        "message_ids": market_ids,
    }
    await ws.send(json.dumps(subscribe_msg))
    return market_ids


async def unsubscribe(ws, market_ids: list):
    """Unsubscribe from markets."""
    unsub_msg = {
        "type": "unsubscribe",
        "channel": "market",
        "message_ids": market_ids,
    }
    await ws.send(json.dumps(unsub_msg))


async def listen_for_edge(args):
    """
    Connect to Polymarket WS, subscribe to target markets,
    and yield entries when price crosses the edge threshold.
    """
    market_ids = args.market_ids.split(",")
    thresholds = [float(t) for t in args.thresholds.split(",")]
    sides = args.sides.split(",") if args.sides else ["yes"] * len(market_ids)
    questions = args.questions.split("||") if args.questions else ["?"] * len(market_ids)

    if not (len(market_ids) == len(thresholds) == len(sides)):
        raise ValueError("market-ids, thresholds, and sides must have same count")

    # Track best price per market
    best_prices = {}       # market_id -> float (best YES price seen)
    price_history = defaultdict(list)  # market_id -> list of (timestamp, price)
    entries_triggered = set()

    connected = False
    retries = 0

    async def connect():
        nonlocal connected, retries
        ws = await websockets.connect(WS_URL, ping_interval=None)
        connected = True
        retries = 0
        await subscribe(ws, market_ids)
        return ws

    async def handle_message(msg: dict):
        """Process incoming WS message. Returns (market_id, price, triggered) or None."""
        if args.verbose:
            print(f"[WS] {msg.get('type')} | {json.dumps(msg)[:120]}")

        msg_type = msg.get("type")
        if msg_type == "price_change":
            market_id = msg.get("message_id") or msg.get("id")
            price_data = msg.get("price", {})

            if isinstance(price_data, dict):
                yes_price = float(price_data.get("yes", 0))
            elif isinstance(price_data, (int, float)):
                yes_price = float(price_data)
            else:
                return None

            if market_id not in market_ids or yes_price <= 0:
                return None

            price_history[market_id].append((time.time(), yes_price))

            if market_id not in best_prices or yes_price < best_prices[market_id]:
                best_prices[market_id] = yes_price

            idx = market_ids.index(market_id)
            threshold = thresholds[idx]
            side = sides[idx]

            # Edge: price is below threshold (entry opportunity)
            # For YES side: lower price = more edge
            # Trigger when price drops below threshold AND we haven't entered yet
            if side.lower() == "yes" and yes_price < threshold:
                edge = threshold - yes_price
                if edge >= args.min_edge:
                    entries_triggered.add(market_id)
                    return market_id, yes_price, "yes", edge, questions[idx]

        elif msg_type == "best_bid_ask":
            market_id = msg.get("message_id") or msg.get("id")
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])

            if not asks or market_id not in market_ids:
                return None

            best_ask = float(asks[0][0]) if asks else None
            if best_ask and best_ask > 0:
                idx = market_ids.index(market_id)
                threshold = thresholds[idx]
                side = sides[idx]

                if side.lower() == "yes" and best_ask < threshold:
                    edge = threshold - best_ask
                    if edge >= args.min_edge:
                        entries_triggered.add(market_id)
                        return market_id, best_ask, "yes", edge, questions[idx]

        elif msg_type == "last_trade_price":
            market_id = msg.get("message_id") or msg.get("id")
            price = msg.get("price")

            if market_id not in market_ids or price is None:
                return None

            try:
                price = float(price)
            except (ValueError, TypeError):
                return None

            if price <= 0:
                return None

            price_history[market_id].append((time.time(), price))

            idx = market_ids.index(market_id)
            threshold = thresholds[idx]
            side = sides[idx]

            if side.lower() == "yes" and price < threshold:
                edge = threshold - price
                if edge >= args.min_edge:
                    entries_triggered.add(market_id)
                    return market_id, price, "yes", edge, questions[idx]

        return None

    start_ts = time.time()
    ws = None

    try:
        ws = await connect()
        print(f"[WS] Connected and subscribed to {len(market_ids)} markets. Listening for {args.timeout}s...")

        async for raw_msg in ws:
            elapsed = time.time() - start_ts
            if elapsed > args.timeout:
                print(f"[WS] Timeout after {elapsed:.0f}s — exiting gracefully")
                break

            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            # Handle heartbeat/ping
            if msg.get("type") in ("ping", "pong"):
                continue

            result = await handle_message(msg)
            if result:
                mid, price, side, edge, question = result
                print(f"[WS] ENTRY SIGNAL | market={mid[:16]}... | price=${price:.4f} | edge={edge:.4f} | q={question[:50]}")

    except websockets.exceptions.ConnectionClosed as e:
        print(f"[WS] Connection closed: {e}")
        if retries < MAX_RETRIES:
            retries += 1
            print(f"[WS] Reconnecting in {RECONNECT_DELAY}s (attempt {retries}/{MAX_RETRIES})...")
            await asyncio.sleep(RECONNECT_DELAY)
            ws = await connect()
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        if ws and ws.open:
            try:
                await unsubscribe(ws, market_ids)
                await ws.close()
            except Exception:
                pass

    # Summary
    print(f"\n[WS] --- Session Summary ---")
    print(f"[WS] Markets watched: {len(market_ids)}")
    print(f"[WS] Entries triggered: {len(entries_triggered)}")
    for mid in market_ids:
        idx = market_ids.index(mid)
        best = best_prices.get(mid)
        hist = price_history.get(mid, [])
        print(f"[WS]   {mid[:20]}... | best=${best:.4f if best else 'N/A'} | {len(hist)} updates | q={questions[idx][:40]}")


async def main():
    args = parse_args()
    await listen_for_edge(args)


if __name__ == "__main__":
    asyncio.run(main())
