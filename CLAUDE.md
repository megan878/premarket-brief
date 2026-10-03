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

## Readiness scoring (v1 added 2026-10-01; **v2 current since 2026-10-03**) — technical picks only, PINNED/interactive
**v2 (current) fixes one defect in v1.** v1 compared the last 5 true ranges with the 5 before them, and the 5 sessions of volume up to AND INCLUDING
the pivot day, so a single wide, heavy breakout day inside those windows scored as a contraction: MPWR scored a perfect 100 on 2 Oct (tightness 25,
dry-up 25) although its 7 base days were only 4.6% quieter than the run-up. v2 measures the **base against the run-up and excludes the pivot day**:
- run-up = the 10 sessions immediately before the pivot day; base = the sessions after the pivot day through the last session.
- **TIGHTNESS** = decline of mean true range, base vs run-up (`(runup_TR − base_TR) / runup_TR × 100`), on the same anchor ladder as v1.
- **DRY-UP** = decline of mean volume, base vs run-up, same ladder.
- Both need `time_in_base >= 2` and at least 5 run-up sessions, else they score 0 (there is no base / nothing to compare against).
- PROXIMITY and TIME-IN-BASE are unchanged. Composite = round(sum of the four), ties round up.
- Regression: MPWR (25 sessions to 1 Oct 2026) is v1 100 (25/25/25/25) and v2 **78 (7/25/21/25)**; `tests/test_tracker.py::ReadinessV2` pins it.
- **Compute it mechanically, not by hand:** `python scripts/tracker.py score --ohlc out/ohlc.json --ticker X --basis <last session> [--stop S]` prints the
  validated closes, pivot, readiness (v2), ATR14, ADR20 and the stop-floor verdict. Write `scoringVersion: "v2"` on every pick. A pick block with no
  `scoringVersion` is read as v1. Records scored under v1 stay v1 (the stats split by version); `tracker.readiness_v1` stays as the reference for them.

The text below is the **v1** definition (still what every record before 3 Oct used, and the source of the shared reference points, PROXIMITY and TIME-IN-BASE).
Ranks the picks by breakout readiness instead of leaving them in filter-pass order. Four components, 0-25 each,
summed into a 0-100 composite. All four need real daily OHLCV (closes/highs/lows/volume) for a lookback window of
roughly the last 25 trading days (oldest first) — compute them from the same `stockanalysis.com` history you
already pull for the sparkline, via its JSON API (see "Web sources" below), not from Finviz.

First compute two reference points shared by all four components:
- `pivot` = the highest daily High in the lookback window; `pivot_idx` = its index.
- `time_in_base` = (last index) − `pivot_idx` — trading days since that high (0 if today made a new high).
- `TR` (true range) per day = `max(high-low, abs(high-prevClose), abs(low-prevClose))` (first day: just `high-low`).
- `atr14` = mean of the last 14 `TR` values; `atr_pct` = `atr14 / price * 100`.

1. **TIGHTNESS (0-25) — v1.** Take the last 10 `TR` values; `early5` = mean of the first 5, `late5` = mean of the last 5.
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
- **Stop floor (since 2026-10-03): the stop must sit at least `MIN_STOP_ATR` (1.0, `scripts/health.py`) × ATR14 below the last close, or the pick is rejected.**
  Do not widen the stop to make a pick pass; drop the pick and say why (`rejected`/near-misses). The rule above can otherwise put the stop at or above the
  close when the price is more than 5% under the pivot (1 Oct: GOOGL, ALAB and QRVO were published already below their own stops). Write `atr14` (price
  units, from `tracker.py score`) on every pick: the build then rejects a pick that breaks the floor, and warns "stop above last close: TICKER" for any pick
  whose stop is at or above its last close, whether or not `atr14` is present.
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
- `python scripts/news_loop.py extract --html <file>` — once per loop (first cycle only), the agent reads the
  live BRIEF artifact with the Artifact tool and feeds the saved HTML to this command, which pulls the embedded
  `<script id="brief-data">` JSON into `out/news-loop/live-data.json`. This is the full merged dataset. Do NOT
  point this step at `data/brief-data.json` in the repo — that file is routinely just the automated routine's
  own partial, automated-only subset (indices/macro/catalysts), not the full merged picture; confirmed
  2026-10-01 when the first real automated firing left it with 0 sectors/picks/near-misses, even though the
  published artifact itself had all of them correctly merged in.
