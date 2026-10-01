# Data contracts

The routine agent writes these two JSON files. Missing sections are allowed (and expected on a bad day): the builders
validate each section independently, carry the last good copy forward, and flag it. `data/brief-data.json` and
`data/open-live.json` are the routines' live working files — they get overwritten every run, including by the
stop-hook's auto-commit, so never use them as a reference and never let `tests/` read them. `data/brief-data.example.json`
and `data/open-live.example.json` are the permanent, complete, valid examples; copy their shape from there.

## `data/brief-data.json` (Pre-Market Brief)

| Key | Section (health id) | Shape / rules |
|---|---|---|
| `indices` | `indices` (critical) | `[{id,name,q:{price,change,pct,dayLow,dayHigh,yearHigh,yearLow,ma50,ma200}}]` for SPX, DJI, RUT, VIX (FMP) plus optional `{id:"QQQ",name,proxyFor:"NDX",q:{price,change,pct},src}`. `pct` must equal `change/(price-change)*100`. VIX 5-90. |
| `macro` | `macro` | `{y10,y10prev,y2,y30,y10path,fed:{range,move,date,vote,note,sources:[{t,u}]}}` yields in %, from FMP treasury-rates. |
| `sectors`, `sectorSource` | `sectors` (critical, automated via WebSearch) | 11 distinct `{name,pct}`, cap-weighted 1-month. WebSearch-sourced — cross-check at least two sources per sector and drop the whole set if they disagree by more than ~1-2 points (do not publish a reconciled-looking number that isn't). If you can't get a confident set, omit `sectors`/`sectorSource` entirely; the builder carries the previous copy forward as `stale`. |
| `industries` | `industries` (**pinned** — interactive only, see CLAUDE.md) | 3 groups `{sector,items:[{name,pct,n}]}` × 3 items, best first. Needs Finviz group/screener pages (WebFetch) — the automated routine never writes this key; omit it entirely. Can legitimately reference a sector that is no longer in today's fresh `sectors` top-3 (a warn, not a hard failure) until the next interactive refresh. |
| `picks`, `pickWindow` | `technical` (**pinned** — interactive only, see CLAUDE.md) | 3-4 items: `tk,name,industry,sector,setup,tags,pivot,entryZone("206.20 – 207.40"),entry,stop,stopNote,target,why,trim,closes[>=15 daily closes, oldest first],cap("$251B"),avgVol,sma[3],rsi,volW,volM,px,pct[,flag],readiness`. Need `stop<entry<target`, entry inside zone, price>5, cap>=$5B. Needs OHLC history (WebFetch) — the automated routine never writes this key; omit it entirely and the builder carries the last interactive copy forward. `readiness: {composite,tightness,proximity,volumeDryUp,timeInBase}` — the four components are 0-25 each and sum to the 0-100 composite; see CLAUDE.md "Readiness scoring" for the exact formulas. The template sorts `picks` by `readiness.composite` descending itself (highest first) — write them in any order; picks with no `readiness` sort last. |
| `catalysts` | `catalysts` (**alerts** — automated, FMP + WebSearch only) | 3-4 items: `tk,name,industry,sector,ctype,cdate,cdateISO("2026-09-17"),what,sources:[{t,u:https}],cap,px,pct,topSector`. `cdateISO` must be one of the last 5 sessions. `px`/`pct` are FMP's live quote, not a computed trade level — no `entry/stop/target/closes`; those needed an OHLC tape that is WebFetch-only. |
| `bg` | (follows the tickers shown) | `{TK:{cap,what[,rev,revG,eps,epsG,epsNote?,next,nextWarn,deal:[...]]}}`. For catalyst-alert tickers only `cap`+`what` are available (FMP profile) — revenue/EPS/next-earnings need FMP's statements/earnings-calendar endpoints, both blocked on the free plan. Pinned technical picks keep their full bg from the last interactive run. `nextWarn:true` when earnings fall inside the trade window. |
| `nearmiss` | `nearmiss` | `[{tk,px,pct,why}]` 2-3 names that almost made the cut. |
| `notices`, `screenNotes`, `rejected` | (text) | Caveat bullets, screening-coverage bullets `{st:"ok|px|no",icon,html}`, and the "considered and rejected" list. Rewrite from this run's facts; never leave last week's names in them. |
| `sections` | (meta) | `{indices|macro|sectors|industries|technical|catalysts|nearmiss: {asOf:"YYYY-MM-DD", source:"..."}}` — the session date each section describes. Must not be in the future. The automated routine only ever writes `indices`, `macro`, `sectors` and `catalysts` here — never `industries`, `technical` or `nearmiss` (that's what keeps them pinned). |
| `fetchlog` | (meta) | `[{id, ok, note}]` one row per external call. |
| `meta` | (meta) | `{targetSession, lastSession}` informational; the builder recomputes the last completed session itself. |

The builder adds `health`, `watch`, `asOf`, `asOfLabel`, `builtLabel`. Do not write those.

## `data/open-live.json` (Market Open Update) — hybrid, no WebFetch (see CLAUDE.md)

```
{ "asOf": "text", "note": "text",
  "meta": {"schemaVersion": 1},
  "sections": {"indices":{"asOfUTC":"2026-09-21T13:48:00Z","source":"..."}, "futures":{...}, "quotes":{...}, "news":{...}},
  "indices": [{id,name,px,pct,prev[,proxy]}]            // SPX, DJI, RUT, VIX via FMP index-quote; QQQ via WebSearch (approximate)
  "futures": {contract:"Dec-26", asOf:"09:37 ET", rows:[{id:"ES|NQ|YM|RTY",name,px,pct,chk}], sources:[{t,u}]}   // WebSearch only - FMP has no CME index-futures data on this plan; chk = 2nd source's %
  "quotes":  {TK:{px,pct,vol,avgVol[,change][,open]}}   // every tk in the brief's `watch` groups (technical picks + catalyst-alert tickers + near-misses - not a fixed count); via FMP company/profile-symbol (which gives `price`/`changePercentage`/`change`/`volume`/`averageVolume`). Pass `change` through - the validator uses price-minus-change as an implied previous close to sanity-check `pct`, since the brief's own reference price can be weeks old for a PINNED ticker and is no longer a safe stand-in for "yesterday's close". `open` (today's regular-session print) is NOT available from that endpoint on the free plan - omit it; the page shows "open print not available yet", which is correct, not a bug.
  "notes":   {TK:"one line"},                           // optional commentary per ticker
  "news":    [{tag,top3,title,text(<=3 lines),names:[TK],sources:[{t,u:https}]}],   // WebSearch only
  "footnotes": ["optional caveats"] }
```
Each section's `asOfUTC` is when its data was read. Quotes from a previous ET date are rejected. Snapshots older than
45 minutes are flagged. The builder adds `health`, `links`, `asOf` (rewritten) and `meta.builtUTC`.

**Level check applies to technical picks only.** Catalyst-alert tickers have no `entryZone`/`entry`/`stop`/`target`
(see the brief's `catalysts` row above) - the open page shows their live quote with a `★ <ctype>` tag instead of a
zone/stop/target status. The builder's `health.sections.levels` is keyed off the brief's `technical` section alone
and normally reads `pinned` (dated from the last interactive refresh) every day - that is expected under the hybrid
design, not a failure. It only reads `failed` if the brief has no usable technical picks at all.
