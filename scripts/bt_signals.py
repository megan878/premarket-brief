"""Point-in-time indicators and setup detectors for the backtest and for the live scan (research code; nothing here is wired into the page).

Every indicator is CAUSAL (a rolling window that ends at t), so a value read at index t can only use data up to t. `tests/test_bt_signals.py`
checks that by recomputing a value on truncated data. Detectors:

  a  base       within `maxBelowPivotPct` of the 25-session pivot, base >= `minBase` sessions, readiness (v2) as the setup quality
  b  breakout   close above the prior 25-session pivot within the last 1-3 sessions on >= 1.5x volume, close in the top 30% of the day's range,
                no more than 1.5 ATR above the pivot; entry above the highest high since the breakout day
  c  pullback   price > SMA50 > SMA200, RS percentile >= 70, 3-8% off the 20-day high (or <= 1.5 ATR), close within 1 ATR of EMA21,
                pullback volume below average, RSI14 40-55; entry above the signal day's high
  d  flag       prior 20-session gain >= 20%, then 3-10 sessions of range contraction on falling volume; entry above the flag high

All starting values live in PARAMS (tuned on the first 60% of the history only, judged on the rest). Each signal carries its own entry, structure stop
and invalidation text; `finalize_levels` then applies the close-based floors (stop at least 1 x ATR14 and 0.5 x ADR20 below the LAST CLOSE).
"""
import math
import numpy as np
import pandas as pd

import tracker as tr

PARAMS = {
    'universe': {'minPrice': 5.0, 'minAvgVol': 500_000, 'minAdr': 2.0, 'minCap': 5e9},
    'a': {'maxBelowPivotPct': 5.0, 'minBase': 5, 'minReadiness': 55},
    'b': {'volMult': 1.5, 'closeTopFrac': 0.30, 'maxExtAtr': 1.5, 'maxAge': 3},
    'c': {'minRsPct': 70, 'depthLo': 3.0, 'depthHi': 8.0, 'depthAtrMax': 1.5, 'ema21Atr': 1.0, 'rsiLo': 40, 'rsiHi': 55, 'stopAtr': 0.25},
    'd': {'minRun': 20.0, 'minFlag': 3, 'maxFlag': 10, 'contraction': 0.75, 'volFall': 0.85},
    'floors': {'atr': 1.0, 'adr': 0.5},
    'targetPct': 10.0, 'targetR': 2.0,
}
SETUPS = ('base', 'breakout', 'pullback', 'flag')


# ───────────────────────────── indicators (all causal) ─────────────────────────────
def indicators(df):
    """df: columns open, high, low, close, volume (NaN allowed before the first listing). Returns a DataFrame of causal indicators."""
    o, h, l, c, v = df['open'], df['high'], df['low'], df['close'], df['volume']
    pc = c.shift(1)
    trr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    out = pd.DataFrame(index=df.index)
    out['atr14'] = trr.rolling(14).mean()
    out['adr20'] = ((h / l).rolling(20).mean() - 1) * 100
    for n in (20, 50, 200):
        out[f'sma{n}'] = c.rolling(n).mean()
    out['ema9'] = c.ewm(span=9, adjust=False).mean()
    out['ema21'] = c.ewm(span=21, adjust=False).mean()
    out['avgvol20'] = v.rolling(20).mean()
    out['avgvol50'] = v.rolling(50).mean()
    # Wilder RSI / ADX (ewm alpha 1/14)
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    out['rsi14'] = 100 - 100 / (1 + rs)
    pdm = ((h - h.shift(1)).where((h - h.shift(1)) > (l.shift(1) - l), 0)).clip(lower=0)
    ndm = ((l.shift(1) - l).where((l.shift(1) - l) > (h - h.shift(1)), 0)).clip(lower=0)
    atrw = trr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi, ndi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atrw, 100 * ndm.ewm(alpha=1 / 14, adjust=False).mean() / atrw
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    out['adx14'] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    out['hh25'] = h.rolling(25).max()
    out['hh20'] = h.rolling(20).max()
    out['hh25_prev'] = h.shift(1).rolling(25).max()                 # the pivot as it stood BEFORE today
    out['ret21'], out['ret63'], out['ret126'] = c / c.shift(21) - 1, c / c.shift(63) - 1, c / c.shift(126) - 1
    upv = v.where(c > pc, 0.0)
    dnv = v.where(c < pc, 0.0)
    out['updown20'] = upv.rolling(20).sum() / dnv.rolling(20).sum().replace(0, np.nan)
    return out


def argmax_age(high, t, n=25):
    """Sessions since the highest high of the n sessions ending at t (0 = today); first occurrence of the max."""
    w = high[t - n + 1:t + 1]
    return n - 1 - int(np.argmax(w))


