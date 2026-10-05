Run a full "Reset scan" for the Pre-Market Swing Brief (this repo). Follow CLAUDE.md sections "Reset scan", "Quality selection, max 10", "Strength flags" and "Catalyst priced-in score".

1. State the clear/keep list from `scripts/reset_policy.py`. Never touch `data/picks.json` records, provenance files, the Catalyst Playbook / Reference section, regime rules, config or design.
2. Sector ranking + industry drill-down: Finviz groups (Perf Month); read the table twice. If Finviz refuses, say so and keep the last good data marked STALE.
3. `python scripts/scan_fetch.py lists --industries "<the 9 industries>" --out out/scan/lists.json`, then `ohlc`, then `python scripts/scan_build.py analyse|stats|select`.
   A name with no earnings date from stockanalysis gets a WebSearch second source before it is rejected as "earnings date unverified"; log those names.
4. Catalyst alerts: keep those still inside their 5-day window and re-price them (`python scripts/scan_catalysts.py ...`); drop older ones.
5. `python scripts/scan_assemble.py ...`, then `python scripts/build_brief.py ... --ledger data/picks.json`; the build runs the normal validation and health checks.
6. Report: qualifying count, the count at readiness 45 / 55 / 65 and the v2 score distribution, flags, priced-in scores, names rejected for unverified earnings. Run the test files in `tests/` and `tests/shoot_brief.py`.
7. ASK before publishing or pushing. Push with `git push origin HEAD:refs/heads/main` and verify with `git ls-remote`.
