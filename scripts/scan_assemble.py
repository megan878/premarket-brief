"""Assemble data/brief-data.json from the interactive scan outputs in out/scan/ (the last published block is the base).

    python scripts/scan_assemble.py --base out/scan/base.json --scan out/scan --market out/scan/market.json --out data/brief-data.json

Cleared and rebuilt (reset_policy.CLEAR): sectors, industries, picks + selection, near-misses, expired catalysts. Kept: live catalysts
(re-priced, priced-in scored, flagged), notices, macro rules. Derived blocks (health, watch, tracker, labels) are dropped: the builder recomputes them.
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import selection as sel, reset_policy as rp, health as hl, market_time as mt

DERIVED = ('health', 'watch', 'tracker', 'asOf', 'asOfLabel', 'builtLabel', 'meta', 'runs', 'calendar', 'resetPolicy', 'selectionConfig')


def load(p):
    return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True); ap.add_argument('--scan', required=True); ap.add_argument('--market', required=True)
    ap.add_argument('--fmp', required=True, help='json with indices[] / macro{} already in page shape'); ap.add_argument('--out', required=True)
    ap.add_argument('--last-session', required=True)
    a = ap.parse_args(argv)
    scan = pathlib.Path(a.scan)
    base, market, fmp = load(a.base), load(a.market), load(a.fmp)
    cand, sl, cats = load(scan / 'candidates.json'), load(scan / 'selection.json'), load(scan / 'catalysts.json')
    last = a.last_session
    d = {k: v for k, v in base.items() if k not in DERIVED}
    for k in rp.CLEAR_KEYS:
        d.pop(k, None)
    # ── fresh automated-type sections from FMP (index closes, rates) ──
    d['indices'] = fmp['indices']
    d['macro'] = {**base['macro'], **fmp['macro']}
    # ── sector ranking / industry drill-down (Finviz groups, read twice) ──
    d['sectors'], d['industries'], d['sectorSource'] = market['sectors'], market['industries'], market['sectorSource']
    # ── picks: quality rules, possibly zero ──
    picks = []
    for rank, c in enumerate(sl['picks'], 1):
        picks.append({**{k: c[k] for k in ('tk', 'name', 'industry', 'sector', 'pivot', 'entry', 'stop', 'target', 'stopNote', 'closes', 'readiness', 'atr14')},
                      'selectionRank': rank, 'selectionVersion': sel.SELECTION_VERSION, 'scoringVersion': 'v2'})
    d['picks'] = picks                                           # (cards for qualifiers are built by scan_cards.py when there are any)
    ld = mt.dt.date.fromisoformat(last)
    d['pickWindow'] = f'25 trading days to {ld.day} {ld.strftime("%b %Y")}, stockanalysis.com daily data'
    passed = [c for c in cand['candidates'] if not c['universeFail']]
    d['selection'] = {'version': sel.SELECTION_VERSION, 'scored': sl['scored'], 'qualified': sl['qualified'], 'cut': len(sl['cut']),
                      'config': sl['config'], 'thresholds': sl['thresholds'], 'headline': sel.header_line(sl),
                      'universe': {'considered': cand['considered'], 'passedFilters': len(passed),
                                   'emaZone': sum(1 for c in passed if c['emaCompression']), 'adxOver20': sum(1 for c in passed if (c['adx14'] or 0) > 20)},
                      'why': sel.failure_summary(sl['scoredCandidates'], sl['config']),
                      'earningsUnverified': [x['tk'] for x in sl['rejected'] if any(f['reason'].startswith('earnings date unverified') for f in x['failed'])]}
    # ── near misses: the closest names and the rule that stopped each ──
    near = sorted([x for x in sl['rejected'] if x['readiness'] is not None], key=lambda x: -x['readiness'])
    by_tk = {c['tk']: c for c in sl['scoredCandidates']}
    d['nearmiss'] = []
    for x in near[:12]:
        c = by_tk[x['tk']]
        why = '; '.join(f['reason'] for f in x['failed'])
        if x['failed'] and all(f['rule'].startswith('stop') for f in x['failed']) and x['readiness'] >= sl['config']['minReadiness']:
            why = 'reaches the readiness bar; stopped ONLY by the stop rule: ' + why
        d['nearmiss'].append({'tk': x['tk'], 'px': c['lastClose'], 'pct': c['lastDayPct'] if c['lastDayPct'] is not None else 0.0,
                              'why': f"{c['industry']} · readiness {x['readiness']} · {why}"})
    for c in sl['cut']:
        d['nearmiss'].append({'tk': c['tk'], 'px': c['lastClose'], 'pct': c['lastDayPct'] or 0.0, 'why': f"{c['industry']} · readiness {c['readiness']['composite']} · cut at {sl['config']['maxPicks']}"})
    # ── catalysts: re-priced, scored, flagged ──
    out_c = []
    for c in base['catalysts']:
        u = cats.get(c['tk'])
        if not u:
            continue
        out_c.append({**c, 'px': u['px'], 'pct': u['pct'], 'pricedIn': u['pricedIn'], 'flags': u['flags']})
    d['catalysts'] = out_c
    flag_log = [f"{tk}: {m}" for tk, u in cats.items() for m in u['flagLog']]
    flag_log += ['technical picks: none published this run, so no pick flags were computed',
                 'catalyst flag for technical picks / near-misses: no catalyst search was run (alerts only)',
                 'financials flags use stockanalysis.com TTM statements (single source, not cross-checked)']
    d['flagLog'] = flag_log
    # ── sections meta ──
    d['sections'] = {
        'indices': {'asOf': last, 'source': 'FMP index quotes (SPX/DJI/RUT); VIX = FMP previousClose (the 2 Oct close); QQQ omitted'},
        'macro': {'asOf': last, 'source': 'FMP treasury-rates; Fed block unchanged (no FOMC meeting since 16 Sep)'},
        'sectors': {'asOf': last, 'source': 'Finviz groups · Perf Month (interactive refresh, 5 Oct)'},
        'industries': {'asOf': last, 'source': 'Finviz groups · Perf Month (interactive refresh, 5 Oct)'},
        'technical': {'asOf': last, 'source': 'stockanalysis.com industry lists + daily OHLC API (interactive scan, scripts/scan_build.py)'},
        'catalysts': {'asOf': last, 'source': 'events: WebSearch (2+ sources each, 1 Oct brief); prices: stockanalysis.com 2 Oct closes; priced-in + flags computed'},
        'nearmiss': {'asOf': last, 'source': 'scan candidates that failed a selection rule (interactive scan)'}}
    d['screenNotes'] = [
        {'st': 'ok', 'icon': '✓', 'html': f"<b>Universe</b> · {cand['considered']} names over $5B in the 9 industries (stockanalysis.com lists); {len(passed)} passed price &gt; $5, 20-day volume &gt; 500K, ADR20 &gt; 2%, above SMA20/50/200 and not &gt; 20% above SMA50."},
        {'st': 'ok', 'icon': '✓', 'html': f"<b>EMA9 / ADX(14)</b> · computed from the daily bars for every candidate, shown on each card, but not gates (the scan has never gated on them): {d['selection']['universe']['emaZone']} of {len(passed)} would pass EMA9 &gt; price &gt; EMA21, {d['selection']['universe']['adxOver20']} would pass ADX &gt; 20."},
        {'st': 'ok', 'icon': '✓', 'html': f"<b>Selection</b> · {sel.header_line(sl)}. Names passing at readiness 45 / 55 / 65: {sl['thresholds']['passing']['45']} / {sl['thresholds']['passing']['55']} / {sl['thresholds']['passing']['65']} (every rule applied)."},
        {'st': 'px', 'icon': '~', 'html': "<b>Earnings dates</b> · stockanalysis.com statistics page; a name with no verifiable date is rejected (\"earnings date unverified\")."}]
    d['rejected'] = []
    d['notices'] = [
        "WebFetch is unavailable to the automated routine (platform bug): the scan, industry drill-down and near-misses are refreshed only in interactive sessions and carried forward between them.",
        f"Sector ranking and industry drill-down: Finviz groups (Perf Month), interactive refresh Mon 5 Oct, data to the {last} close; the table was read twice and both reads agree.",
        "Index quotes are FMP's 2 Oct closes. The VIX uses FMP's previousClose (its live print had already moved to 5 Oct). QQQ shows the 1 Oct close ($742.03, WebSearch, recorded in the 2 Oct provenance): no reliable 2 Oct close was found.",
        "Catalyst alerts are the 30 Sep - 1 Oct events from the 1 Oct brief, re-priced with 2 Oct closes; the routine's next run refreshes the alerts and their sources.",
        "Priced-in scores use stockanalysis.com daily bars and its consensus price target (a single source).",
    ]
    d['fetchlog'] = [{'id': 'stockanalysis.industry-lists', 'ok': True, 'note': f"{cand['considered']} names over $5B"},
                     {'id': 'stockanalysis.ohlc-api', 'ok': True, 'note': f"{cand['considered']} tickers, {len(cand['excludedBadOhlc'])} excluded for bad bars"},
                     {'id': 'stockanalysis.statistics', 'ok': True, 'note': 'SMA200, earnings date, price target, margins for candidates'},
                     {'id': 'finviz.groups sector+industry', 'ok': True, 'note': 'read twice, identical'},
                     {'id': 'fmp.indexes.index-quote', 'ok': True, 'note': 'SPX, DJI, RUT 2 Oct close; VIX previousClose'},
                     {'id': 'fmp.economics.treasury-rates', 'ok': True, 'note': 'through 2 Oct'},
                     {'id': 'websearch.qqq.close', 'ok': False, 'note': 'no reliable 2 Oct close found; tile omitted'}]
    pathlib.Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    print('wrote', a.out, '| picks', len(picks), '| nearmiss', len(d['nearmiss']), '| catalysts', len(out_c))
    return 0


if __name__ == '__main__':
    sys.exit(main())