# ───────────────────────────── stop / target placement ─────────────────────────────
def finalize_levels(entry, struct_stop, close, atr, adr_pct, floors=None, target_pct=None, target_r=None):
    """Structure stop with the close-based floors; both target models. Returns None if no valid stop below the close exists."""
    fl = floors or PARAMS['floors']
    tp = PARAMS['targetPct'] if target_pct is None else target_pct
    tR = PARAMS['targetR'] if target_r is None else target_r
    if not (atr and atr > 0 and adr_pct and adr_pct > 0 and entry and struct_stop):
        return None
    cands = [struct_stop, close - fl['atr'] * atr, close - fl['adr'] * adr_pct / 100 * close]
    stop = math.floor(min(cands) * 100 + 1e-9) / 100
    if not (stop < close and stop < entry):
        return None
    risk = entry - stop
    return {'entry': round(entry, 2), 'stop': stop, 'riskPct': round(risk / entry * 100, 2),
            'target10': round(entry * (1 + tp / 100), 2), 'target2R': round(entry + tR * risk, 2), 'target3R': round(entry + 3 * risk, 2),
            'rr10': round((entry * tp / 100) / risk, 2)}


# ───────────────────────────── detectors (scalar, at index t) ─────────────────────────────
class Series:
    """One ticker: price arrays + causal indicators, for scalar lookups at index t."""

    def __init__(self, tk, df, ind, meta):
        self.tk, self.meta = tk, meta
        self.o, self.h, self.l, self.c, self.v = (df[k].to_numpy(float) for k in ('open', 'high', 'low', 'close', 'volume'))
        self.ind = {k: ind[k].to_numpy(float) for k in ind.columns}
        self.dates = list(df.index)
        self.capnow = meta['cap']

    def cap_at(self, t):
        return self.capnow * self.c[t] / self.c[-1] if not math.isnan(self.c[t]) else float('nan')


def universe_ok(s, t, p=None):
    u = (p or PARAMS)['universe']
    I = s.ind
    c = s.c[t]
    if t < 220 or math.isnan(c) or math.isnan(I['sma200'][t]):
        return False
    return (c > u['minPrice'] and I['avgvol20'][t] > u['minAvgVol'] and I['adr20'][t] > u['minAdr'] and s.cap_at(t) >= u['minCap'])


def rows_window(s, t, n=25):
    return [{'date': s.dates[i], 'open': s.o[i], 'high': s.h[i], 'low': s.l[i], 'close': s.c[i], 'volume': s.v[i]} for i in range(t - n + 1, t + 1)]


def detect_base(s, t, p=None):
    pa = (p or PARAMS)['a']
    I = s.ind
    pivot = I['hh25'][t]
    c = s.c[t]
    if math.isnan(pivot) or (pivot - c) / pivot * 100 > pa['maxBelowPivotPct']:
        return None
    tib = argmax_age(s.h, t)
    if tib < pa['minBase']:
        return None
    rd = tr.readiness_v2(rows_window(s, t))
    if rd['composite'] < pa['minReadiness']:
        return None
    lows = s.l[t - tib + 1:t + 1]
    struct = float(np.min(lows)) - 0.25 * I['atr14'][t]
    lv = finalize_levels(pivot + 0.01, struct, c, I['atr14'][t], I['adr20'][t], (p or PARAMS)['floors'])
    if not lv:
        return None
    return {'setup': 'base', 'quality': float(rd['composite']), 'lv': lv, 'tib': tib,
            'trigger': f"close above {pivot:.2f} (the {tib}-session-old 25-day high)", 'invalidation': f"close/low under {lv['stop']:.2f}",
            'why': f"{tib}-session base {(pivot - c) / pivot * 100:.1f}% under its pivot, readiness {rd['composite']}"}


def detect_breakout(s, t, p=None):
    pb = (p or PARAMS)['b']
    I = s.ind
    for age in range(0, pb['maxAge']):
        j = t - age
        piv = I['hh25_prev'][j]
        if math.isnan(piv) or s.c[j] <= piv:
            continue
        if j > 0 and s.c[j - 1] > I['hh25_prev'][j - 1]:        # not the first close above
            continue
        rng = s.h[j] - s.l[j]
        if rng <= 0 or I['avgvol20'][j - 1] is None or math.isnan(I['avgvol20'][j - 1]):
            continue
        volx = s.v[j] / I['avgvol20'][j - 1]
        pos = (s.c[j] - s.l[j]) / rng
        ext = (s.c[t] - piv) / I['atr14'][t]
        if volx < pb['volMult'] or pos < 1 - pb['closeTopFrac'] or ext > pb['maxExtAtr']:
            continue
        entry = float(np.max(s.h[j:t + 1])) + 0.01
        struct = min(s.l[j], piv - 0.5 * I['atr14'][t])
        lv = finalize_levels(entry, struct, s.c[t], I['atr14'][t], I['adr20'][t], (p or PARAMS)['floors'])
        if not lv:
            continue
        q = 50 * min(volx / 3, 1) + 30 * pos + 20 * (1 - max(ext, 0) / pb['maxExtAtr'])
        return {'setup': 'breakout', 'quality': float(q), 'lv': lv, 'age': age,
                'trigger': f"buy above {entry:.2f} (high since the breakout day)", 'invalidation': f"back under {lv['stop']:.2f} (breakout-day low / pivot - 0.5 ATR)",
                'why': f"closed above {piv:.2f} {age} session(s) ago on {volx:.1f}x volume, {ext:.1f} ATR extended"}
    return None


