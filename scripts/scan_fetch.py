"""Interactive-session data pull for the technical scan (stockanalysis.com public pages + JSON API; robots.txt allows them).

Not used by the cloud routines (they have no outbound HTTP besides FMP/WebSearch). Run from an interactive session:

    python scripts/scan_fetch.py lists  --industries "Semiconductors,Electronic Components" --out out/scan/lists.json
    python scripts/scan_fetch.py ohlc   --lists out/scan/lists.json --out out/scan/ohlc.json  [--min-cap 5e9]
    python scripts/scan_fetch.py stats  --tickers NVDA,MPWR --out out/scan/stats.json

Everything is fetched raw (no summarizer) and written as JSON so downstream code validates it (tracker.validate_rows).
One request at a time with a short pause; an honest user-agent.
"""
import argparse, datetime as dt, html, json, pathlib, re, sys, time, urllib.request

UA = 'premarket-brief-personal-research/1.0'
PAUSE = 0.25


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = urllib.request.Request(url, headers={'User-Agent': UA})
            return urllib.request.urlopen(r, timeout=30).read().decode('utf-8', 'replace')
        except Exception as e:                      # noqa: BLE001 - report and retry
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f'{url}: {last}')


def slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower().replace('&', 'and')).strip('-')


def parse_list(page):
    out = []
    for m in re.finditer(r'\{no:\d+,s:"([A-Z0-9.\-]+)",n:"((?:[^"\\]|\\.)*)",marketCap:(\d+),industry:"((?:[^"\\]|\\.)*)",change:(-?[\d.]+|null),volume:(\d+|null)', page):
        out.append({'tk': m.group(1), 'name': m.group(2).encode().decode('unicode_escape', 'ignore') if '\\' in m.group(2) else m.group(2),
                    'cap': float(m.group(3)), 'industry': m.group(4),
                    'chg': None if m.group(5) == 'null' else float(m.group(5)),
                    'vol': None if m.group(6) == 'null' else int(m.group(6))})
    return out


def fetch_lists(industries):
    res = {}
    for name in industries:
        res[name] = parse_list(get(f'https://stockanalysis.com/stocks/industry/{slug(name)}/'))
        time.sleep(PAUSE)
    return res


def fetch_ohlc(tk, rng='3M'):
    j = json.loads(get(f'https://stockanalysis.com/api/symbol/s/{tk}/history?range={rng}&period=Daily'))
    rows = [{'date': r['t'], 'open': r['o'], 'high': r['h'], 'low': r['l'], 'close': r['c'], 'volume': r['v']} for r in j.get('data', [])]
    return sorted(rows, key=lambda r: r['date'])


def _clean(s):
    s = re.sub(r'<!--.*?-->', '', s, flags=re.S)
    return html.unescape(re.sub(r'<[^>]+>', ' ', s)).strip()


def fetch_stats(tk):
    """Label -> displayed value for the statistics page (moving averages, avg volume, earnings date, margins, growth ...)."""
    page = get(f'https://stockanalysis.com/stocks/{tk.lower()}/statistics/')
    out = {}
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) >= 2:
            out[_clean(tds[0])] = _clean(tds[1])
    return out


def num(s):
    """'1.5B' -> 1.5e9, '+24.95%' -> 24.95, '1,234' -> 1234; None for '-' / n/a."""
    if s is None:
        return None
    t = str(s).replace(',', '').replace('+', '').strip()
    m = re.match(r'^(-?[\d.]+)\s*([KMBT%]?)$', t)
    if not m:
        return None
    v = float(m.group(1))
    return v * {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12, '%': 1, '': 1}[m.group(2)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['lists', 'ohlc', 'stats'])
    ap.add_argument('--industries')
    ap.add_argument('--lists')
    ap.add_argument('--tickers')
    ap.add_argument('--min-cap', type=float, default=5e9)
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == 'lists':
        res = fetch_lists([x.strip() for x in a.industries.split(',')])
        out.write_text(json.dumps({'fetchedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'source': 'stockanalysis.com industry lists', 'industries': res}, indent=1), encoding='utf-8')
        print({k: len(v) for k, v in res.items()})
        return 0
    if a.cmd == 'ohlc':
        lists = json.loads(pathlib.Path(a.lists).read_text(encoding='utf-8'))['industries']
        tks = sorted({r['tk'] for rows in lists.values() for r in rows if r['cap'] >= a.min_cap})
    else:
        tks = [t.strip().upper() for t in a.tickers.split(',')]
    res, fails = {}, {}
    for i, tk in enumerate(tks, 1):
        try:
            res[tk] = fetch_ohlc(tk) if a.cmd == 'ohlc' else fetch_stats(tk)
        except Exception as e:                      # noqa: BLE001
            fails[tk] = str(e)
        time.sleep(PAUSE)
        if i % 20 == 0:
            print(f'{i}/{len(tks)}', file=sys.stderr)
    out.write_text(json.dumps({'fetchedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'tickers': res, 'failures': fails}, indent=1), encoding='utf-8')
    print(f'{len(res)} ok, {len(fails)} failed -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())


def fetch_financials(tk):
    """TTM view of the income statement: {revGrowthPct, epsGrowthPct, opMarginPct, opMarginPriorPct (4 quarters back), period}."""
    page = get(f'https://stockanalysis.com/stocks/{tk.lower()}/financials/?p=trailing')
    rows = {}
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
        tds = [_clean(t) for t in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        if tds:
            rows.setdefault(tds[0].split('   ')[0].strip().split('  ')[0] if tds[0].startswith(('Revenue ', 'Earnings Per Share ')) else tds[0], tds[1:])
    def col(label, i):
        r = rows.get(label)
        return num(r[i]) if r and len(r) > i else None
    q = (rows.get('Fiscal Quarter') or [None])[0]
    return {'revGrowthPct': col('Revenue Growth', 0), 'epsGrowthPct': col('EPS Growth', 0), 'opMarginPct': col('Operating Margin', 0),
            'opMarginPriorPct': col('Operating Margin', 4), 'period': q, 'source': 'stockanalysis.com TTM income statement'}
