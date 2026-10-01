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

## Regime score (9 checks as of 2026-10-01 — added a rates check)
Rule-based, fully transparent, computed client-side in the template from `indices` + `macro`. 8 original checks, all
from `indices`:
1-2. S&P 500 above its 50-day / 200-day average
3-4. Dow above its 50-day / 200-day average
5-6. Russell 2000 above its 50-day / 200-day average
7. VIX below 20
8. VIX below its own 50-day average

Plus the rates check (9th, automated — FMP works for this, unlike the pinned sections):
9. **10Y yield down or flat over the last 5 trading days.** Need `macro.y10hist`: exactly 5 `{date, y10}` entries,
   oldest first, ending at the current session (same source/session as `y10`/`y10prev`). Let `latest` = the last
   entry, `oneAgo` = the 2nd-to-last, `fiveAgo` = the 1st. `ratesUp = latest > oneAgo && latest > fiveAgo` (both
   conditions — a sustained move, not a one-day spike). The check PASSES when `!ratesUp` (down or flat — easing,
   supportive for equities) and FAILS when `ratesUp` (rising — a headwind). If `y10hist` is missing or malformed
   (not exactly 5 valid entries), the template **skips this check entirely** and falls back to the 8-check score —
   never guess a direction from partial data.

Thresholds scale with however many checks actually ran: 9 checks → ≥7 RISK-ON, 4-6 MIXED, ≤3 RISK-OFF; 8 checks
(rates check skipped) → ≥6 RISK-ON, 4-5 MIXED, ≤3 RISK-OFF. The page's "Regime read" card always states which
threshold band it used and whether the rates check ran.

Fetch for `y10hist` (automated routine): FMP `economics/treasury-rates` already pulled for `y10`/`y10prev` covers
this — just keep the last 5 distinct trading-day rows instead of only the latest 2, and write all 5 as `y10hist`.
No extra call needed.

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

## Readiness scoring (added 2026-10-01) — technical picks only, PINNED/interactive
Ranks the picks by breakout readiness instead of leaving them in filter-pass order. Four components, 0-25 each,
summed into a 0-100 composite. All four need real daily OHLCV (closes/highs/lows/volume) for a lookback window of
roughly the last 25 trading days (oldest first) — compute them from the same `stockanalysis.com` history you
already pull for the sparkline, via its JSON API (see "Web sources" below), not from Finviz.

First compute two reference points shared by all four components:
- `pivot` = the highest daily High in the lookback window; `pivot_idx` = its index.
- `time_in_base` = (last index) − `pivot_idx` — trading days since that high (0 if today made a new high).
- `TR` (true range) per day = `max(high-low, abs(high-prevClose), abs(low-prevClose))` (first day: just `high-low`).
- `atr14` = mean of the last 14 `TR` values; `atr_pct` = `atr14 / price * 100`.

1. **TIGHTNESS (0-25).** Take the last 10 `TR` values; `early5` = mean of the first 5, `late5` = mean of the last 5.
   `decline_pct = (early5 - late5) / early5 * 100`. Score via these anchors (piecewise-linear, clamped at the ends):
   `-20%→0, 0%→5, 20%→15, 40%→25, 60%→25`. A proxy for "declining ATR / Bollinger bandwidth" per the spec — this
   project does not compute true Bollinger Bands.
