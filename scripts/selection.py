"""Quality-not-quota pick selection (added 2026-10-05). Pure functions, no I/O.

A scored candidate becomes a technical pick only if it passes ALL of these rules (zero picks is a valid result):
  readiness   composite readiness (current formula, v2) >= CONFIG['minReadiness']          (55 was a starting guess: tune it here)
  base        a valid base: the pivot is not the last session, >= CONFIG['minSessionsSincePivot'] sessions since the pivot
  stop-adr    stop below the last close by at least CONFIG['stopMinAdr'] x ADR20 (ADR20 in price terms at the last close)
  stop-atr    stop below the last close by at least CONFIG['stopMinAtr'] x ATR14   (the 3 Oct floor; the stricter of the two wins)
  earnings    no earnings within CONFIG['earningsBlackoutSessions'] trading sessions after the last close; a name whose
              earnings date could not be verified from two sources is rejected ("earnings date unverified")
The universe filters (price, cap, volume, ADR, trend) are applied upstream by scan_build.py and are not repeated here.
Qualifiers are ranked by readiness (then proximity, then ticker); beyond CONFIG['maxPicks'] the rest become near-misses
with the reason "cut at 10".
"""
import datetime as dt
import market_time as mt
from health import MIN_STOP_ATR

CONFIG = {
    'minReadiness': 55,
    'maxPicks': 10,
    'minSessionsSincePivot': 2,
    'stopMinAdr': 0.5,
    'stopMinAtr': MIN_STOP_ATR,
    'earningsBlackoutSessions': 3,
}
EARNINGS_STALE_DAYS = 45
SELECTION_VERSION = 'quality-max10-v1'   # stored on every new ledger record; the 16 earlier records are 'top4-v1'
LEGACY_SELECTION_VERSION = 'top4-v1'


def sessions_until(last_session, d):
    """Number of trading sessions after last_session up to and including date d (0 if d <= last_session)."""
    last, d = (dt.date.fromisoformat(x) if isinstance(x, str) else x for x in (last_session, d))
    n, cur = 0, last
    while cur < d:
        cur += dt.timedelta(days=1)
        if mt.is_trading_day(cur):
            n += 1
    return n


def check(c, cfg=None):
    """Rule results for one candidate -> {'passed': bool, 'failed': [{'rule','reason'}], 'rules': {rule: bool}}.

    c needs: readiness{composite}, timeInBaseDays, lastClose, stop, atr14, adr20Pct, lastSession,
             nextEarnings (ISO date or None), optionally earningsSources (count of independent confirmations)."""
    cfg = {**CONFIG, **(cfg or {})}
    failed, rules = [], {}

    def rule(name, ok, reason):
        rules[name] = bool(ok)
        if not ok:
            failed.append({'rule': name, 'reason': reason})

    comp = (c.get('readiness') or {}).get('composite')
    rule('readiness', comp is not None and comp >= cfg['minReadiness'],
         f"readiness {comp} < {cfg['minReadiness']}" if comp is not None else 'not scored')
    tib = c.get('timeInBaseDays')
    rule('base', tib is not None and tib >= cfg['minSessionsSincePivot'],
         f"only {tib} session(s) since the pivot (need {cfg['minSessionsSincePivot']}): same-day or too-fresh breakout")
    last, stop, adr, atr = c.get('lastClose'), c.get('stop'), c.get('adr20Pct'), c.get('atr14')
    gap = None if (last is None or stop is None) else last - stop
    adr_px = None if (last is None or adr is None) else adr / 100 * last
    rule('stop-adr', gap is not None and adr_px is not None and gap >= cfg['stopMinAdr'] * adr_px,
         'stop/ADR20 unknown' if (gap is None or adr_px is None) else
         f"stop is {gap / adr_px:.2f} x ADR20 below the last close (needs {cfg['stopMinAdr']})" if gap > 0 else f'stop {stop} is at/above the last close {last}')
    rule('stop-atr', gap is not None and atr and gap >= cfg['stopMinAtr'] * atr,
         'ATR14 unknown' if not atr else (f"stop is {gap / atr:.2f} x ATR14 below the last close (needs {cfg['stopMinAtr']})" if gap is not None else 'stop unknown'))
    ne = c.get('nextEarnings')
    if not ne:
        rule('earnings', False, 'earnings date unverified')
    elif ne <= c['lastSession']:
        # a date on/before the last close is the report that just happened (the source has not rolled to the next one yet):
        # recent (<= 45 days) means the next report is about a quarter away; an older stale date is "unverified"
        age = (dt.date.fromisoformat(c['lastSession']) - dt.date.fromisoformat(ne)).days
        rule('earnings', age <= EARNINGS_STALE_DAYS, f'earnings date unverified (source still shows {ne})')
    else:
        n = sessions_until(c['lastSession'], ne)
        rule('earnings', not (n <= cfg['earningsBlackoutSessions']),
             f"earnings {ne} is {n} session(s) after the last close (blackout {cfg['earningsBlackoutSessions']})")
    return {'passed': not failed, 'failed': failed, 'rules': rules}


