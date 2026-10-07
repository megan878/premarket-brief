"""Interactive technical scan: universe filters -> readiness (current formula) -> selection rules -> pick cards.

    python scripts/scan_build.py analyse --lists out/scan/lists.json --ohlc out/scan/ohlc.json --last-session 2026-10-02 --out out/scan/candidates.json
    python scripts/scan_build.py stats   --cands out/scan/candidates.json --min-readiness 45 --out out/scan/stats.json
    python scripts/scan_build.py select  --cands out/scan/candidates.json --stats out/scan/stats.json --out out/scan/selection.json

Universe filters mirror the documented scan (CLAUDE.md "The scan"): price > $5, cap > $5B, 20-day average volume > 500K, ADR20 > 2%,
price above SMA20/50/200 (Finviz proxies), plus the 2 Oct practice of skipping names more than 20% above SMA50. EMA9/EMA21 and ADX(14)
are COMPUTED from the OHLC and reported on each candidate, but they are not gates (the scan has never gated on them): the report counts
how many names would pass if they were. Everything is computed from raw rows; nothing is typed by hand.
"""
import argparse, datetime as dt, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import market_time as mt, tracker as tr, selection as sel, scan_fetch as sf

SECTOR_OF = {'Semiconductor Equipment & Materials': 'Technology', 'Electronic Components': 'Technology', 'Semiconductors': 'Technology',
             'Internet Content & Information': 'Communication Services', 'Electronic Gaming & Multimedia': 'Communication Services',
             'Publishing': 'Communication Services', 'Electrical Equipment & Parts': 'Industrials',
             'Specialty Industrial Machinery': 'Industrials', 'Building Products & Equipment': 'Industrials',
             'Communication Equipment': 'Technology', 'Software - Infrastructure': 'Technology', 'Electronics & Computer Distribution': 'Technology',
             'Airlines': 'Industrials', 'Marine Shipping': 'Industrials'}      # plus whatever --market names (the scan's own nine industries)
UNIVERSE = {'minPrice': 5.0, 'minCap': 5e9, 'minAvgVol': 500_000, 'minAdr': 2.0, 'maxAboveSma50Pct': 20.0}


def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def ema(closes, n):
    k, e = 2 / (n + 1), closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def adx14(rows):
    """Wilder ADX(14) from OHLC rows (oldest first); None if fewer than 29 rows."""
    if len(rows) < 29:
        return None
    tr_, pdm, ndm = [], [], []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]['high'], rows[i]['low'], rows[i - 1]['close']
        up, dn = h - rows[i - 1]['high'], rows[i - 1]['low'] - l
        tr_.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
    def wilder(xs):
        s = sum(xs[:14]); out = [s]
        for x in xs[14:]:
            s = s - s / 14 + x; out.append(s)
        return out
    atr, p, n = wilder(tr_), wilder(pdm), wilder(ndm)
    dx = []
    for a, b, c in zip(atr, p, n):
        pi, ni = 100 * b / a if a else 0, 100 * c / a if a else 0
        dx.append(100 * abs(pi - ni) / (pi + ni) if (pi + ni) else 0)
    if len(dx) < 14:
        return None
    a = sum(dx[:14]) / 14
    for x in dx[14:]:
        a = (a * 13 + x) / 14
    return a


def round_level(entry, target):
    """Nearest round number strictly between entry and target (for the trim note), else None."""
    for step in (100, 50, 25, 10, 5, 2.5, 1):
        if entry >= step * 4 or step == 1:
            lvl = (int(entry / step) + 1) * step
            if lvl < target:
                return lvl
    return None


