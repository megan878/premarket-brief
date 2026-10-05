"""Catalyst "priced-in" score, 0-100 (added 2026-10-05). 0 = not priced in (room left), 100 = fully priced in (the move is done).

Four components, each 0-25, more priced in = more points:
  a  MOVE SIZE   event-day (reaction-day) move / ADR20 (ADR over the 20 sessions BEFORE the event): <=1x -> 0, >=3x -> 25, linear between.
  b  FOLLOW-THROUGH / EXHAUSTION   25 x (0.6 x fade + 0.4 x age)
        fade = 0 when the last price is at/above the reaction-day high, 1 when at/below its low, linear in between (gap faded = high)
        age  = 0 for sessions-since-event 1..3 (reaction day = session 1), 1 from session 5, linear between (flat for 5+ days = high)
  c  DISTANCE TO CONSENSUS   % below the updated average analyst target: <=5% -> 25, >=25% -> 0, linear; at/above target -> 25.
  d  POSITIONING   % gain in the 20 sessions BEFORE the event: <=0% -> 0, >=20% -> 25, linear.
Score = sum of the available components / (25 x n available) x 100, rounded half up. n < 4 is shown as "n/4 components" and is
"partial" (the cloud routine has no OHLC, so a, b (the event range) and d may be missing).
Bands: 0-30 room left | 31-60 partly priced in | 61-100 mostly or fully priced in.
All anchors live in CONFIG so they can be tuned in one place.
"""
import math

CONFIG = {
    'a': {'lo': 1.0, 'hi': 3.0},            # event move as a multiple of ADR20
    'b': {'fadeWeight': 0.6, 'ageWeight': 0.4, 'ageStart': 3, 'ageFull': 5},
    'c': {'lo': 5.0, 'hi': 25.0},           # % below consensus target: <=lo -> 25, >=hi -> 0
    'd': {'lo': 0.0, 'hi': 20.0},           # % run-up before the event
    'bands': [(30, 'room left'), (60, 'partly priced in'), (100, 'mostly or fully priced in')],
    'maxPoints': 25,
}
COMPONENTS = ('a', 'b', 'c', 'd')
NAMES = {'a': 'Move vs volatility', 'b': 'Follow-through', 'c': 'Distance to target', 'd': 'Run-up before'}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _lin(x, x0, x1, y0, y1):
    """Linear from (x0,y0) to (x1,y1), clamped at the ends."""
    if x0 == x1:
        return y1
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def round_half_up(v):
    return int(math.floor(v + 0.5 + 1e-9))


def comp_a(move_pct, adr20_pct, cfg=None):
    c = (cfg or CONFIG)['a']
    if not (_num(move_pct) and _num(adr20_pct)) or adr20_pct <= 0:
        return None
    return _lin(abs(move_pct) / adr20_pct, c['lo'], c['hi'], 0, CONFIG['maxPoints'])


def comp_b(last, event_high, event_low, sessions_since, cfg=None):
    c = (cfg or CONFIG)['b']
    if not all(_num(x) for x in (last, event_high, event_low, sessions_since)) or event_high <= event_low:
        return None
    fade = _lin(event_high - last, 0, event_high - event_low, 0, 1)
    age = _lin(sessions_since, c['ageStart'], c['ageFull'], 0, 1)
    return CONFIG['maxPoints'] * (c['fadeWeight'] * fade + c['ageWeight'] * age)


def comp_c(price, target, cfg=None):
    c = (cfg or CONFIG)['c']
    if not (_num(price) and _num(target)) or target <= 0:
        return None
    below = (target - price) / target * 100
    return _lin(below, c['lo'], c['hi'], CONFIG['maxPoints'], 0)


def comp_d(runup_pct, cfg=None):
    c = (cfg or CONFIG)['d']
    if not _num(runup_pct):
        return None
    return _lin(runup_pct, c['lo'], c['hi'], 0, CONFIG['maxPoints'])


def band(score):
    for hi, name in CONFIG['bands']:
        if score <= hi:
            return name
    return CONFIG['bands'][-1][1]


def score(inp, cfg=None):
    """inp keys (any may be missing): movePct, adr20Pct, last, eventHigh, eventLow, sessionsSince, price, target, runUpPct.
    Returns {'score','band','label','n','partial','components':{a..d: points|None}, 'inputs'} ; score None when nothing is available."""
    pts = {'a': comp_a(inp.get('movePct'), inp.get('adr20Pct'), cfg),
           'b': comp_b(inp.get('last'), inp.get('eventHigh'), inp.get('eventLow'), inp.get('sessionsSince'), cfg),
           'c': comp_c(inp.get('price'), inp.get('target'), cfg),
           'd': comp_d(inp.get('runUpPct'), cfg)}
    avail = {k: v for k, v in pts.items() if v is not None}
    n = len(avail)
    if not n:
        return {'score': None, 'band': None, 'label': 'Priced-in: not scored (no inputs)', 'n': 0, 'partial': True,
                'components': {k: None for k in COMPONENTS}, 'inputs': dict(inp)}
    s = round_half_up(sum(avail.values()) / (CONFIG['maxPoints'] * n) * 100)
    b = band(s)
    return {'score': s, 'band': b, 'n': n, 'partial': n < 4,
            'label': f'Priced-in {s}/100 — {b}' + (f' ({n}/4 components, partial)' if n < 4 else ''),
            'components': {k: (None if v is None else round(v, 1)) for k, v in pts.items()}, 'inputs': dict(inp)}


def rank_alerts(items):
    """Catalyst alerts, lowest priced-in score first; unscored last; ties by ticker."""
    def key(c):
        s = (c.get('pricedIn') or {}).get('score')
        return (1, 0, c['tk']) if s is None else (0, s, c['tk'])
    return sorted(items, key=key)


def inputs_from_ohlc(rows, reaction_date, last_session, price=None, target=None):
    """Event inputs from validated, date-sorted daily rows (oldest first). reaction_date = the first session that traded the news
    (the announcement day if it came before the open, the next session if it came after the close)."""
    idx = {r['date']: i for i, r in enumerate(rows)}
    if reaction_date not in idx or idx[reaction_date] < 21:
        return {'price': price, 'target': target}
    i = idx[reaction_date]
    ev, prev = rows[i], rows[i - 1]
    pre = rows[i - 20:i]                                   # the 20 sessions before the event
    adr = 100 * (sum(r['high'] / r['low'] for r in pre) / 20 - 1)
    last_i = idx.get(last_session, len(rows) - 1)
    return {'movePct': round((ev['close'] / prev['close'] - 1) * 100, 2), 'adr20Pct': round(adr, 3),
            'eventHigh': ev['high'], 'eventLow': ev['low'], 'last': rows[last_i]['close'], 'sessionsSince': last_i - i + 1,
            'runUpPct': round((prev['close'] / rows[i - 21]['close'] - 1) * 100, 2),
            'price': price if price is not None else rows[last_i]['close'], 'target': target, 'reactionDate': reaction_date}
