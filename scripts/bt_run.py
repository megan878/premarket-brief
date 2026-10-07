"""Point-in-time backtest of the technical scan, replayed with the SAME tracker.replay as the live ledger.

    python scripts/bt_run.py build --ohlc out/bt/ohlc.json --out out/bt/world.pkl          # align + indicators once
    python scripts/bt_run.py scan  --world out/bt/world.pkl --out out/bt/signals.pkl [--step 5] [--params '{...}']
No look-ahead: a signal on scan date t reads indicators/arrays at index <= t only; its outcome is replayed from t+1 onward by tracker.replay
(entry trigger, stop, target, gap rules, 10-session expiry, 20-session time exit). Limits are printed with every report (see REPORT_LIMITS).
"""
import argparse, copy, json, math, pathlib, pickle, random, sys, time
import numpy as np
import pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import tracker as tr, selection as sel
import bt_signals as bs

REPORT_LIMITS = [
    'Universe = names that are above the cap floor TODAY (survivorship bias: delisted / shrunken names are absent, which flatters every long strategy).',
    'Market cap in the past is estimated as today\'s cap x close_t / close_today (constant share count).',
    'One market regime (roughly Oct 2021 - Oct 2026 of the data source), small n per bucket, overlapping holding windows between scan dates.',
    'No historical earnings calendar exists in the source, so the earnings blackout cannot be applied in the backtest (it is applied live).',
    'No historical VIX in the source: the regime filter uses SPY close > SMA50 and SPY 20-day realised vol < 20% as the proxy for "VIX < 20".',
    'Sector / financials / catalyst flags need today\'s fundamentals, so their weight is 0 in the backtest composite (renormalised).',
    'Fills assume the tracker rules (stop-buy at entry, gap fills at the open); no slippage, no commissions.',
]


def build_world(ohlc_path, out_path):
    raw = json.loads(pathlib.Path(ohlc_path).read_text(encoding='utf-8'))
    meta, ohlc = raw['meta'], raw['ohlc']
    spy = pd.DataFrame(ohlc['SPY']).set_index('date')
    cal = list(spy.index)
    world = {'cal': cal, 'series': {}, 'meta': {}, 'bench': {}}
    for k in ('SPY', 'QQQ', 'IWM'):
        if k in ohlc:
            df = pd.DataFrame(ohlc[k]).set_index('date').reindex(cal)
            world['bench'][k] = (df, bs.indicators(df))
    for tk, rows in ohlc.items():
        if tk in ('SPY', 'QQQ', 'IWM') or tk not in meta or len(rows) < 260:
            continue
        df = pd.DataFrame(rows).drop_duplicates('date').set_index('date')
        df = df[df.index.isin(cal)].reindex(cal)
        if df['close'].notna().sum() < 250:
            continue
        df = df.astype(float)
        bad = (df['low'] <= 0) | (df['high'] < df['low']) | (df['close'] > df['high'] * 1.0001) | (df['close'] < df['low'] * 0.9999)
        df.loc[bad.fillna(False), :] = np.nan                           # drop corrupt rows rather than guess
        ind = bs.indicators(df)
        world['series'][tk] = bs.Series(tk, df, ind, meta[tk])
        world['meta'][tk] = meta[tk]
    pathlib.Path(out_path).write_bytes(pickle.dumps(world, protocol=4))
    print(f"world: {len(world['series'])} names, {len(cal)} sessions {cal[0]} -> {cal[-1]}")


def cross_section(world, t, params):
    """Point-in-time universe, RS percentile and industry rank at index t."""
    P = params
    spy = world['bench']['SPY'][0]['close'].to_numpy(float)
    spy63 = spy[t] / spy[t - 63] - 1
    names, perf = [], []
    for tk, s in world['series'].items():
        if bs.universe_ok(s, t, P):
            I = s.ind
            r = 0.5 * I['ret63'][t] + 0.3 * I['ret126'][t] + 0.2 * I['ret21'][t]
            if not math.isnan(r):
                names.append(tk); perf.append(r)
    if not names:
        return {}, {}, spy63
    rank = pd.Series(perf, index=names).rank(pct=True) * 100
    ind = {}
    for tk in names:
        s = world['series'][tk]
        ind.setdefault(s.meta['industry'], []).append((s.ind['ret21'][t], s.ind['ret63'][t]))
    sc = {k: (np.nanmedian([a for a, _ in v]), np.nanmedian([b for _, b in v])) for k, v in ind.items() if len(v) >= 3}
    r21 = pd.Series({k: v[0] for k, v in sc.items()}).rank(pct=True)
    r63 = pd.Series({k: v[1] for k, v in sc.items()}).rank(pct=True)
    comb = ((r21 + r63) / 2).sort_values(ascending=False)
    top = set(comb.index[:15])
    return rank.to_dict(), top, spy63


