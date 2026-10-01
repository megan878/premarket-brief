# Pre-Market Swing Brief — pipeline rules (read first, every run)

Personal US pre-market dashboard for a Hong Kong swing trader (shares only, moderate risk, 1 day to ~5 weeks).
Two scheduled runs publish two self-contained HTML pages. **You fetch data; scripts validate and render.**

## WebFetch is broken in the cloud sandbox — HYBRID architecture (decided 2026-10-01)
Confirmed by direct test: WebFetch to finviz.com and stockanalysis.com returns `EGRESS_BLOCKED` in the automated
cloud routine under Trusted, Custom (explicit allowlist) AND Full network access alike — a platform bug, not a
config problem (matches multiple open `anthropics/claude-code` GitHub issues on the egress proxy). **The automated
"Pre-Market Brief" routine's `allowed_tools` does not include WebFetch at all. Do not add it back without re-testing.**
Only FMP (MCP connector, bypasses the proxy) and WebSearch (also bypasses it) work automatically.

This splits the page into two kinds of section:
- **Automated, fresh every run** (FMP + WebSearch only): index/VIX quotes, rates, regime score, sector rankings,
  catalyst **alerts** (news only — ticker, what happened, date, 2 sources, FMP price/cap; no computed trade levels),
  earnings-calendar flags. Written fresh into `data/brief-data.json` every run.
- **Pinned, interactive-only**: the technical scan (`picks`/`pickWindow`), the industry drill-down (`industries`,
  separate from the automated `sectors` ranking) and `nearmiss` (leftovers of that same scan) all need Finviz
  (WebFetch) for OHLC history, EMA/ADX/ADR proxies and the pattern screener. The automated routine **must never
  write these keys** — just omit them entirely. `health.py` then carries the last interactive copy forward
  verbatim, dated, labelled `PINNED` (not `STALE`) in the page. Refresh them yourself by running an interactive
  `claude` session with WebFetch available (the technical-scan steps that used to be part of this routine) whenever
  they feel stale, then let the next automated run carry the new copy forward.
- Why not fix the proxy instead: it is a platform bug outside this project's control (see the GitHub issues cited
  above); re-test it occasionally, but do not block the dashboard on it.

## Market Open Update — same hybrid treatment (decided 2026-10-01)
The open-update routine gets the identical no-WebFetch rule, with its own fetch plan:
- **Indices/VIX**: FMP `indexes/index-quote` for ^GSPC ^DJI ^RUT ^VIX. QQQ: WebSearch (FMP blocks NDX/QQQ on the free
  plan), labelled approximate, same as the brief.
- **Stock quotes** (technical picks + catalyst-alert tickers + near-misses — however many the brief currently lists,
  not a fixed "8"): FMP `company/profile-symbol` per ticker gives a live `price`/`changePercentage`/`volume`/
  `averageVolume` — use those for `px`/`pct`/`vol`/`avgVol`. It does **not** expose today's regular-session opening
  print, so `open` stays omitted; the page already renders "open print not available yet" when `open` is missing —
  that is the correct degraded state, not a bug to work around.
- **Futures (ES/NQ/YM/RTY)**: FMP has no CME index-futures data on this plan at all (confirmed: both `quote` and
  `commodity` single-symbol endpoints return ACCESS DENIED) — go straight to WebSearch, cross-checking two sources
  per contract same as before (`chk` field).
- **Overnight news**: WebSearch only (was WebFetch + WebSearch). Same sourcing bar: https links, dated, tagged to a
  top-3 sector or one of the watchlist names.
- **Level check applies to technical picks only.** Catalyst alerts carry no entry/stop/target (see above) — the open
  page shows their live price/% only, tagged `★ <ctype>` with the brief's one-line `what`, not a zone/stop/target
  status. `health_live.py`'s `levels` section is keyed off the brief's `technical` section specifically (not the
  whole brief), and reads `PINNED` — not `STALE` or a failure — every single day, since technical picks are always
  pinned under the hybrid design. That's expected; only flag it if `levels` reads `FAILED` (no technical picks at all).

## Hard rules
1. **Never invent, estimate or "fill in" a number.** If you cannot fetch or verify something, leave that section out of
   your JSON. The builder carries the last good copy forward and shows a STALE (or PINNED, for map/technical) banner.
   That is the correct outcome.
2. **FMP first, WebSearch second. No WebFetch in the automated routine** (see above). Web-search results are read by
   a summarizer that can garble numbers: cross-check key figures against a second source and drop anything that does
   not reconcile.
3. **Every catalyst needs an https source and a dated event inside the last 5 trading sessions.** No source = no card.
   Catalyst alerts carry FMP's live price/cap, not a computed entry/stop/target — that needs an OHLC tape that only
   an interactive WebFetch session can read.
4. **Record every external call in `fetchlog`** (`{"id": "fmp.profile.AAPL", "ok": false, "note": "404"}`).
5. Retry a failed call once. After that use the fallback in the routine prompt, then give up on that section.
6. The scripts in `scripts/` are the only thing that writes the HTML. Never hand-edit `out/*.html`.
7. Do not touch `templates/` or `scripts/` during a routine run. If a script fails, report it in the final message.
8. Publish only if the builder exits 0. Exit 3/4 means "nothing publishable": do NOT publish, report why.

