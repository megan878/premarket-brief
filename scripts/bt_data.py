"""Backtest data pull (interactive only): every stockanalysis.com industry list, then up to 5Y of daily OHLC for names above a cap floor.

    python scripts/bt_data.py lists  --out out/bt/lists.json
    python scripts/bt_data.py ohlc   --lists out/bt/lists.json --min-cap 3e9 --out out/bt/ohlc.json [--extra SPY,QQQ,IWM]
Raw JSON from the public API (robots.txt allows it); one request at a time. The cap is the CURRENT market cap, so the universe has
survivorship bias (names that fell out of the index or were delisted are absent) - stated in every backtest report.
"""
import argparse, json, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import scan_fetch as sf


def all_industries():
    page = sf.get('https://stockanalysis.com/stocks/industry/')
    return sorted(set(re.findall(r'href="/stocks/industry/([a-z0-9-]+)/?"', page)))


def fetch_ohlc_rng(tk, kind='s', rng='5Y'):
    j = json.loads(sf.get(f'https://stockanalysis.com/api/symbol/{kind}/{tk}/history?range={rng}&period=Daily'))
    rows = [{'date': r['t'], 'open': r['o'], 'high': r['h'], 'low': r['l'], 'close': r['c'], 'volume': r['v']} for r in j.get('data', [])]
    return sorted(rows, key=lambda r: r['date'])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['lists', 'ohlc'])
    ap.add_argument('--lists'); ap.add_argument('--min-cap', type=float, default=3e9); ap.add_argument('--extra', default='SPY,QQQ,IWM')
    ap.add_argument('--out', required=True); ap.add_argument('--rng', default='5Y')
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out)
    if a.cmd == 'lists':
        res = {}
        for slug in all_industries():
            try:
                rows = sf.parse_list(sf.get(f'https://stockanalysis.com/stocks/industry/{slug}/'))
            except Exception as e:                  # noqa: BLE001
                print('fail', slug, e, file=sys.stderr); continue
            res[slug] = rows
            time.sleep(sf.PAUSE)
        out.write_text(json.dumps({'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'industries': res}), encoding='utf-8')
        print({'industries': len(res), 'names': sum(len(v) for v in res.values())})
        return 0
    lists = json.loads(pathlib.Path(a.lists).read_text(encoding='utf-8'))['industries']
    meta = {}
    for slug, rows in lists.items():
        for r in rows:
            if r['cap'] >= a.min_cap:
                meta.setdefault(r['tk'], {'name': r['name'], 'industry': r['industry'], 'slug': slug, 'cap': r['cap']})
    tks = sorted(meta)
    data, fails = {}, {}
    prior = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'ohlc': {}}
    data = prior.get('ohlc', {})
    for i, tk in enumerate([e for e in a.extra.split(',') if e] + tks, 1):
        if tk in data:
            continue
        kind = 'e' if tk in ('SPY', 'QQQ', 'IWM') else 's'
        try:
            data[tk] = fetch_ohlc_rng(tk, kind, a.rng)
        except Exception as e:                          # noqa: BLE001
            fails[tk] = str(e)[:80]
        time.sleep(sf.PAUSE)
        if i % 100 == 0:
            print(f'{i}/{len(tks)}', file=sys.stderr, flush=True)
            out.write_text(json.dumps({'meta': meta, 'ohlc': data, 'failures': fails}), encoding='utf-8')
    out.write_text(json.dumps({'meta': meta, 'ohlc': data, 'failures': fails}), encoding='utf-8')
    print({'names': len(meta), 'fetched': len(data), 'failed': len(fails)})
    return 0


if __name__ == '__main__':
    sys.exit(main())