def analyse_one(tk, rows, last, meta):
    srt = tr.sort_rows(rows)
    start = mt.trading_days_back(dt.date.fromisoformat(last), tr.SERIES_SESSIONS)[-1].isoformat()
    ok, probs, win = tr.validate_rows([r for r in srt if r['date'] <= last], start, last)
    if not ok:
        return None, probs
    hist = [r for r in srt if r['date'] <= last]
    closes = [r['close'] for r in hist]
    px = closes[-1]
    c = {'tk': tk, 'name': meta['name'], 'industry': meta['industry'], 'sector': SECTOR_OF.get(meta['industry']), 'cap': meta['cap'],
         'lastSession': last, 'lastClose': px, 'lastDayPct': round((px / closes[-2] - 1) * 100, 2) if len(closes) > 1 else None}
    vol20 = [r['volume'] for r in hist[-20:]]
    c['avgVol20'] = sum(vol20) / len(vol20)
    c['adr20Pct'] = tr.adr20_pct(hist, last)
    c['atr14'] = tr.atr14(hist[-15:])
    c['sma20'], c['sma50'] = sma(closes, 20), sma(closes, 50)
    c['ema9'], c['ema21'] = ema(closes, 9), ema(closes, 21)
    c['emaCompression'] = c['ema9'] > px > c['ema21']
    c['adx14'] = adx14(hist)
    c['ret1m'] = round((px / closes[-22] - 1) * 100, 2) if len(closes) > 22 else None      # 21 sessions back
    f = []
    if px <= UNIVERSE['minPrice']: f.append('price <= $5')
    if c['avgVol20'] <= UNIVERSE['minAvgVol']: f.append('avg volume <= 500K')
    if c['adr20Pct'] is None or c['adr20Pct'] <= UNIVERSE['minAdr']: f.append('ADR20 <= 2%')
    if c['sma20'] is None or px <= c['sma20']: f.append('not above SMA20')
    if c['sma50'] is None or px <= c['sma50']: f.append('not above SMA50')
    elif (px / c['sma50'] - 1) * 100 > UNIVERSE['maxAboveSma50Pct']: f.append('more than 20% above SMA50')
    c['universeFail'] = f
    rd = tr.readiness(win)
    c['readiness'] = {k: rd[k] for k in ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')}
    c['timeInBaseDays'], c['pivot'] = rd['timeInBaseDays'], rd['pivot']
    c['pivotDate'] = win[[r['high'] for r in win].index(rd['pivot'])]['date']
    entry = round(rd['pivot'] + 0.01, 2)
    last_low = hist[-1]['low']
    stop = round(last_low, 2) if 0.04 <= (entry - last_low) / entry <= 0.06 else round(entry * 0.95, 2)
    c.update(entry=entry, stop=stop, target=round(entry * 1.10, 2),
             stopNote=("last session's low (%.1f%% away)" % ((entry - last_low) / entry * 100)) if stop == round(last_low, 2) else '~5% below entry')
    c['lastLow'] = hist[-1]['low']
    c['closes'] = [r['close'] for r in win]
    c['windowRows'] = len(win)
    return c, probs


def cmd_analyse(a):
    if getattr(a, 'market', None):
        mk = json.loads(pathlib.Path(a.market).read_text(encoding='utf-8'))
        for g in mk['industries']:
            for it in g['items']:
                SECTOR_OF[it['name']] = g['sector']
    lists = json.loads(pathlib.Path(a.lists).read_text(encoding='utf-8'))['industries']
    ohlc = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8'))['tickers']
    meta = {}
    for ind, rows in lists.items():
        for r in rows:
            if r['cap'] >= UNIVERSE['minCap']:
                meta.setdefault(r['tk'], {'name': r['name'], 'industry': ind, 'cap': r['cap']})
    out, excluded = [], {}
    for tk, m in sorted(meta.items()):
        c, probs = analyse_one(tk, ohlc.get(tk) or [], a.last_session, m)
        if c is None:
            excluded[tk] = probs
        else:
            out.append(c)
    pathlib.Path(a.out).write_text(json.dumps({'lastSession': a.last_session, 'universe': UNIVERSE, 'considered': len(meta), 'candidates': out,
                                               'excludedBadOhlc': excluded}, indent=1), encoding='utf-8')
    passed = [c for c in out if not c['universeFail']]
    print(f"considered {len(meta)} names over $5B; bad OHLC {len(excluded)}; universe filters passed {len(passed)}; "
          f"of which EMA9>price>EMA21: {sum(1 for c in passed if c['emaCompression'])}, ADX>20: {sum(1 for c in passed if (c['adx14'] or 0) > 20)}")
    return 0


def cmd_stats(a):
    d = json.loads(pathlib.Path(a.cands).read_text(encoding='utf-8'))
    tks = [c['tk'] for c in d['candidates'] if not c['universeFail'] and c['readiness']['composite'] >= a.min_readiness]
    res, fails = {}, {}
    for tk in tks:
        try:
            res[tk] = sf.fetch_stats(tk)
        except Exception as e:                   # noqa: BLE001
            fails[tk] = str(e)
    pathlib.Path(a.out).write_text(json.dumps({'tickers': res, 'failures': fails}, indent=1), encoding='utf-8')
    print(f'{len(res)} stats pages, {len(fails)} failed')
    return 0


def parse_date(s):
    try:
        return dt.datetime.strptime(s.strip(), '%b %d, %Y').date().isoformat()
    except Exception:
        return None


def prepare(d, st, extra=None):
    """Scored candidates = universe filters passed + SMA200 + earnings date attached. Returns (scored, universe_out)."""
    extra = extra or {}
    scored, universe_out = [], []
    for c in d['candidates']:
        if c['universeFail']:
            universe_out.append(c)
            continue
        s = st.get(c['tk'])
        if s:
            sma200 = sf.num(s.get('200-Day Moving Average'))
            c['sma200'] = sma200
            if sma200 is not None and c['lastClose'] <= sma200:
                c['universeFail'].append('not above SMA200')
                universe_out.append(c)
                continue
            c['nextEarnings'] = parse_date(s.get('Earnings Date', ''))
            c['earningsSource'] = 'stockanalysis statistics'
            c['stats'] = {k: s.get(k) for k in ('Operating Margin', 'Free Cash Flow', 'Price Target', 'Relative Strength Index (RSI)', 'Revenue', 'Earnings Per Share (EPS)', 'Analyst Count')}
        if c['tk'] in extra:                      # a verified date from a second source (WebSearch) overrides a missing one
            c['nextEarnings'] = c.get('nextEarnings') or extra[c['tk']].get('date')
            c['earningsSource'] = (c.get('earningsSource') or '') + ' + ' + extra[c['tk']].get('source', 'web')
        scored.append(c)
    return scored, universe_out


def cmd_select(a):
    d = json.loads(pathlib.Path(a.cands).read_text(encoding='utf-8'))
    st = json.loads(pathlib.Path(a.stats).read_text(encoding='utf-8'))['tickers']
    extra = json.loads(pathlib.Path(a.earnings_extra).read_text(encoding='utf-8')) if a.earnings_extra and pathlib.Path(a.earnings_extra).exists() else {}
    scored, universe_out = prepare(d, st, extra)
    r = sel.select_tiered(scored)
    r['thresholds'] = sel.threshold_counts([c for c in scored if c.get('stats')], cfg=None)
    r['universeOut'] = len(universe_out)
    r['scoredCandidates'] = scored
    pathlib.Path(a.out).write_text(json.dumps(r, indent=1, default=str), encoding='utf-8')
    print(sel.header_line(r), '| thresholds', r['thresholds']['passing'])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    an = sub.add_parser('analyse'); an.add_argument('--lists', required=True); an.add_argument('--ohlc', required=True)
    an.add_argument('--last-session', required=True); an.add_argument('--out', required=True); an.add_argument('--market', help='market.json: the scan own industry to sector map')
    s = sub.add_parser('stats'); s.add_argument('--cands', required=True); s.add_argument('--min-readiness', type=int, default=45); s.add_argument('--out', required=True)
    se = sub.add_parser('select'); se.add_argument('--cands', required=True); se.add_argument('--stats', required=True)
    se.add_argument('--earnings-extra'); se.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    return {'analyse': cmd_analyse, 'stats': cmd_stats, 'select': cmd_select}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
