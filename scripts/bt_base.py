"""Baselines for the backtest, replayed with the same tracker.replay: top4-v1 (readiness v1, top 4, the old stop rule, +10%),
quality-max10-v2 (readiness v2 >= 55 and the v2 gates, up to 10, +10%) and a random pick from the same universe.

Earnings blackout is not applied (no historical earnings calendar in the source); it is the only rule of the live logic left out.
"""
import math, random
import numpy as np
import tracker as tr, selection as sel
import bt_signals as bs
import bt_run as br


def _cands(world, t, params, need_v2=False):
    out = []
    for tk, s in world['series'].items():
        if not bs.universe_ok(s, t, params):
            continue
        I = s.ind
        c = s.c[t]
        if not (c > I['sma20'][t] and c > I['sma50'][t] and c > I['sma200'][t]) or c > 1.2 * I['sma50'][t]:
            continue
        out.append(s)
    return out


def top4_v1(world, t, params):
    """Readiness v1 on the 25-session window, top 4, old stop rule (last low if 4-6% below the entry else 5% below; no close-based floor)."""
    best = []
    for s in _cands(world, t, params):
        win = bs.rows_window(s, t)
        if any(math.isnan(r['close']) for r in win):
            continue
        rd = tr.readiness_v1(win)
        pivot = max(r['high'] for r in win)
        entry = round(pivot + 0.01, 2)
        low = s.l[t]
        stop = round(low, 2) if 0.04 <= (entry - low) / entry <= 0.06 else round(entry * 0.95, 2)
        best.append((rd['composite'], s.tk, s, entry, stop))
    best.sort(key=lambda x: (-x[0], x[1]))
    res = []
    for rank, (comp, tk, s, entry, stop) in enumerate(best[:4], 1):
        lv = {'entry': entry, 'stop': stop, 'target10': round(entry * 1.10, 2), 'target2R': round(entry + 2 * (entry - stop), 2) if entry > stop else entry}
        res.append({'baseline': 'top4-v1', 't': t, 'date': s.dates[t], 'tk': tk, 'rank': rank, 'readiness': comp, 'lv': lv,
                    'o10': br.outcome(s, t, lv, '10')})
    return res


def quality_v2(world, t, params):
    cands = []
    for s in _cands(world, t, params):
        win = bs.rows_window(s, t)
        if any(math.isnan(r['close']) for r in win):
            continue
        rd = tr.readiness_v2(win)
        if rd['composite'] < 40:
            continue
        pivot = rd['pivot']
        cands.append({'tk': s.tk, 'readiness': {'composite': rd['composite'], 'proximity': rd['proximity']}, 'timeInBaseDays': rd['timeInBaseDays'],
                      'lastClose': s.c[t], 'pivot': pivot, 'entry': round(pivot + 0.01, 2), 'lastLow': s.l[t], 'atr14': s.ind['atr14'][t],
                      'adr20Pct': s.ind['adr20'][t], 'nextEarnings': '2099-01-01', 'lastSession': s.dates[t], '_s': s})
    r = sel.select(cands)
    res = []
    for rank, p in enumerate(r['picks'], 1):
        s = p['_s']
        lv = {'entry': p['entry'], 'stop': p['stop'], 'target10': p['target'], 'target2R': round(p['entry'] + 2 * (p['entry'] - p['stop']), 2)}
        res.append({'baseline': 'quality-max10-v2', 't': t, 'date': s.dates[t], 'tk': p['tk'], 'rank': rank, 'readiness': p['readiness']['composite'], 'lv': lv,
                    'o10': br.outcome(s, t, lv, '10')})
    return res


def random_pick(world, t, params, rng, k=5):
    pool = [world['series'][tk] for tk in world['series'] if bs.universe_ok(world['series'][tk], t, params)]
    res = []
    for s in rng.sample(pool, min(k, len(pool))):
        I = s.ind
        c = s.c[t]
        lv = bs.finalize_levels(c * 1.001, c - 1.5 * I['atr14'][t], c, I['atr14'][t], I['adr20'][t], params['floors'])
        if not lv:
            continue
        res.append({'baseline': 'random', 't': t, 'date': s.dates[t], 'tk': s.tk, 'rank': 0, 'lv': lv,
                    'o10': br.outcome(s, t, lv, '10'), 'o2R': br.outcome(s, t, lv, '2R')})
    return res


def run_baselines(world, params, step=5, first=240, only=None):
    n = len(world['cal'])
    idx = [i for i in range(first, n - 31, step) if only is None or i in only]
    rng = random.Random(11)
    out = []
    for t in idx:
        out += top4_v1(world, t, params)
        out += quality_v2(world, t, params)
        out += random_pick(world, t, params, rng)
    return out
