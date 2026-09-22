# Pre-Market Swing Brief — pipeline rules (read first, every run)

Personal US pre-market dashboard for a Hong Kong swing trader (shares only, moderate risk, 1 day to ~5 weeks).
Two scheduled runs publish two self-contained HTML pages. **You fetch data; scripts validate and render.**

## Hard rules
1. **Never invent, estimate or "fill in" a number.** If you cannot fetch or verify something, leave that section out of
   your JSON. The builder carries the last good copy forward and shows a STALE banner. That is the correct outcome.
2. **FMP first, web second.** The FMP MCP connector is on the FREE plan (see "FMP limits"). Fall back to WebFetch / WebSearch
   of free public pages and label the source. Web pages are read by a summarizer that can garble numbers: cross-check key
   figures against a second source and drop anything that does not reconcile.
3. **Every catalyst needs an https source and a dated event inside the last 5 trading sessions.** No source = no card.
4. **Record every external call in `fetchlog`** (`{"id": "finviz.groups.sector", "ok": false, "note": "403"}`).
5. Retry a failed call once. After that use the fallback in the routine prompt, then give up on that section.
6. The scripts in `scripts/` are the only thing that writes the HTML. Never hand-edit `out/*.html`.
7. Do not touch `templates/` or `scripts/` during a routine run. If a script fails, report it in the final message.
8. Publish only if the builder exits 0. Exit 3/4 means "nothing publishable": do NOT publish, report why.

## The scan (mirror exactly; do not "improve" it)
- Universe: the top 9 industries (3 per sector) inside the top 3 US sectors by 1-month performance, cap-weighted (Finviz groups).
- Filters: price > $5, market cap > $5B, avg volume > 500K, EMA9 > price > EMA21 (compression zone), ADX(14) > 20, ADR > 2%.
- Finviz has NO EMA / ADX / ADR filters. Proxies: price above SMA20/50/200 (`ta_sma20_pa,ta_sma50_pa,ta_sma200_pa`),
  Finviz "Volatility (week/month)" for ADR, Finviz pattern flags (`ta_pattern_tlresistance | wedgeresistance | wedge | horizontal`)
  plus a read of daily OHLC. Say on the page which conditions are unverified (EMA9/EMA21, ADX).
- Market cap: Finviz's smallest bucket is `cap_midover` ($2B+); drop names under $5B using the displayed cap.
- Setups wanted: tight bull flag / pennant / compression coiling under a pivot on a good base.
- Pick 3-4 technical picks. A catalyst pick must not duplicate a technical pick.

## Trade levels (arithmetic on daily highs/lows, nothing more)
- Entry zone: just above the pattern high / pivot (technical) or the hold, retest or reclaim level (catalyst).
- Stop: the last session's low if it is 4-6% below entry, otherwise ~5% below entry (or below the gap/breakout level).
- Target: +10% from entry. R:R = reward / risk. Trim 25% at resistance on the way.
- Risk band 1.5-8%, R:R 1.4-4: the validator warns outside it.

## Catalysts (last 5 trading sessions)
Earnings beat + raised guidance, major contract/partnership/deal, analyst upgrade with a target > 15% above price,
product launch / regulatory approval. Confirm the event date AND the price reaction on the daily tape
(stockanalysis history). Sources sometimes exaggerate moves; the tape wins. Reject sell-the-news, cap < $5B, stale events.

## FMP limits (free plan, verified 2026-09-21)
Works: `indexes/index-quote` for ^GSPC ^DJI ^RUT ^VIX; `company/profile-symbol`; `economics/treasury-rates`;
`marketHours/holidays-by-exchange`; `marketPerformance` sector/industry snapshots (equal-weighted, NOT cap-weighted, prefer Finviz).
Blocked: ^NDX and QQQ, screener/search, directory, charts/price history, batch quotes, statements, earnings calendar,
technical indicators, aftermarket quotes. Use Finviz / stockanalysis.com instead.

## Web sources that work / do not
Work: finviz.com (groups.ashx, screener.ashx with `v=111|141|171`, quote.ashx), stockanalysis.com (`/stocks/<t>/`,
`/stocks/<t>/history/`, `/etf/qqq/`), investing.com futures page, finance.yahoo.com live blogs, investrade.com previews.
Blocked or unreliable: cnbc.com (403 to fetch; headlines in search results are fine), bloomberg.com, tradingview.com,
barchart.com, stooq, fred (garbled dates). Do not use investing.com technical pages (numbers did not reconcile).

## Files
- `data/brief-data.json` / `data/open-live.json` — what YOU write (schema in `SCHEMA.md`).
- `scripts/` — `market_time.py` (DST + holiday guard), `health.py`, `health_live.py`, `build_brief.py`, `build_open.py`.
- `templates/` — page templates. `out/` — generated pages and reports (never committed).
- `tests/` — `python tests/test_market_time.py && python tests/test_pipeline.py` must pass before any change to scripts.