- `python scripts/news_loop.py context` — reads the extracted cache from the step above and prints today's
  top-3 sectors plus the full watchlist (technical picks + catalyst-alert tickers + near-misses, ~11 names).
  Pure local read, no network.
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

## Pick tracker (added 2026-10-03) — every technical pick recorded, followed to an outcome, summarised
Purpose: learn whether the readiness score actually predicts better trades. Code: `scripts/tracker.py` (pure, no network),
tests: `tests/test_tracker.py`. The ledger is `data/picks.json` — **append-only**: never delete or reorder records, never
hand-edit it, never change a terminal outcome. New information arrives as new fields on the existing record.

**Ledger schema** (`{"schemaVersion": 1, "records": [...]}`, one record per line). One record per pick per publish:
`id` ("2026-10-02-NVDA", HKT publish date; `-2` suffix if the same ticker is re-picked the same day) · `publishedAt` (ISO, +08:00)
· `publishTimeApproximate` (true when only an upper bound is known; the page shows "≈") · `basisSession` (last session of the OHLC
the levels came from) · `startSession` (first session replayed) · `ticker, sector, industry, setup` · `pivot, entryZoneLow,
entryZoneHigh, entry, stop, target` · `readiness {composite,tightness,proximity,volumeDryUp,timeInBase}` (null if unscored) ·
`readinessAsPublished` (only when `readiness` was recomputed) · `readinessCheck` · `scoringVersion` · `regimeLabel, regimeScore,
regimeChecks` at publish · `nextEarningsDate` · `adr20Pct` (ADR over the 20 sessions to the basis session: 100 × (mean High/Low − 1)) · `basisClose` · `levelsInvalidAtPublish` (+ `levelsInvalidReason`: stop ≥ basis close) · `tightStopAtPublish` (stop within 0.5 × ADR20% of the basis close)
· `status, triggerDate, fillPrice, chased, exitDate, exitPrice, exitReason, rMultiple, daysHeld` · `lastClose, evaluatedThrough`
· `repickDates` · `evidence` (the OHLC rows that decided the trigger and the exit) · `series` (validated closes for the sparkline:
`{from, through, closes[]}`, 25 sessions to the basis session extended to `evaluatedThrough`) · `notes`.

**Statuses.** Non-terminal: `pending`, `triggered`. Terminal: `stopped`, `target`, `time-exit` (the trade closed; `rMultiple` set),
`invalidated`, `expired`, `skipped` ("gapped past target"), `replaced`. Page chips: Waiting / Live / Stopped / Target / Time exit /
Invalidated / Expired / Skipped / Replaced, plus **Awaiting data** (a record with sessions still to evaluate and no OHLC).

**Outcome rules** (`tracker.replay`, a pure function that always replays from `startSession`, so it is idempotent):
- First replayed session = the first session whose 09:30 ET open is strictly after `publishedAt` (no lookahead). A pick published
  after the open starts the next session.
- pending → **triggered** when a session's high ≥ entry. Fill = max(open, entry). `chased` = fill > `entryZoneHigh`.
- pending → **invalidated** if the open ≤ stop (the stop broke before any trigger), or if the low ≤ stop while the high never reached
  the entry. A session that both reaches the entry and touches the stop: open ≤ stop → invalidated; open ≥ entry → triggered at the open,
  then the stop is checked the same session; opened between stop and entry → triggered at the entry, then stopped (a loss).
- pending → **skipped** ("gapped-past-target") if the trigger session opens ≥ target. Kept out of win rate and R, but counted.
- pending → **expired** after 10 evaluable sessions without a trigger (a trigger on session 10 counts).
- triggered → **stopped** when low ≤ stop (exit = min(open, stop): a gap-down fills at the open). → **target** when high ≥ target (exit =
  target). Both in one session: the stop is assumed first. → **time-exit** at the close of session 20 (the trigger session is session 1).
- `rMultiple` = (exit − fill) / (entry − stop). `daysHeld` counts the trigger session as 1.
- Rows are validated before use (`tracker.validate_rows`): numeric OHLC, low ≤ open/close ≤ high, strictly increasing dates, trading
  days only, no missing session from the first replayed session to the last completed one, last date = last completed session, last close
  within 3% of an independent FMP price (a >0.5% gap is a warning). Rows after the last completed session (a partial day) are ignored.
  A ticker that fails is excluded, logged in health, and its records stay as they were (→ "awaiting data"). Never guess a row.

