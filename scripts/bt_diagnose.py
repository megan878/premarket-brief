"""Step 0: full trace of named tickers through the CURRENT scan logic and the proposed detectors, on the latest stored data.

    python scripts/bt_diagnose.py --world out/bt/world.pkl --tickers ALAB,NET,INTC,NBIS,SANM
"""
import argparse, json, math, pathlib, pickle, sys
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import tracker as tr, selection as sel, scan_fetch as sf, scan_build as sb
import bt_signals as bs, bt_run as br

NINE = ['Semiconductor Equipment & Materials', 'Electronic Components', 'Semiconductors', 'Internet Content & Information',
        'Electronic Gaming & Multimedia', 'Publishing', 'Electrical Equipment & Parts', 'Specialty Industrial Machinery', 'Building Products & Equipment']


def trace(world, tk, t, rs_pct, spy, stats_page):
    s = world['series'][tk]
    I = s.ind
    c = s.c[t]
    P = bs.PARAMS
    L = []
    pr = lambda k, v: L.append(f'    {k:<34} {v}')               # noqa: E731
    L.append(f"{tk} · {s.meta['name']} · industry: {s.meta['industry']}")
    pr('data as of', s.dates[t])
    pr('in the CURRENT 9-industry universe?', 'YES' if s.meta['industry'] in NINE else f"NO - '{s.meta['industry']}' is not one of the 9 industries (universe gap)")
    capn = s.cap_at(t)
    pr('market cap (est. at t)', f'${capn / 1e9:.1f}B  (filter > $5B: {"ok" if capn >= 5e9 else "FAIL"})')
    pr('price / avg vol 20d / ADR20', f"{c:.2f} / {I['avgvol20'][t]:,.0f} / {I['adr20'][t]:.2f}%  (price>5, vol>500K, ADR>2: {'ok' if c > 5 and I['avgvol20'][t] > 5e5 and I['adr20'][t] > 2 else 'FAIL'})")
    a20, a50, a200 = c > I['sma20'][t], c > I['sma50'][t], c > I['sma200'][t]
    pr('above SMA20 / SMA50 / SMA200', f"{I['sma20'][t]:.2f} {'yes' if a20 else 'NO'} / {I['sma50'][t]:.2f} {'yes' if a50 else 'NO'} / {I['sma200'][t]:.2f} {'yes' if a200 else 'NO'}")
    pr('extension over SMA50', f"{(c / I['sma50'][t] - 1) * 100:.1f}%  (scan skips > 20%)")
    pivot = I['hh25'][t]
    age = bs.argmax_age(s.h, t)
    pr('25-session pivot / sessions since', f"{pivot:.2f} / {age}   close is {(pivot - c) / pivot * 100:.1f}% below it")
    pr('ADX14 / ATR% / EMA9>price>EMA21', f"{I['adx14'][t]:.1f} / {I['atr14'][t] / c * 100:.2f}% / {'yes' if I['ema9'][t] > c > I['ema21'][t] else 'no'}")
    pr('return 1M / 3M (SPY 1M / 3M)', f"{I['ret21'][t] * 100:+.1f}% / {I['ret63'][t] * 100:+.1f}%  ({spy['r21'] * 100:+.1f}% / {spy['r63'] * 100:+.1f}%)")
    pr('RS percentile in the universe', f"{rs_pct.get(tk, float('nan')):.0f}th" if tk in rs_pct else 'not in the point-in-time universe')
    v5 = np.nanmean(s.v[t - 4:t + 1])
    pr('volume: last 5d vs 20d avg / up:down 20d', f"{v5 / I['avgvol20'][t]:.2f}x / {I['updown20'][t]:.2f}")
    rd = tr.readiness_v2(bs.rows_window(s, t))
    pr('readiness v2 (tight/prox/dry/time)', f"{rd['composite']}  ({rd['tightness']}/{rd['proximity']}/{rd['volumeDryUp']}/{rd['timeInBase']})")
    # current scan gates
    cand = {'tk': tk, 'readiness': {'composite': rd['composite'], 'proximity': rd['proximity']}, 'timeInBaseDays': rd['timeInBaseDays'], 'lastClose': c, 'pivot': rd['pivot'],
            'entry': round(rd['pivot'] + 0.01, 2), 'lastLow': s.l[t], 'atr14': I['atr14'][t], 'adr20Pct': I['adr20'][t], 'lastSession': s.dates[t],
            'nextEarnings': None}
    ne = sb.parse_date((stats_page or {}).get('Earnings Date', ''))
    cand['nextEarnings'] = ne
    r = sel.check(cand)
    pr('earnings date (stockanalysis)', ne or 'not found')
    lv = r['levels']
    pr('current-rule levels', f"entry {cand['entry']} stop {lv['stop']} ({lv['stopNote']}) risk {lv['riskPct']}% R:R {lv['rr']}")
    first = []
    if s.meta['industry'] not in NINE:
        first.append('UNIVERSE: industry not in the 9 scanned')
    if not (c > 5 and I['avgvol20'][t] > 5e5 and I['adr20'][t] > 2 and capn >= 5e9):
        first.append('UNIVERSE: cap/price/volume/ADR filter')
    if not (a20 and a50) or c > 1.2 * I['sma50'][t]:
        first.append('UNIVERSE: SMA20/SMA50/extension filter')
    if not a200:
        first.append('UNIVERSE: below SMA200')
    first += [f"GATE {f['rule']}: {f['reason']}" for f in r['failed']]
    pr('CURRENT SCAN VERDICT', 'would be a pick' if not first else ' | '.join(first))
    # proposed detectors
    d = {'base': bs.detect_base(s, t), 'breakout': bs.detect_breakout(s, t), 'pullback': bs.detect_pullback(s, t, None, rs_pct.get(tk)), 'flag': bs.detect_flag(s, t)}
    for k, v in d.items():
        pr(f'detector {k}', ('YES: ' + v['why'] + f" | entry {v['lv']['entry']} stop {v['lv']['stop']} risk {v['lv']['riskPct']}% 2R target {v['lv']['target2R']}") if v else 'no')
    return '\n'.join(L), d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--world', required=True); ap.add_argument('--tickers', required=True)
    a = ap.parse_args(argv)
    world = pickle.loads(pathlib.Path(a.world).read_bytes())
    t = len(world['cal']) - 1
    P = bs.PARAMS
    rs_pct, top_ind, spy63 = br.cross_section(world, t, P)
    sp = world['bench']['SPY'][0]['close'].to_numpy(float)
    spy = {'r21': sp[t] / sp[t - 21] - 1, 'r63': sp[t] / sp[t - 63] - 1}
    for tk in a.tickers.split(','):
        if tk not in world['series']:
            print(f'{tk}: not in the downloaded universe (cap < floor or no history)')
            continue
        try:
            st = sf.fetch_stats(tk)
        except Exception:                                   # noqa: BLE001
            st = {}
        print(trace(world, tk, t, rs_pct, spy, st)[0])
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())


