# Deploy v1 — plan for confirmation (nothing is activated)

Status: routines **not created**, artifacts **not created**, nothing pushed anywhere. Everything below is drafted and locally tested.

## 0. Four corrections to the brief that shape the design

1. **`build_open.py` cannot pull prices or news.** FMP, WebSearch and WebFetch are agent tools; a Python script cannot call them.
   So the routine's *agent* fetches and writes `data/open-live.json`, then `build_open.py` validates and renders. Same for the brief.
2. **A cloud run is a fresh checkout with no local files, and this folder is not a git repo.** The routines need a GitHub repo containing this
   `brief-pipeline/` folder (not the ChartDesk files that share the parent directory). Nothing persists between runs, so **the published artifact is the
   state store**: each run reads the last published page and recovers its data block as the "previous good copy".
3. **Cron is UTC-only with a 1-hour minimum interval.** No timezone field. HKT has no DST; New York does (section 3).
4. **Free feeds have no pre-market prices or volume.** At 21:25 HKT (09:25 ET) the page can show futures, overnight news and levels, but stock
   opening prints only exist after 09:30 ET. The page detects this and labels itself **PRE-OPEN** or **AFTER THE OPEN**; a run that takes ~8 minutes will
   partly land after the open. See decision D3.

## 1. Every external call, and what can fail

### Routine 1 — Pre-Market Brief (20:00 HKT, about 95-120 calls)

| # | Call | Tool | Calls | Feeds | Likely failure | Fallback (in order) |
|---|---|---|---|---|---|---|
| A1 | Index quotes ^GSPC ^DJI ^RUT ^VIX | FMP `index-quote` | 4 | index strip, regime score | plan change, timeout | retry → stockanalysis/Yahoo index page → **previous copy, STALE** |
| A2 | QQQ (NDX proxy) | Finviz quote + stockanalysis ETF page | 2 | QQQ tile | 403, JS page | tile omitted + warning |
| B1 | Treasury curve | FMP `treasury-rates` | 1 | rates tile/line | plan change | previous copy, STALE |
| B2 | Fed decision check | WebSearch (only if FOMC ≤ 7 days ago) | 0-2 | rates line | outlets disagree, paywall | previous `fed` block, STALE |
| C1 | Sector 1M ranking | Finviz groups (sector) | 1 | sector bars, top-3 | bot block / layout change | previous map, STALE |
| C2 | Industry 1M ranking | Finviz groups (industry) | 1 | industry slots | same | previous map, STALE |
| C3 | Industry to sector check | Finviz screener `sec+ind` | 9 | universe integrity | empty result = bad pairing | drop that industry; re-rank |
| D1 | Universe lists | Finviz screener `v=111` | 9-12 | candidate names, caps | 403, truncated table | that industry skipped, warning |
| D2 | Technical columns | Finviz screener `v=171` | 9-12 | SMA distances, ATR, RSI | same | as D1 |
| D3 | Pattern flags | Finviz screener, 3 sectors x 4 patterns | 12 | flag/pennant detection | zero rows can mean "none", not "failed" | treat as no-flag, note it |
| D4 | Volatility (ADR proxy) | Finviz `v=141` | 1-2 | ADR proxy chip | 403 | chip shows "n/a" |
| D5 | Daily OHLC for finalists | stockanalysis history | 8-10 | pivots, stops, sparklines | missing today's row, cache | pick dropped (levels need OHLC) |
| F1 | Company profile | FMP `profile-symbol` | 8-11 | market cap, description | plan change | strip shows what it has |
| F2 | Revenue, EPS, next earnings | stockanalysis overview | 8 | info strip | layout change | field shows "n/a", never guessed |
| F3 | Earnings date confirmation | WebSearch | 1-3 | earnings-risk flag | conflicting dates | show date + "unconfirmed" |
| E1 | Catalyst discovery | WebSearch, morning previews, live blogs | 8-10 | catalyst candidates | CNBC/Bloomberg 403, off-topic hits | fewer candidates |
| E2 | Catalyst verification | WebSearch + stockanalysis + Finviz `t=` | 15-20 | sourced facts, tape check | summarizer garbles numbers | **candidate rejected** |
| S1 | Recover last page | Artifact `read` | 1 | carry-forward state | first run, outage | continue with no fallback |
| S2 | Publish | Artifact `publish` | 1-3 | the page | permission, size, conflict | retry x2, then report failure |

### Routine 2 — Market Open Update (21:25 HKT summer / 22:25 HKT winter, about 40-45 calls)