def rank_key(c):
    return (-(c['readiness']['composite']), -(c['readiness'].get('proximity') or 0), c['tk'])


def select(cands, cfg=None):
    """Apply the rules to every scored candidate. Returns
    {'picks': [...top max by readiness], 'cut': [... qualified but beyond max], 'rejected': [{'tk','failed':[...]}],
     'scored': n, 'qualified': n (before the cut), 'config': {...}}."""
    cfg = {**CONFIG, **(cfg or {})}
    ok, rej = [], []
    for c in cands:
        r = check(c, cfg)
        (ok if r['passed'] else rej).append((c, r))
    ok.sort(key=lambda t: rank_key(t[0]))
    picks = [c for c, _ in ok[:cfg['maxPicks']]]
    cut = [c for c, _ in ok[cfg['maxPicks']:]]
    return {'picks': picks, 'cut': cut,
            'rejected': [{'tk': c['tk'], 'failed': r['failed'], 'readiness': (c.get('readiness') or {}).get('composite')} for c, r in rej],
            'scored': len(cands), 'qualified': len(ok), 'config': cfg}


def failure_summary(cands, cfg=None):
    """Why the list is as short as it is: how many names fail each rule, and which names reach the readiness bar but are stopped by
    other rules (the page uses this to explain an empty or short picks section)."""
    cfg = {**CONFIG, **(cfg or {})}
    by, blocked, ok = {}, [], 0
    for c in cands:
        r = check(c, cfg)
        for f in r['failed']:
            by[f['rule']] = by.get(f['rule'], 0) + 1
        if r['rules'].get('readiness'):
            ok += 1
            if not r['passed']:
                blocked.append({'tk': c['tk'], 'readiness': c['readiness']['composite'], 'rules': [f['rule'] for f in r['failed']]})
    blocked.sort(key=lambda b: (-b['readiness'], b['tk']))
    return {'scored': len(cands), 'byRule': by, 'readinessOk': ok, 'minReadiness': cfg['minReadiness'], 'blocked': blocked,
            'blockedOnlyStop': [b for b in blocked if all(x.startswith('stop') for x in b['rules'])]}


def threshold_counts(cands, thresholds=(45, 55, 65), cfg=None):
    """How many names pass every rule at each readiness threshold (for tuning minReadiness), plus the score distribution."""
    out = {}
    for t in thresholds:
        out[str(t)] = sum(1 for c in cands if check(c, {**(cfg or {}), 'minReadiness': t})['passed'])
    scores = sorted(((c.get('readiness') or {}).get('composite') for c in cands if c.get('readiness')), reverse=True)
    return {'passing': out, 'scores': scores}


def header_line(sel):
    return f"{sel['qualified']} of {sel['scored']} scored names qualified (readiness ≥ {sel['config']['minReadiness']})"