def pivot_today(world, t, names=None, label='all'):
    """Names whose 25-session high was made on the last session, classified by what they are doing."""
    P = bs.PARAMS
    rows = {'fresh breakout (volume >= 1.5x, strong close, <= 1.5 ATR over the old pivot)': [],
            'extended (> 1.5 ATR above the old pivot, or > 20% over SMA50)': [],
            'new high on ordinary volume (trend drift, no base before it)': [],
            'weak: new intraday high but closed under the old pivot': []}
    for tk, s in world['series'].items():
        if names is not None and tk not in names:
            continue
        if not bs.universe_ok(s, t, P) or bs.argmax_age(s.h, t) != 0:
            continue
        I = s.ind
        piv = I['hh25_prev'][t]
        rng = s.h[t] - s.l[t]
        volx = s.v[t] / I['avgvol20'][t - 1]
        pos = (s.c[t] - s.l[t]) / rng if rng > 0 else 0
        ext = (s.c[t] - piv) / I['atr14'][t]
        over50 = (s.c[t] / I['sma50'][t] - 1) * 100
        if s.c[t] <= piv:
            k = 'weak: new intraday high but closed under the old pivot'
        elif ext > 1.5 or over50 > 20:
            k = 'extended (> 1.5 ATR above the old pivot, or > 20% over SMA50)'
        elif volx >= 1.5 and pos >= 0.7:
            k = 'fresh breakout (volume >= 1.5x, strong close, <= 1.5 ATR over the old pivot)'
        else:
            k = 'new high on ordinary volume (trend drift, no base before it)'
        rows[k].append((tk, round(volx, 2), round(ext, 2), round(over50, 1)))
    print(f'--- names with their pivot on the last session ({world["cal"][t]}), universe: {label} ---')
    tot = sum(len(v) for v in rows.values())
    print(f'total: {tot}')
    for k, v in rows.items():
        print(f'  {len(v):>3}  {k}')
        print('       ', ', '.join(f'{a}({b}x,{c}ATR,+{d}%50d)' for a, b, c, d in sorted(v, key=lambda x: -x[1])[:12]))
    return rows