| # | Call | Tool | Calls | Feeds | Likely failure | Fallback |
|---|---|---|---|---|---|---|
| G0 | Run/skip guard | local script | 0 | DST + holiday decision | none (pure code) | n/a |
| S1 | Read brief (levels) | Artifact `read` | 1 | trade levels | artifact unreadable | **STOP: no levels, no page** (previous open page stays) |
| S1b | Read previous open page | Artifact `read` | 1 | same-day fallback | first run | none needed |
| P1 | Stock quotes | FMP `profile-symbol` | 11 | live price, volume | timeout, plan change | that ticker shows "No live price" |
| P2 | Opening prints | stockanalysis overview | 11 (after 09:30 ET only) | gap, "opened above zone" | stale cache, row missing | gap shown as "not available yet" |
| P3 | Index quotes | FMP `index-quote` | 4 | regime tiles | as A1 | earlier snapshot same day, else "unavailable" |
| P4 | QQQ | stockanalysis ETF | 1 | NDX proxy | different prior close (seen) | measured against the brief's close; footnote |
| P5 | Futures ES NQ YM RTY | Investing.com + Yahoo | 2 | open read | delayed/blocked | earlier snapshot, else "unavailable" |
| N1 | Overnight news | WebSearch + WebFetch | 8-12 | news cards | nothing found, 403 | "no news found" card; unsourced items dropped |
| S2 | Publish | Artifact `publish` | 1-3 | the page | as brief | retry x2, report |

### Non-call failure modes (also handled or reported)
Routine fails to start (repo clone/permission, connector detached, environment missing) — visible only in the run log (`list_runs` / `get_run_log`);
`allowed_tools` missing WebSearch/Artifact/MCP names; the cron fires but the guard says skip (normal, once a day for routine 2); the agent runs out of time mid-fetch
(sections it did not reach are simply absent, so they carry forward flagged); a script bug (blocked by 23 scenario tests + the "never overwrite on exit 3" rule).

## 2. Error handling (built and tested locally)

Principle: **never crash, never overwrite good data with garbage, never show old data silently.**

| Layer | Behaviour |
|---|---|
| Per-call | Retry once, then the fallback in the table. Every call is logged in `fetchlog`. |
| Validation (`health.py`, `health_live.py`) | Each section is checked on its own: numbers finite and plausible, price/change/% consistent, exactly 11 sectors and 3x3 industries, stop < entry < target, price > $5, cap >= $5B, catalyst dated in the last 5 sessions with an https source, futures cross-checked (0.25 pt), no as-of dates in the future, quotes belong to today's ET session. |
| Carry-forward | A failed or invalid section is replaced by the **previous copy of that section only**, tagged with its real as-of date. Bad items (one pick, one quote, one news card) are dropped, not the whole section. Catalysts older than 5 sessions expire even if carried. |
| On the page | An amber banner names each stale section and why; a red one if the update failed. `STALE · as of <date>` chips sit on the affected headers; missing quotes show "No live price"; empty sections show an explicit empty state instead of vanishing. A pre-open build says PRE-OPEN and hides gaps it does not have. |
| Refusal | Brief: exit 3 if indices or the sector map have neither fresh nor previous data. Open page: exit 3 if there are no indices AND no quotes; exit 4 if no levels. **Exit != 0 means "do not publish"**, so the last good page stays up. |
| Levels dependency | The open page states how old its levels are and inherits the brief's own stale flags. |
| Observability | `out/build-report.json`, `out/open-build-report.json`, the health block embedded in each page, and the routine run log. There is **no push notification**; a failed day is visible on the pinned page itself (banner) or, if publishing failed, only in the run log (decision D6). |

**Test evidence** (`python tests/test_pipeline.py`, 23 scenarios, all pass): sector source down; garbage index quotes; total fetch failure (all sections stale); no data and no history (refuses); one bad pick; expired catalysts; future-dated data; old data; unsourced catalyst/news; small-cap intruder; garbage + missing quotes; futures down (with and without an earlier snapshot); everything down; pre-open; old snapshot; yesterday's quotes; old/degraded brief; no brief. The degraded pages were also opened in a browser to confirm no JavaScript errors (this caught one real bug: a missing catalyst section vanished silently; fixed).

**Not covered by tests** (needs a live dry run): whether cloud sessions can use WebSearch, WebFetch, Artifact and the FMP connector under `allowed_tools`; how long a full brief run takes; Finviz rate-limiting from cloud IPs.

## 3. DST logic

Facts: HKT is UTC+8 all year. New York is UTC-4 (EDT) from the 2nd Sunday of March to the 1st Sunday of November, UTC-5 (EST) otherwise. The US open (09:30 ET) is therefore:

| Season | US open | 5 min before | UTC cron time | 2026-27 dates |
|---|---|---|---|---|
| EDT (summer) | 21:30 HKT / 13:30 UTC | **21:25 HKT** | 13:25 | until Sun 1 Nov 2026, again from Sun 14 Mar 2027 |
| EST (winter) | 22:30 HKT / 14:30 UTC | **22:25 HKT** | 14:25 | Sun 1 Nov 2026 to Sun 14 Mar 2027 |