def regime(world, t):
    spy = world['bench']['SPY'][0]['close'].to_numpy(float)
    sma50 = world['bench']['SPY'][1]['sma50'].to_numpy(float)[t]
    rv = np.nanstd(np.diff(np.log(spy[t - 20:t + 1]))) * math.sqrt(252) * 100
    return bool(spy[t] > sma50 and rv < 20), float(rv)


def make_rows(s, a, b):
    return [{'date': s.dates[i], 'open': s.o[i], 'high': s.h[i], 'low': s.l[i], 'close': s.c[i]} for i in range(a, b)
            if not math.isnan(s.c[i])]


def replay_levels(s, t, entry, stop, target):
    rec = {'entry': entry, 'stop': stop, 'target': target, 'entryZoneHigh': entry * 1.01, 'startSession': s.dates[t + 1]}
    return tr.replay(rec, make_rows(s, t + 1, t + 31))


def outcome(s, t, lv, model):
    target = lv['target10'] if model == '10' else lv['target2R']
    o = replay_levels(s, t, lv['entry'], lv['stop'], target)
    return {'status': o['status'], 'R': o['rMultiple'], 'fill': o['fillPrice'], 'days': o['daysHeld'], 'exitDate': o['exitDate'], 'trigDate': o['triggerDate']}


def signals_at(world, params, t, setups=None):
    """All setup signals on scan date index t (point-in-time), with the composite, primary flag and rank. No outcomes."""
    rs_pct, top_ind, spy63 = cross_section(world, t, params)
    if not rs_pct:
        return []
    risk_on, rv = regime(world, t)
    day = []
    for tk, rsp in rs_pct.items():
        s = world['series'][tk]
        found = []
        for name, fn in (('base', bs.detect_base), ('breakout', bs.detect_breakout), ('flag', bs.detect_flag)):
            if setups is not None and name not in setups:
                continue
            d = fn(s, t, params)
            if d:
                found.append(d)
        if setups is None or 'pullback' in setups:
            d = bs.detect_pullback(s, t, params, rsp)
            if d:
                found.append(d)
        if not found:
            continue
        r63 = s.ind['ret63'][t]
        rs_score = 0.6 * rsp + 0.4 * min(max(50 + 100 * (r63 - spy63), 0), 100)
        for d in found:
            comp = bs.composite(rs_score, d['quality'], bs.trend_quality(s, t), bs.accumulation(s, t), None)
            day.append({'t': t, 'date': s.dates[t], 'tk': tk, 'industry': s.meta['industry'], 'setup': d['setup'], 'quality': d['quality'],
                        'rs': rs_score, 'rsPct': rsp, 'trend': bs.trend_quality(s, t), 'accum': bs.accumulation(s, t), 'composite': comp,
                        'topIndustry': s.meta['industry'] in top_ind, 'riskOn': risk_on, 'rv': rv, 'cap': s.cap_at(t), 'lv': d['lv'],
                        'why': d['why'], 'trigger': d['trigger'], 'invalidation': d['invalidation']})
    best = {}
    for d in sorted(day, key=lambda d: -d['composite']):
        best.setdefault(d['tk'], d['composite'])
    for d in day:
        d['primary'] = abs(best[d['tk']] - d['composite']) < 1e-12
    prim = sorted([d for d in day if d['primary']], key=lambda d: -d['composite'])
    for r, d in enumerate(prim, 1):
        d['rank'] = r
    for d in day:
        d.setdefault('rank', None)
    return day


def scan(world, params, step=5, first=240, only=None, setups=None):
    n = len(world['cal'])
    idx = list(range(first, n - 31, step))
    if only is not None:
        idx = [i for i in idx if i in only]
    sigs = []
    for t in idx:
        day = signals_at(world, params, t, setups)
        for d in day:
            s = world['series'][d['tk']]
            d['o10'] = outcome(s, t, d['lv'], '10')
            d['o2R'] = outcome(s, t, d['lv'], '2R')
        sigs += day
    return sigs


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build'); b.add_argument('--ohlc', required=True); b.add_argument('--out', required=True)
    s = sub.add_parser('scan'); s.add_argument('--world', required=True); s.add_argument('--out', required=True)
    s.add_argument('--step', type=int, default=5); s.add_argument('--params')
    a = ap.parse_args(argv)
    if a.cmd == 'build':
        build_world(a.ohlc, a.out)
        return 0
    world = pickle.loads(pathlib.Path(a.world).read_bytes())
    P = copy.deepcopy(bs.PARAMS)
    if a.params:
        for k, v in json.loads(a.params).items():
            P[k].update(v) if isinstance(v, dict) else P.__setitem__(k, v)
    t0 = time.time()
    sigs = scan(world, P, a.step)
    pathlib.Path(a.out).write_bytes(pickle.dumps({'params': P, 'signals': sigs, 'cal': world['cal']}, protocol=4))
    print(f'{len(sigs)} signals in {time.time() - t0:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