**Identity, re-picks, merging.** A record's identity is (ticker, `basisSession`): a carry-forward republish of the same pick set is a
no-op (the pinned picks reappear on the page every day). A fresh scan that re-picks a ticker with new levels closes a still-pending old
record as `replaced` and opens a new one; if the old record is `triggered`, the live trade is kept and only the date is added to
`repickDates`. The ledger lives in git (`data/picks.json`) **and** is embedded in every published page (`tracker.records`); `update` and
`build_brief.py --ledger` merge the two by id: the later `evaluatedThrough` wins for non-terminal fields, a terminal outcome is immutable
and beats a non-terminal copy, two disagreeing terminal copies keep the git copy and raise a health warning.

**scoringVersion.** `SCORING_VERSION` in `tracker.py` ("v2" since 3 Oct) is the current formula and must be bumped whenever the formulas in "Readiness scoring" change; every record
stores the version its `readiness` was computed with (a pick block with no `scoringVersion` is v1) and the stats split by it. A set scored by a different formula is NOT labelled v1: it is
recomputed with the documented formula from validated OHLC (`tracker.readiness_v1`, verification only: the formula itself is unchanged) and the
published numbers are kept in `readinessAsPublished`. The 2 Oct set (NVDA/MPWR/TXN/DAL) was scored by a reconstructed formula; see
`data/provenance/2026-10-02/NOTE.md`.

**Stats** (`tracker.summarize*`, computed, never hand-written): picks, trades, closed, trigger rate, win rate (hit target, or exit > fill),
average R, expectancy, average days held; split by readiness band (<50, 50–69, ≥70, unscored), regime at publish, sector, scoringVersion and
fill type (clean / chased). A bucket with fewer than 20 closed trades shows "n too small (<20)", never a percentage. Every bucket also shows
its number of distinct publish sets: three semiconductors published together are one independent sample.
**Records published with invalid levels** (`levelsInvalidAtPublish`: stop ≥ basis-session close; a scan defect, not a market outcome) are excluded from both
views and shown on their own line ("published with invalid levels: n"). **Tight-stop records** (`tightStopAtPublish`) stay in, and the splits include a
"stop distance at publish" group. The two flags are computed at ingest (and filled in by `augment` for older records) from the validated basis close.
**Two views, side by side.** *Literal* is the record (every fill). *Disciplined* treats a fill whose open is above `entryZoneHigh` by ≥ 1× ADR20%
or more than 3% as a skipped chase, not a trade. The definition is the single config value `CHASE_RULE` in `tracker.py`
(`{'adrMultiple': 1.0, 'maxPct': 3.0}`); if `adr20Pct` is unknown only the 3% leg applies. The ledger is never changed by the disciplined view.

**Where it runs.**
- *Interactive refresh* (has WebFetch): fetch daily OHLC for every non-terminal record's ticker and for any new pick (stockanalysis.com JSON
  API, see "Web sources"), write them to `out/ohlc.json` as `{"tickers": {"NVDA": {"rows": [...], "fmpLast": <FMP price>}}}`, then
  `python scripts/tracker.py update --ledger data/picks.json --data data/brief-data.json --previous-html out/published-brief.html --ohlc out/ohlc.json`,
  then build with `--ledger data/picks.json`, publish, commit `data/picks.json` and `data/brief-data.json`, push with
  `git push origin HEAD:refs/heads/main` and confirm `git ls-remote origin refs/heads/main` equals `git rev-parse HEAD`.
- *Pre-Market Brief routine* (no price history): runs `tracker.py update` without OHLC (records picks, nothing evaluated → "awaiting data"),
  pushes the ledger before building, and passes `--tracker-warn "ledger not pushed"` to the build if the push or the hash check fails.
- **Every `closes` array on a pick card must be built with `python scripts/tracker.py closes --ohlc out/ohlc.json --ticker X --upto <basis session>`**
  (sorted by date, same validator). Hand-assembling `closes` from WebFetch output is how the 1 Oct QRVO-set sparklines got misordered. A tracked
  pick's card draws the validated ledger `series`; the build warns in health if a card's own `closes` disagree with it.