Design:
1. **Routine 2 uses one cron that fires at both times:** `25 13,14 * * 1-5` (weekdays, UTC). Two firings per day, one is always wrong.
2. **A guard decides.** First action of the routine: `python scripts/market_time.py guard open-update`. It converts "now" to ET (own DST rule, no timezone database needed), checks NYSE holidays, and runs only if the US open is between -10 and +20 minutes away. Otherwise exit code 10 and the agent stops in seconds. Result: exactly one real run per trading day, no cron edits twice a year.
3. **Routine 1 needs no DST logic for timing:** `0 12 * * 1-5` = 20:00 HKT Mon-Fri (12:00 UTC has the same weekday as 20:00 HKT). It still calls the guard so it skips weekends-in-ET and US holidays. 20:00 HKT is 08:00 ET in summer, 07:00 ET in winter: before the open both ways.
4. **Holidays:** NYSE full closures 2026-27 are hard-coded and match FMP's holiday calendar exactly (test). Early-close days (27 Nov, 24 Dec 2026) still open at 09:30. After 2027 the guard runs anyway and says the table is out of date.
5. **Last completed session** (used to date every section and to expire catalysts) is computed the same way, so a Monday run correctly targets Friday and a post-holiday run targets the session before it.
6. **Verified:** my DST rule equals the real tz database for every hour of 2026-2027; both slots tested in summer and winter; the transition instants (1 Nov 2026 06:00 UTC, 14 Mar 2027 07:00 UTC); weekends; Labor Day and Thanksgiving. Cost of the two-firing design: one no-op cloud session per day (seconds).

## 4. Routine configs (drafts, `enabled: false`)

| | Pre-Market Brief | Market Open Update |
|---|---|---|
| Cron (UTC) | `0 12 * * 1-5` = 20:00 HKT Mon-Fri | `25 13,14 * * 1-5` = 21:25 HKT (summer) / 22:25 HKT (winter), guard picks one |
| Model | claude-sonnet-5 (see D7) | claude-sonnet-5 |
| Environment | `env_011GEddWgLHigXXVsZaD6hRV` (account default, anthropic_cloud) | same |
| Repo | `<GITHUB_REPO_URL>` (folder `brief-pipeline/` as repo root) | same |
| Connector | FMP: uuid `01a07ad1-c7b9-4104-a24b-5718bb62e41a` (inferred from this session's tool prefix, **URL to be confirmed**) | same |
| allowed_tools | Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Artifact (FMP tool names to verify in the dry run) | same |
| Artifact | writes `<BRIEF_ARTIFACT_URL>` | reads BRIEF, writes `<OPEN_ARTIFACT_URL>` |
| Prompt | `routines/pre-market-brief.prompt.md` (4.5k chars) | `routines/market-open-update.prompt.md` (2.8k chars) |

Files: `routines/pre-market-brief.config.json`, `routines/market-open-update.config.json` (full `create` bodies, generated by `routines/make_config.py`).

## 5. What I need from you (decisions)

- **D1 Repo.** Create a GitHub repo containing only `brief-pipeline/` (suggest `premarket-brief`, private). I will not push anything without your say-so. Give me the URL or tell me to create it with `gh` (needs your login).
- **D2 FMP connector.** Confirm it is connected on claude.ai and paste its MCP URL (claude.ai/customize/connectors). Routines can only use claude.ai connectors, not locally configured MCP servers.
- **D3 Run time of routine 2.** Keep 21:25 (page will often be PRE-OPEN: futures, news, levels, no opening prints) or move to ~21:40 (real opening prints, but the guard window and cron change: `40 13,14`)? My recommendation: 21:35-21:40. It costs 10 minutes and turns the gap/volume columns from "not yet" into real numbers.
- **D4 Artifacts.** OK to create two private placeholder artifacts (brief, open) to get the two fixed URLs? Publishing later updates them in place. They stay private unless you share them.
- **D5 Weekdays only.** Both cron lines are Mon-Fri; the guard also skips US holidays. Confirm you do not want a Sunday-evening brief for Monday.
- **D6 Failure alerts.** Today a failure shows as a banner on the pinned page. Want an active alert (e.g. email/chat via a connector) when a run ends "not published"?
- **D7 Model.** Sonnet is the default. The brief does ~100 research calls with judgement; say if you want a stronger model for routine 1 only.

## 6. Activation sequence (after you confirm)

1. Create the repo, push `brief-pipeline/` (excluding `out/`), confirm `python tests/test_market_time.py && python tests/test_pipeline.py` pass in the cloud checkout.
2. Create the two placeholder artifacts and record their URLs; regenerate configs with `make_config.py`.
3. `create` both routines **disabled**. `run` routine 1 once by hand, read the run log (`get_run_log`) and the published page; fix tool-access issues.
4. `run` routine 2 once by hand at a chosen time; confirm the guard, the phase label and the levels from the published brief.
5. Enable both. Watch the first week: check `list_runs` daily; the first no-op firing of routine 2 should end in seconds with "wrong DST slot".
6. On Sun 1 Nov 2026 the US leaves daylight time: nothing to change, but check Monday 2 Nov's run landed at 22:25 HKT.

Confirming this plan authorises unattended publishing of two private artifacts at the two fixed URLs, and nothing else.
