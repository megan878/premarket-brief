"""Interactive pass over the catalyst alerts: re-price them, score priced-in, compute flags (OHLC + stockanalysis pages).

    python scripts/scan_catalysts.py --data out/scan/base.json --market out/scan/market.json --last-session 2026-10-02 --reactions ACN=2026-10-01,... --out out/scan/catalysts.json
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import tracker as tr, pricedin as pi, flags as fl, scan_fetch as sf


def money(s):
    return sf.num(str(s).replace('$', '')) if s else None


def process(cat, rows, stats, fin, last, market, reaction_date):
    rows = [r for r in tr.sort_rows(rows) if r['date'] <= last]
    price = rows[-1]['close']
    tgt = money((stats or {}).get('Price Target'))
    inp = pi.inputs_from_ohlc(rows, reaction_date, last, price=price, target=tgt)
    inp['targetSource'] = 'stockanalysis.com consensus (single source)' if tgt else None
    p = pi.score(inp)
    out = {'tk': cat['tk'], 'px': price, 'pct': round((price / rows[-2]['close'] - 1) * 100, 2), 'pricedIn': p}
    flags, unavailable = [], []
    ret1m = round((price / rows[-22]['close'] - 1) * 100, 2) if len(rows) > 22 else None
    f, u = fl.sector_flag(cat.get('sector'), cat.get('industry'), ret1m, market['sectors'], market['industries'])
    (flags.append(f) if f else None); (unavailable.append(f"sector: {u}") if u else None)
    fin = dict(fin or {})
    fin['fcf'] = money((stats or {}).get('Free Cash Flow'))
    if fin.get('epsGrowthPct') is not None and fin['epsGrowthPct'] < 0 and (fin.get('revGrowthPct') or 0) > 0 and \
            (fin.get('opMarginPct') or 0) >= (fin.get('opMarginPriorPct') or 0):
        fin['epsNote'] = 'EPS fell while revenue and operating margin rose: likely a one-off in the comparison year (GAAP base distorted)'
    f, u = fl.financials_flag(fin)
    (flags.append(f) if f else None); (unavailable.append(f"financials: {u}") if u else None)
    f, u = fl.catalyst_flag(cat, p, last)
    (flags.append(f) if f else None); (unavailable.append(f"catalyst: {u}") if u else None)
    out.update(flags=flags, flagLog=unavailable, financials=fin, ret1m=ret1m)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True); ap.add_argument('--market', required=True); ap.add_argument('--last-session', required=True)
    ap.add_argument('--reactions', required=True, help='TK=YYYY-MM-DD,... first session that traded the news'); ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    data = json.loads(pathlib.Path(a.data).read_text(encoding='utf-8'))
    market = json.loads(pathlib.Path(a.market).read_text(encoding='utf-8'))
    react = dict(x.split('=') for x in a.reactions.split(','))
    res = {}
    for cat in data['catalysts']:
        tk = cat['tk']
        res[tk] = process(cat, sf.fetch_ohlc(tk), sf.fetch_stats(tk), sf.fetch_financials(tk), a.last_session, market, react[tk])
    pathlib.Path(a.out).write_text(json.dumps(res, indent=1), encoding='utf-8')
    for tk, r in res.items():
        print(tk, r['px'], r['pricedIn']['label'], r['pricedIn']['components'], [f['key'] for f in r['flags']], r['flagLog'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
