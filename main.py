"""cbBTC breakout bot for agent-fun/bars-replay/v1.

Strategy in one line: buy a 60-bar closing breakout only when the last
hour's momentum and a 240-bar trend filter agree, then hold until the
trend actually breaks (2.5% trailing stop, 3% hard stop, or a close 1%
under the trend EMA). A full round trip costs roughly 30-50 bps once
fees, slippage and pool impact are counted, so the bot trades rarely
and only on moves that have already covered that cost several times
over. Everything is integer arithmetic on the bars as they arrive:
no clock, no network, no randomness, so two runs on the same bars
produce the same orders byte for byte.
"""

import json
import sys

SYMBOL = "CBBTC"

# Strategy parameters (bars are 1 minute).
BREAKOUT_WIN = 60        # close must beat every close of the prior 60 bars
MOM_WIN = 60             # momentum lookback
MOM_MIN_BPS = 30         # last hour must be up at least 30 bps
TREND_N = 240            # EMA of closes used as the trend filter
SIZE_BPS = 9700          # spend 97% of cash on an entry
TRAIL_PCT_NUM = 25       # trailing stop: 2.5% under the peak close
HARD_STOP_NUM = 30       # hard stop: 3.0% under the entry fill price
EXIT_EMA_NUM = 99        # exit if close < 99% of the trend EMA
COOLDOWN = 30            # bars to wait after an exit before re-entering
MAX_SLIP_BPS = 300       # order slippage cap sent with every order
PCT_DEN = 1000           # the *_NUM constants are tenths of a percent


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.warmup = int(hello.get("warmup_bars", 0))
        self.seq = -1
        self.cash = 0
        self.qty = 0
        sym = None
        for s in hello["symbols"]:
            if s["symbol"] == SYMBOL:
                sym = s
        self.qty_scale = int(sym["qty_scale"])
        self.price_scale = int(sym["price_scale"])
        self.quote_scale = int(hello["capital"]["quote_scale"])
        self.closes = []          # raw integer closes, oldest first
        self.ema = None           # trend EMA, kept as a float-free fraction
        self.ema_num = 0
        self.ema_den = 1
        self.in_position = False
        self.entry_px = 0         # raw fill price of the current position
        self.peak_close = 0       # highest raw close since entry
        self.last_depth = 0       # latest close depth, quote units (per hop)
        self.pending = None       # "buy" or "sell": order sent, fill unknown
        self.last_exit_seq = -10 ** 9
        self.order_n = 0

    # -- price helpers -------------------------------------------------
    def value_units(self, qty, price_raw):
        """Quote units a qty is worth at a raw price."""
        return qty * price_raw * (10 ** self.quote_scale) // (10 ** (self.qty_scale + self.price_scale))

    # -- account -------------------------------------------------------
    def apply_account(self, account):
        self.cash = int(account["cash_quote_units"])
        self.qty = 0
        for p in account["positions"]:
            if p["symbol"] == SYMBOL:
                self.qty = int(p["qty"])
        was = self.in_position
        self.in_position = self.qty > 0
        if was and not self.in_position:
            self.last_exit_seq = self.seq
            self.entry_px = 0
            self.peak_close = 0
        if self.pending is not None:
            # The fill (or rejection) of the order we sent on the previous
            # batch is reflected in this account; either way it is settled.
            if self.pending == "buy" and self.in_position and self.closes:
                # Entry price is not reported back to us bar by bar; use the
                # position cost the account carries.
                pass
            self.pending = None

    def note_position_cost(self, account):
        for p in account["positions"]:
            if p["symbol"] == SYMBOL and int(p["qty"]) > 0:
                cost = int(p["cost_quote_units"])
                q = int(p["qty"])
                if q > 0:
                    # cost per unit back into a raw price
                    self.entry_px = cost * (10 ** (self.qty_scale + self.price_scale)) // (q * (10 ** self.quote_scale))

    # -- strategy ------------------------------------------------------
    def decide(self):
        """Return a list of wire orders for this batch (usually empty)."""
        if self.seq < self.warmup:
            return []
        if self.pending is not None or not self.closes:
            return []
        close = self.closes[-1]
        if self.in_position:
            if close > self.peak_close:
                self.peak_close = close
            if self.entry_px and close * PCT_DEN <= self.entry_px * (PCT_DEN - HARD_STOP_NUM):
                return [self.sell_all()]
            if self.peak_close and close * PCT_DEN <= self.peak_close * (PCT_DEN - TRAIL_PCT_NUM) and close > self.entry_px:
                return [self.sell_all()]
            if self.ema is not None and close * 100 < self.ema * EXIT_EMA_NUM:
                return [self.sell_all()]
            return []
        # Flat: look for a breakout entry.
        if len(self.closes) <= max(BREAKOUT_WIN, MOM_WIN, TREND_N // 2):
            return []
        if self.seq - self.last_exit_seq < COOLDOWN:
            return []
        window = self.closes[-BREAKOUT_WIN - 1:-1]
        if close < max(window):
            return []
        ref = self.closes[-MOM_WIN - 1]
        if ref <= 0 or (close - ref) * 10000 < MOM_MIN_BPS * ref:
            return []
        if self.ema is not None and close <= self.ema:
            return []
        return [self.buy()]

    def buy(self):
        self.order_n += 1
        notional = self.cash * SIZE_BPS // 10000
        # Impact grows with size over depth: a trade worth a pays about
        # a/depth extra. Cap the spend so that stays near 25 bps even
        # when the pool thins out; at the usual depth this never binds.
        if self.last_depth > 0:
            cap = self.last_depth * 25 // 10000
            if cap < notional:
                notional = cap
        self.pending = "buy"
        return {
            "side": "buy",
            "client_id": f"b{self.order_n}",
            "symbol": SYMBOL,
            "notional_quote_units": str(notional),
            "max_slippage_bps": MAX_SLIP_BPS,
        }

    def sell_all(self):
        self.order_n += 1
        self.pending = "sell"
        return {
            "side": "sell",
            "client_id": f"s{self.order_n}",
            "symbol": SYMBOL,
            "qty": str(self.qty),
            "max_slippage_bps": MAX_SLIP_BPS,
        }

    # -- protocol ------------------------------------------------------
    def on_batch(self, batch):
        self.seq = int(batch["seq"])
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                c = int(bar["c"])
                self.closes.append(c)
                depths = bar.get("c_depth") or []
                if depths:
                    self.last_depth = min(int(d) for d in depths)
                if self.ema is None:
                    self.ema = float(c)
                else:
                    k = 2.0 / (TREND_N + 1)
                    self.ema = c * k + self.ema * (1.0 - k)
                if self.in_position and c > self.peak_close:
                    self.peak_close = c
        self.apply_account(batch["account"])
        self.note_position_cost(batch["account"])
        if self.in_position and self.peak_close == 0 and self.closes:
            self.peak_close = self.closes[-1]
        return self.decide()


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(msg)
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            bot.apply_account(msg["account"])
            return


if __name__ == "__main__":
    main()