def detect_pullback(s, t, p=None, rs_pct=None):
    pc = (p or PARAMS)['c']
    I = s.ind
    c = s.c[t]
    if not (c > I['sma50'][t] > I['sma200'][t]) or rs_pct is None or rs_pct < pc['minRsPct']:
        return None
    hh = I['hh20'][t]
    depth = (hh - c) / hh * 100
    depth_atr = (hh - c) / I['atr14'][t]
    ok_depth = (pc['depthLo'] <= depth <= pc['depthHi']) or (depth >= 1.5 and depth_atr <= pc['depthAtrMax'])
    if not ok_depth or abs(c - I['ema21'][t]) > pc['ema21Atr'] * I['atr14'][t]:
        return None
    if not (pc['rsiLo'] <= I['rsi14'][t] <= pc['rsiHi']):
        return None
    k = 20 - 1 - int(np.argmax(s.h[t - 19:t + 1]))                    # sessions since the 20-day high
    if k < 1:
        return None
    if np.mean(s.v[t - k + 1:t + 1]) >= I['avgvol20'][t]:             # pullback volume must be below average
        return None
    pull_low = float(np.min(s.l[t - k + 1:t + 1]))
    entry = s.h[t] + 0.01
    lv = finalize_levels(entry, pull_low - pc['stopAtr'] * I['atr14'][t], c, I['atr14'][t], I['adr20'][t], (p or PARAMS)['floors'])
    if not lv:
        return None
    q = 40 * (rs_pct / 100) + 30 * (1 - min(abs(depth - 5) / 5, 1)) + 30 * (1 - min(abs(c - I['ema21'][t]) / I['atr14'][t], 1))
    return {'setup': 'pullback', 'quality': float(q), 'lv': lv, 'depth': depth,
            'trigger': f"buy above yesterday's high {entry:.2f}", 'invalidation': f"under the pullback low {pull_low:.2f} - 0.25 ATR",
            'why': f"{depth:.1f}% off the 20-day high in an uptrend (RS {rs_pct:.0f}th pct), at EMA21, RSI {I['rsi14'][t]:.0f}, light volume"}


def detect_flag(s, t, p=None):
    pd_ = (p or PARAMS)['d']
    I = s.ind
    c = s.c
    for L in range(pd_['maxFlag'], pd_['minFlag'] - 1, -1):
        a = t - L + 1                                                   # first flag session
        if a - 21 < 0:
            continue
        run = (c[a - 1] / c[a - 21] - 1) * 100
        if run < pd_['minRun']:
            continue
        runwin = slice(a - 20, a)
        flag = slice(a, t + 1)
        rr = np.mean(s.h[runwin] - s.l[runwin])
        fr = np.mean(s.h[flag] - s.l[flag])
        if rr <= 0 or fr / rr > pd_['contraction']:
            continue
        if np.mean(s.v[flag]) / np.mean(s.v[runwin]) > pd_['volFall']:
            continue
        fh, fl = float(np.max(s.h[flag])), float(np.min(s.l[flag]))
        if c[t] < fl + 0.4 * (fh - fl):                                  # sitting in the lower part of the flag: not coiled under the high
            continue
        lv = finalize_levels(fh + 0.01, fl - 0.1 * I['atr14'][t], c[t], I['atr14'][t], I['adr20'][t], (p or PARAMS)['floors'])
        if not lv:
            continue
        q = 40 * min(run / 60, 1) + 30 * (1 - fr / rr) + 30 * (1 - np.mean(s.v[flag]) / np.mean(s.v[runwin]))
        return {'setup': 'flag', 'quality': float(max(0, min(100, q))), 'lv': lv, 'flagLen': L,
                'trigger': f"buy above the flag high {fh + 0.01:.2f}", 'invalidation': f"under the flag low {fl:.2f}",
                'why': f"{L}-session flag after a {run:.0f}% run, ranges {fr / rr * 100:.0f}% of the run's, volume drying up"}
    return None


# ───────────────────────────── composite ranking ─────────────────────────────
def trend_quality(s, t):
    I = s.ind
    c = s.c[t]
    stack = (c > I['sma50'][t]) + (I['sma50'][t] > I['sma200'][t]) + (I['ema9'][t] > I['ema21'][t])
    adx = I['adx14'][t]
    return 100 * (0.5 * stack / 3 + 0.5 * min(max((adx - 10) / 25, 0), 1))


def accumulation(s, t):
    r = s.ind['updown20'][t]
    if r is None or math.isnan(r):
        return 50.0
    return 100 * min(max((r - 0.5) / 1.5, 0), 1)                         # 0.5 -> 0, 2.0 -> 100


def composite(rs_score, quality, trend, accum, boost=0.0, w=(30, 25, 15, 15, 15)):
    """Weighted 0-100 rank score. The flags weight (last) is 0 in the backtest (flags need today's financials) and its weight is renormalised away."""
    parts = [rs_score, quality, trend, accum, boost]
    ws = list(w)
    if boost is None:
        parts, ws = parts[:4], ws[:4]
    tot = sum(ws)
    return sum(x * wi for x, wi in zip(parts, ws)) / tot