## The scan (mirror exactly; do not "improve" it) — PINNED, interactive refresh only
- Universe: the top 9 industries (3 per sector) inside the top 3 US sectors by 1-month performance, cap-weighted (Finviz groups).
- Filters: price > $5, market cap > $5B, avg volume > 500K, EMA9 > price > EMA21 (compression zone), ADX(14) > 20, ADR > 2%.
- Finviz has NO EMA / ADX / ADR filters. Proxies: price above SMA20/50/200 (`ta_sma20_pa,ta_sma50_pa,ta_sma200_pa`),
  Finviz "Volatility (week/month)" for ADR, Finviz pattern flags (`ta_pattern_tlresistance | wedgeresistance | wedge | horizontal`)
  plus a read of daily OHLC. Say on the page which conditions are unverified (EMA9/EMA21, ADX).
- Market cap: Finviz's smallest bucket is `cap_midover` ($2B+); drop names under $5B using the displayed cap.
- Setups wanted: tight bull flag / pennant / compression coiling under a pivot on a good base.
- Pick 3-4 technical picks. A catalyst pick must not duplicate a technical pick.

## Trade levels (arithmetic on daily highs/lows, nothing more) — technical picks only, PINNED/interactive
- Entry zone: just above the pattern high / pivot.
- Stop: the last session's low if it is 4-6% below entry, otherwise ~5% below entry (or below the gap/breakout level).
- Target: +10% from entry. R:R = reward / risk. Trim 25% at resistance on the way.
- Risk band 1.5-8%, R:R 1.4-4: the validator warns outside it.
- Catalyst **alerts** carry no computed levels at all (see below) — this section no longer applies to them.

## Catalyst alerts (automated, last 5 trading sessions, FMP + WebSearch only)
Earnings beat + raised guidance, major contract/partnership/deal, analyst upgrade with a target > 15% above price,
product launch / regulatory approval. Confirm the event date with at least two independent web-search sources.
Record FMP's live `price`/`changesPercentage`/`mktCap` for the ticker (`company/profile-symbol`) — this is a quote,
not a verified reaction on the daily tape, since reading that tape needs WebFetch (stockanalysis.com), which the
automated routine does not have. Say so on the page. Reject sell-the-news, cap < $5B, stale events, or anything
duplicating a pinned technical pick. No entry/stop/target/closes fields — just `tk,name,industry,sector,ctype,
cdate,cdateISO,what,sources,cap,px,pct,topSector` (see SCHEMA.md). If you want trade-ready levels for a catalyst
name, do that by hand in an interactive session with WebFetch, the same way as the technical scan.

## FMP limits (free plan, verified 2026-09-21)
Works: `indexes/index-quote` for ^GSPC ^DJI ^RUT ^VIX; `company/profile-symbol` (includes a live `price`/
`changesPercentage`/`mktCap`/`description` — this is what powers catalyst alerts and the light `bg` snapshot);
`economics/treasury-rates`; `marketHours/holidays-by-exchange`; `marketPerformance` sector/industry snapshots
(equal-weighted, NOT cap-weighted — use WebSearch for cap-weighted 1-month sector rankings instead).
Blocked: ^NDX and QQQ, screener/search, directory, charts/price history, batch quotes, statements, earnings
calendar, technical indicators, aftermarket quotes. None of these have an automated fallback any more (WebFetch is
out) — `nextWarn`/earnings-calendar flags and revenue/EPS come from WebSearch when findable, otherwise omitted.

## Web sources — INTERACTIVE SESSIONS ONLY (neither automated routine has WebFetch)
When you are refreshing the pinned technical scan / industry map by hand with WebFetch available (the open-update
routine has no pinned sections of its own — it has no WebFetch fetch plan to fall back to at all, only FMP + WebSearch):
Work: finviz.com (groups.ashx, screener.ashx with `v=111|141|171`, quote.ashx), stockanalysis.com (`/stocks/<t>/`,
`/stocks/<t>/history/`, `/etf/qqq/`), investing.com futures page, finance.yahoo.com live blogs, investrade.com previews.
Blocked or unreliable: cnbc.com (403 to fetch; headlines in search results are fine), bloomberg.com, tradingview.com,
barchart.com, stooq, fred (garbled dates). Do not use investing.com technical pages (numbers did not reconcile).
Re-test the automated routine against these domains occasionally (same test as 2026-10-01) — if the platform bug
is ever fixed, WebFetch could come back into the automated routine and this whole hybrid split could be revisited.

## Files
- `data/brief-data.json` / `data/open-live.json` — what YOU write, every run (schema in `SCHEMA.md`). These get
  overwritten daily (and auto-committed by the stop-hook) — never treat them as a stable reference.
- `data/brief-data.example.json` — the permanent, complete schema example (separated from the live file above after
  the two collided: the stop-hook's daily auto-commit kept overwriting the fixture `tests/test_pipeline.py` relies on).
  Copy its shape; never overwrite it from a routine run.
- `scripts/` — `market_time.py` (DST + holiday guard), `health.py`, `health_live.py`, `build_brief.py`, `build_open.py`.
- `templates/` — page templates. `out/` — generated pages and reports (never committed).
- `tests/` — `python tests/test_market_time.py && python tests/test_pipeline.py` must pass before any change to scripts.
  `test_pipeline.py` builds its own `brief_final` fixture rather than reading the ambient `out/last-good/brief-data.json`
  default — that path gets overwritten by any real pipeline run in the same working directory (this once silently
  poisoned the test with a different day's tickers). Don't reintroduce a dependency on that path in tests.
