Run one sector/watchlist news sweep for the Pre-Market Swing Brief. This is a standalone interactive tool —
not part of either scheduled routine, and nothing here gets published anywhere.

1. Run `python scripts/news_loop.py context` and read its output: today's top-3 sectors and the ~11-name watchlist
   (technical picks, catalyst-alert tickers, near-misses).

2. Do three WebSearch sweeps:
   a. Breaking news for each of the top-3 sectors (one query per sector is fine, or combine if it stays precise).
   b. News mentioning any of the watchlist names/tickers by name — batch a few tickers per query.
   c. Emerging sector strength: is any sector NOT in the top-3 starting to trend in headlines right now (a rotation
      signal worth flagging early)? Check 1-2 of the strongest-looking laggards/outsiders.

3. For each genuinely new, concrete item you find (skip generic "markets open higher" chatter — only items tied to
   a specific sector or watchlist name), build one JSON object:
   `{"scope": "<TICKER>" or "<Sector> (sector)" or "<Sector> (emerging)", "headline": "...", "source": "Outlet name",
   "url": "https://...", "tag": "bull"|"bear"|"neutral", "invalidates": true|false, "note": "..." (required if
   invalidates is true — say specifically what thesis it threatens and why)}`.
   Write the full JSON array to a scratch file (do NOT hand-type it into a shell pipe — non-ASCII characters like
   em dashes and smart quotes can get mangled that way on Windows).

4. Run `python scripts/news_loop.py record --in <that file>` and show me its output verbatim — it already dedupes
   against everything shown in earlier sweeps today and formats the result. If it prints "No new items this sweep,"
   that is a normal, complete result — just say so, don't pad it with old news to look busy.

5. Stop there for this cycle. Don't touch brief.html, open-update.html, data/brief-data.json, or either routine.
