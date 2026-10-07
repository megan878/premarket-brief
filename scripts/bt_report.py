"""Backtest statistics: the same definition of a win as the ledger (tracker.replay outcomes), split by setup / rank bucket / period.

A signal is a "trade" when replay filled it and closed it (stopped / target / time-exit); R is the tracker's rMultiple. Signals that never
filled (expired / invalidated / skipped) count in n and in the trigger rate but contribute 0 R to the per-signal expectancy.
"""
import math
import numpy as np

CLOSED = ('stopped', 'target', 'time-exit')


def stats(items, key='o10'):
    n = len(items)
    if not n:
        return {'n': 0}
    closed = [(it['date'], it[key]['exitDate'], it[key]['R']) for it in items if it[key]['status'] in CLOSED and it[key]['R'] is not None]
    rs = [r for _, _, r in closed]
    wins = [r for r in rs if r > 0]
    # worst drawdown of the cumulative R curve, trades ordered by exit date
    curve, peak, dd = 0.0, 0.0, 0.0
    for _, ex, r in sorted(closed, key=lambda x: (x[1] or '', x[0])):
        curve += r
        peak = max(peak, curve)
        dd = min(dd, curve - peak)
    return {'n': n, 'trades': len(rs), 'trig': len(rs) / n, 'win': (len(wins) / len(rs)) if rs else float('nan'),
            'avgR': float(np.mean(rs)) if rs else float('nan'), 'exp': float(sum(rs) / n), 'worstDD': dd,
            'sumR': float(sum(rs)), 'se': float(np.std(rs) / math.sqrt(len(rs))) if len(rs) > 1 else float('nan')}


def fmt(s):
    if not s.get('n'):
        return 'n=0'
    f = lambda x, p=2: 'n/a' if (x is None or (isinstance(x, float) and math.isnan(x))) else f'{x:.{p}f}'   # noqa: E731
    return (f"n={s['n']:>5} trades={s['trades']:>4} trig={f(s['trig'] * 100, 0):>3}% win={f(s['win'] * 100, 0):>3}% avgR={f(s['avgR']):>6} "
            f"exp/signal={f(s['exp']):>6} (±{f(s['se'])}) worstDD={f(s['worstDD'], 1)}R")


def split_dates(signals_or_dates, frac=0.6):
    dates = sorted({x if isinstance(x, str) else x['date'] for x in signals_or_dates})
    k = int(len(dates) * frac)
    return set(dates[:k]), set(dates[k:]), dates[k - 1] if k else None


def bucket(rank):
    if rank is None:
        return 'unranked'
    return 'top 3' if rank <= 3 else '4-10' if rank <= 10 else '11+'
