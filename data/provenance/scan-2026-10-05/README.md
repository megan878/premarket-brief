# Interactive scan, Mon 5 Oct 2026 (data to the 2 Oct close)

First scan under the quality-not-quota rules (`scripts/selection.py`). Raw inputs came straight from stockanalysis.com
(industry lists, daily OHLC JSON API, statistics pages; robots.txt allows them), not through a summarizer. Sector and industry
Perf Month came from Finviz groups via WebFetch and were read twice (identical).

- `candidates.json`: every name over $5B in the 9 industries with its computed metrics, readiness (v2) and universe-filter failures (closes arrays removed).
- `selection.json`: the rule results for the 53 names that reached scoring; 0 qualified at readiness >= 55 (and 0 at 45 and 65).
- `market.json`: sector ranking and the 9-industry drill-down used.
- `catalysts.json`: priced-in components, inputs and flags for the four catalyst alerts.