- Backfill (one-off, 3 Oct): `scripts/tracker_backfill.py` rebuilt the ledger from git history and the committed artifact-v11 block; evidence
  in `data/provenance/backfill-2026-10-03/` (OHLC, FMP cross-check, report).

## Known issues
- **Finviz screener and quote pages now refuse automated fetches** (robots.txt / 404) in interactive sessions too. The 2 Oct scan therefore built its
  universe from stockanalysis.com industry lists (see `data/provenance/2026-10-02/NOTE.md`). The scan code is deliberately unchanged here; it gets its own task.
- Interactive WebFetch of stockanalysis.com returns rows through a summarizer: chunks arrive out of order, rows can be dropped (DAL lost 11–31 Aug),
  and a 1-year range comes back corrupted. Always sort and validate (`tracker.validate_rows` / `build_closes`); use `range=3M`.
- The routine's git push is fragile: on 2 Oct it ran `git push origin HEAD` from a detached HEAD ("not a full refname") and the day's data never reached
  `origin/main`. Prompts now pin `git push origin HEAD:refs/heads/main` and verify with `git ls-remote`.
- **Fixed 2026-10-03 — stop at/above the last close.** The old stop rule (entry × 0.95 when the last low is more than 6% away) put the stop above the
  close when the price was more than 5% under the pivot: on 1 Oct GOOGL (stop 346.09 vs close 344.08), ALAB (359.10 vs 355.97) and QRVO (115.05 vs 114.48)
  were already below their own stops at publication and were invalidated in their first session. Now: the 1-ATR stop floor ("Trade levels"), the build-time
  "stop above last close" warning, and the `levelsInvalidAtPublish` flag that keeps those three out of the stats.
- **Fixed 2026-10-03 — spike-pivot artifact in tightness / dry-up (MPWR = 100).** Readiness v2 measures the base against the run-up and excludes the
  pivot day ("Readiness scoring"). Records scored under v1 keep their v1 numbers and label.

## Files
- `data/brief-data.json` / `data/open-live.json` — what YOU write, every run (schema in `SCHEMA.md`). These get
  overwritten daily (and auto-committed by the stop-hook) — never treat them as a stable reference.
- `data/brief-data.example.json` / `data/open-live.example.json` — the permanent, complete schema examples (separated
  from the live files above after they collided with the test fixtures `tests/test_pipeline.py` relies on — once from
  the stop-hook's daily auto-commit, once from a manual interactive dry run). Copy their shape; never overwrite them
  from a routine run or a dry run — write to `data/brief-data.json` / `data/open-live.json` instead.
- `scripts/` — `market_time.py` (DST + holiday guard), `health.py`, `health_live.py`, `build_brief.py`, `build_open.py`,
  `news_loop.py` (the standalone 15-minute news loop — not called by either routine; `extract` pulls the published
  artifact's embedded data into a local cache, `context` reads that cache, `record` dedupes/formats sweep output).
- `data/picks.json` — the pick ledger (append-only; schema in "Pick tracker"). `data/provenance/` — evidence behind pick sets and the backfill
  (`2026-10-02/` the 2 Oct scan's method + artifact v11 block; `backfill-2026-10-03/` the validated OHLC, FMP cross-check and report).
- `scripts/tracker.py` (outcome rules, validation, stats, `update`/`closes` commands), `scripts/tracker_backfill.py` (one-off backfill),
  `tests/test_tracker.py` (unit tests), `tests/shoot_brief.py` (Chromium render check: console errors, overflow, 1200/390 × light/dark).
- `.claude/commands/news-sweep.md` — the `/loop`-driven news-loop prompt (`/loop 15m /news-sweep` to start it).
- `templates/` — page templates. `out/` — generated pages and reports (never committed), plus `out/news-loop/` —
  the news loop's own dedup logs, also never committed.
- `tests/` — `python tests/test_market_time.py && python tests/test_pipeline.py` must pass before any change to scripts.
  `test_pipeline.py` builds its own `brief_final` fixture rather than reading the ambient `out/last-good/brief-data.json`
  default — that path gets overwritten by any real pipeline run in the same working directory (this once silently
  poisoned the test with a different day's tickers). Don't reintroduce a dependency on that path in tests.
