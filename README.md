# cbBTC breakout bot

A trading bot for the make.fun BTC race. It reads 1-minute cbBTC bars
on stdin and answers with orders on stdout, speaking
`agent-fun/bars-replay/v1`. Paper money only: 1,000 USDC start, long
only, no leverage.

## How it trades

The bot waits for a close that beats every close of the previous 60
bars. It buys only if two more checks agree: the last hour returned at
least 30 bps, and the close sits above a 240-bar EMA. One position at a
time, sized at 97% of cash, and capped so the pool impact of the order
stays near 25 bps when the pool is thin.

It sells when the trend breaks, whichever comes first:

- close drops 3% under the entry price (hard stop)
- close drops 2.5% under the highest close since entry, while still
  above the entry price (trailing stop)
- close falls 1% under the 240-bar EMA

After a sale it waits 30 bars before buying again.

## Why so few trades

A round trip costs about 30 to 50 bps once the 4 bps fee, 2 bps
slippage and pool impact are added up. Most 1-minute moves are smaller
than that, so a bot that trades often pays the pool for the privilege.
This one sits in cash until a move has already covered the cost
several times over, then stays in while the trend holds. On the frozen
dev window shipped with the kit it makes one trade and finishes up
about $26 on the $1,000 start, with a worst peak-to-trough dip of
about 2%.

## Running it

Python 3.13, standard library only:

```
python3 -P -s main.py
```

Feed it the protocol on stdin: one `hello`, one `batch` per bar, then
`done`. It answers each batch with one `reply` line. The bot keeps no
state between runs, reads no clock and no files, and uses no
randomness, so the same bars always produce the same orders.

## Files

- `main.py` is the whole bot: protocol handling and strategy in one
  file.
- `LICENSE` is MIT, as written by the kit's init.
