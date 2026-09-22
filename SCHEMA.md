# Data contracts

The routine agent writes these two JSON files. Missing sections are allowed (and expected on a bad day): the builders
validate each section independently, carry the last good copy forward, and flag it. `data/brief-data.json` in the repo
is a complete, valid example; copy its shape.

## `data/brief-data.json` (Pre-Market Brief)

| Key | Section (health id) | Shape / rules |
|---|---|---|
| `indices` | `indices` (critical) | `[{id,name,q:{price,change,pct,dayLow,dayHigh,yearHigh,yearLow,ma50,ma200}}]` for SPX, DJI, RUT, VIX (FMP) plus optional `{id:"QQQ",name,proxyFor:"NDX",q:{price,change,pct},src}`. `pct` must equal `change/(price-change)*100`. VIX 5-90. |
| `macro` | `macro` | `{y10,y10prev,y2,y30,y10path,fed:{range,move,date,vote,note,sources:[{t,u}]}}` yields in %, from FMP treasury-rates. |
| `sectors`, `sectorSource`, `industries` | `map` (critical) | 11 distinct `{name,pct}` (Finviz 1M, cap-weighted); `industries` = 3 groups `{sector,items:[{name,pct,n}]}` ×3 items, best first, sectors must be the top 3. |
| `picks`, `pickWindow` | `technical` | 3-4 items: `tk,name,industry,sector,setup,tags,pivot,entryZone("206.20 – 207.40"),entry,stop,stopNote,target,why,trim,closes[>=15 daily closes, oldest first],cap("$251B"),avgVol,sma[3],rsi,volW,volM,px,pct[,flag]`. Need `stop<entry<target`, entry inside zone, price>5, cap>=$5B. |
| `catalysts` | `catalysts` | 3-4 items: `tk,name,industry,sector,ctype,cdate,cdateISO("2026-09-17"),what,sources:[{t,u:https}],entryZone,entry,stop,stopNote,target,setup,risk,closes[20],catIdx,cap,avgVol,volW,volM,topSector,extra,px,pct`. `cdateISO` must be one of the last 5 sessions. |
| `bg` | (follows the tickers shown) | `{TK:{cap,rev,revG,eps,epsG,epsNote?,next,nextWarn,what,deal:[...]}}` for every pick/catalyst. `nextWarn:true` when earnings fall inside the trade window. |
| `nearmiss` | `nearmiss` | `[{tk,px,pct,why}]` 2-3 names that almost made the cut. |
| `notices`, `screenNotes`, `rejected` | (text) | Caveat bullets, screening-coverage bullets `{st:"ok|px|no",icon,html}`, and the "considered and rejected" list. Rewrite from this run's facts; never leave last week's names in them. |
| `sections` | (meta) | `{indices|macro|map|technical|catalysts|nearmiss: {asOf:"YYYY-MM-DD", source:"..."}}` — the session date each section describes. Must not be in the future. |
| `fetchlog` | (meta) | `[{id, ok, note}]` one row per external call. |
| `meta` | (meta) | `{targetSession, lastSession}` informational; the builder recomputes the last completed session itself. |

The builder adds `health`, `watch`, `asOf`, `asOfLabel`, `builtLabel`. Do not write those.

## `data/open-live.json` (Market Open Update)

```
{ "asOf": "text", "note": "text",
  "meta": {"schemaVersion": 1},
  "sections": {"indices":{"asOfUTC":"2026-09-21T13:48:00Z","source":"..."}, "futures":{...}, "quotes":{...}, "news":{...}},
  "indices": [{id,name,px,pct,prev[,proxy]}]            // SPX, QQQ, DJI, RUT, VIX; pct vs prior close
  "futures": {contract:"Dec-26", asOf:"09:37 ET", rows:[{id:"ES|NQ|YM|RTY",name,px,pct,chk}], sources:[{t,u}]}   // chk = 2nd source's %
  "quotes":  {TK:{px,pct,vol,avgVol[,open]}}            // 8 picks + 3 near misses; `open` only after 09:30 ET
  "notes":   {TK:"one line"},                           // optional commentary per ticker
  "news":    [{tag,top3,title,text(<=3 lines),names:[TK],sources:[{t,u:https}]}],
  "footnotes": ["optional caveats"] }
```
Each section's `asOfUTC` is when its data was read. Quotes from a previous ET date are rejected. Snapshots older than
45 minutes are flagged. The builder adds `health`, `links`, `asOf` (rewritten) and `meta.builtUTC`.