2. **PROXIMITY (0-25).** `x = (price/pivot - 1) * 100` (always ≤ 0 since pivot is the window's own high). Anchors:
   `-10%→0, -5%→10, -2%→25, +0.5%→25, +atr_pct→10, +(2*atr_pct+1)→0`. The flat 25 between -2% and +0.5% is the
   "within 1-2% of the pivot" sweet spot; beyond +1 ATR above pivot the score collapses (already broke out, missed
   it); beyond -5% below it tapers to 0 (not ready yet).
3. **VOLUME DRY-UP (0-25).** Needs `time_in_base >= 2` (otherwise score 0 — no base exists yet to measure).
   `base_vol` = mean volume of the days *after* `pivot_idx` (the consolidation). `impulse_vol` = mean volume of the
   5 days up to and including `pivot_idx` (the move that made the high). `dryup_pct = (impulse_vol - base_vol) /
   impulse_vol * 100`. Same anchors as tightness: `-20%→0, 0%→5, 20%→15, 40%→25, 60%→25`.
4. **TIME-IN-BASE (0-25).** Scored directly off `time_in_base` (trading days): anchors `0→0, 3→5, 5→25, 15→25,
   20→15, 30→5, 40→0` — the 5-15 day plateau is the "clean flag" window from the spec.

Composite = round(sum of the four). Known limitation: `pivot` is just the window's global max High, so a stock
that spiked once ~20+ sessions ago and has chopped sideways since can still look like it is "near its pivot" even
though there is no tight recent flag — the tightness/volume-dry-up components usually catch this (both score low
on a wide chop), but read the four-way breakdown, not just the composite, before trusting a pick. Re-tune the
anchors if this keeps happening on a cleaner universe.

Write `readiness: {composite, tightness, proximity, volumeDryUp, timeInBase}` on every pick (SCHEMA.md). The page
sorts by `composite` descending and shows the breakdown on each card — see the Picks section legend on the page
itself for the trader-facing explanation.

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
Work: finviz.com (groups.ashx, screener.ashx with `v=111|141|171`, quote.ashx — `groups.ashx` does NOT support a
`f=sec_X` sector filter even though the stock screener does; fetch the full ~144-row industry table and pick your
sectors' industries out of it yourself), stockanalysis.com (`/stocks/<t>/`, `/etf/qqq/`), investing.com futures
page, finance.yahoo.com live blogs, investrade.com previews.
**For daily OHLCV, use `stockanalysis.com/api/symbol/s/<TICKER>/history?range=3M&period=Daily` (their own JSON data
API), not `/stocks/<t>/history/`** — the HTML history page is paginated/JS-rendered and WebFetch's summarizer
reliably returns a stale, gapped window from it (confirmed 2026-10-01: multiple tickers came back with a ~5-week
hole between late August and the most recent day). The API URL returns clean, correctly-dated, gap-free rows.
Blocked or unreliable: cnbc.com (403 to fetch; headlines in search results are fine), bloomberg.com, tradingview.com,
barchart.com, stooq, fred (garbled dates). Do not use investing.com technical pages (numbers did not reconcile).
Re-test the automated routine against these domains occasionally (same test as 2026-10-01) — if the platform bug
is ever fixed, WebFetch could come back into the automated routine and this whole hybrid split could be revisited.

## 15-minute sector news loop (added 2026-10-01) — standalone, interactive only, NOT part of either routine
A manual tool you run yourself during a trading session, driven by Claude Code's `/loop`. It does not touch
`brief.html`, `open-update.html`, `data/brief-data.json`, or either scheduled routine — nothing it does is published.

**To start it:** in an interactive `claude` session with this repo as the working directory, paste:

    /loop 15m /news-sweep

That's the whole invocation — `/news-sweep` is a project slash command (`.claude/commands/news-sweep.md`) with the
full per-cycle instructions already written out, so `/loop` just re-runs it every 15 minutes until you stop it
(Ctrl+C, or closing the session). `/loop` with no interval lets the model self-pace instead, if you'd rather not
pin it to exactly 15 minutes.

**How it works**, split the same way as everywhere else in this repo — the agent fetches, a script renders:
- `python scripts/news_loop.py context` — reads `data/brief-data.json` (the live file, not the example) and
  prints today's top-3 sectors plus the full watchlist (technical picks + catalyst-alert tickers + near-misses,
  ~11 names). Pure local read, no network, always current with whatever the brief last wrote.
- Each cycle, the agent does 3 WebSearch sweeps (top-3-sector news, watchlist-name news, emerging-sector-strength
  — a sector *outside* today's top-3 starting to trend) and assembles candidate items as JSON.
- `python scripts/news_loop.py record --in <file>` — dedupes against everything already shown today (key =
  normalized scope+headline, exact-ish match — a reworded re-run of the same story is an accepted false negative
  for a 15-minute cadence), appends the new ones to `out/news-loop/seen-<today>.json`, and prints only what's
  genuinely new, timestamped, with ticker/sector, headline, source, a bull/bear/neutral tag, and a
  `⚠ POSSIBLE INVALIDATION` line on anything that could undercut a current pick or catalyst thesis. Prefer `--in`
  a scratch file over piping JSON through a shell — a shell pipe can mangle non-ASCII characters in headline text
  on Windows (hit this directly while testing 2026-10-01).
- The log is scoped to the current HKT date (`seen-<today>.json`, under the already-gitignored `out/`) — closing
  and reopening the session mid-day keeps the dedup state; a new trading day starts clean automatically.

## Files
- `data/brief-data.json` / `data/open-live.json` — what YOU write, every run (schema in `SCHEMA.md`). These get
  overwritten daily (and auto-committed by the stop-hook) — never treat them as a stable reference.
- `data/brief-data.example.json` / `data/open-live.example.json` — the permanent, complete schema examples (separated
  from the live files above after they collided with the test fixtures `tests/test_pipeline.py` relies on — once from
  the stop-hook's daily auto-commit, once from a manual interactive dry run). Copy their shape; never overwrite them
  from a routine run or a dry run — write to `data/brief-data.json` / `data/open-live.json` instead.
- `scripts/` — `market_time.py` (DST + holiday guard), `health.py`, `health_live.py`, `build_brief.py`, `build_open.py`,
  `news_loop.py` (the standalone 15-minute news loop — not called by either routine).
- `.claude/commands/news-sweep.md` — the `/loop`-driven news-loop prompt (`/loop 15m /news-sweep` to start it).
- `templates/` — page templates. `out/` — generated pages and reports (never committed), plus `out/news-loop/` —
  the news loop's own dedup logs, also never committed.
- `tests/` — `python tests/test_market_time.py && python tests/test_pipeline.py` must pass before any change to scripts.
  `test_pipeline.py` builds its own `brief_final` fixture rather than reading the ambient `out/last-good/brief-data.json`
  default — that path gets overwritten by any real pipeline run in the same working directory (this once silently
  poisoned the test with a different day's tickers). Don't reintroduce a dependency on that path in tests.
