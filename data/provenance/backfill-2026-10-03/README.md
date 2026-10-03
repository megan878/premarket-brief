# Backfill of data/picks.json — 3 Oct 2026

`python scripts/tracker_backfill.py --ohlc ohlc.csv --fmp-last fmp-last.json --now 2026-10-03T08:00:00Z`
rebuilds the ledger from every pick set ever published (16 records, 4 sets). `report.json` is its full output
(ingest actions, readiness check, closes check, per-pick replay).

| File | What |
|---|---|
| `ohlc.csv` | Daily OHLCV, 10 Aug - 2 Oct 2026, 16 tickers. Source: `stockanalysis.com/api/symbol/s/<TK>/history?range=3M&period=Daily` read through WebFetch (a summarizer model, not raw JSON). |
| `fmp-last.json` | FMP `profile-symbol` price for each ticker on Sat 3 Oct (= the 2 Oct close): independent check of every ticker's last row. All 16 within 0.1%. |
| `report.json` | Backfill output. |

## How the OHLC was cleaned (values never altered)
The summarizer returned several tickers (MTCH, CRL, ACMR, NVT, ONTO, MPWR, DAL) in two chunks, the first newest-first and
including July rows I did not ask for, and DAL silently lacked 11-31 Aug (re-requested). `ohlc.csv` keeps rows from
10 Aug, sorted ascending, volumes rounded to integers (the API returns float noise such as `8125215.000000001`).
Every ticker then passed `tracker.validate_rows`: numeric OHLC, low <= open/close <= high, strictly increasing dates,
trading days only, no missing session from the first replayed session to 2 Oct, last date = last completed session,
last close within 3% of FMP (hard) / 0.5% (warning).

## Publish times (decide the first replayed session = first open after publishedAt)
| Set | publishedAt | Source of the time | First session replayed |
|---|---|---|---|
| 21 Sep (ANET MTCH CVX SNX) | 2026-09-22 19:55 HKT | commit 43e6df5; real publish time unknown, not later | 22 Sep |
| 1 Oct (GOOGL GH CRL ALAB) | 2026-10-01 14:43 HKT | commit dedbea3 | 1 Oct |
| 1 Oct (QRVO ACMR NVT ONTO) | 2026-10-01 22:15:33 HKT | artifact v9 version id 1790864133 (epoch s) | 2 Oct (published 45 min after the open) |
| 2 Oct (NVDA MPWR TXN DAL) | 2026-10-02 19:46:23 HKT | artifact v10 version id 1790941583 (epoch s) | 2 Oct |

## Findings
* The 1 Oct sets' published readiness reproduces under the documented v1 formula (every composite; one component of
  CRL differs by 1, unrounded 5.496 vs published 6). The 2 Oct set does not (rebuilt formula in the zip's `score.py`),
  so its `readiness` was recomputed with v1 and the published numbers are kept in `readinessAsPublished`.
* The page `closes` (sparkline) arrays of the 1 Oct QRVO set (QRVO/ACMR out of order; NVT/ONTO one wrong or dropped
  point; GOOGL missing 31 Aug) disagree with the validated OHLC: hand-assembled from unsorted WebFetch output on 1 Oct.
  Cosmetic (levels and scores came from correctly ordered rows and reproduce); not part of the ledger.
