# Method behind the 2 Oct 2026 scan (NVDA / MPWR / TXN / DAL)

Recorded from the owner's description of the interactive session that published this set straight to the artifact.
The session's own files (raw stats for 130 names, daily OHLC for 35, `score.py`, `build.py`, the published page, the
`h/*.txt` history pages) are not in this repo: the zip never reached the session that wrote this note. If it is uploaded
later, add it alongside this file. Nothing here waits for it: the OHLC used by the tracker for these four tickers is the
validated, FMP-cross-checked series in `../backfill-2026-10-03/ohlc.csv`, which supersedes the `h/*.txt` pages.

## Universe
- stockanalysis.com industry lists, market cap > $5B: 130 names.
- Then price > SMA50 and SMA200 and 20-day average volume > 500K: 56 names.
- 7 skipped as > 20% above SMA50; OHLC fetched for 49; 35 scored.
- Finviz screener and quote pages were blocked (robots.txt / 404), which is why this scan did not use them.

## Readiness: RECONSTRUCTED, NOT v1
- pivot = max high of the last 25 sessions.
- proximity: 25 at <= 1% under the pivot, falling linearly to 0 at 8% (25 if the pivot is the current session).
- time-in-base: 5 points per day under 5 days, 25 for 5-15 days, minus 2.5 per day after 15.
- tightness = mean (H-L)/C of the base days divided by the same measure over the 10 days into the pivot
  (25 at <= 0.5x, 0 at >= 1x).
- volume dry-up = base average volume divided by the average volume of the same 10 days (25 at <= 0.6x, 0 at >= 1x).

This is a different formula from the documented v1 (CLAUDE.md "Readiness scoring"), so its scores are not comparable with
every other set. The ledger therefore stores v1 recomputed from validated OHLC in `readiness` and the numbers as published
in `readinessAsPublished`.

## Levels
- entry = pivot + 0.01
- stop = last session's low if it is 4-6% below entry, else entry x 0.95
- target = entry x 1.10

## Excluded because the history pages were bad
- RAL, ONTO: garbled.
- SANM, AME, TEL: stale, ending 25 Sep.
- IEX, ITT: stale, ending 30 Sep.

## Other files in this directory
- `artifact-v11.brief-data.json`: the `brief-data` block of artifact v11 (see `README.md`).
