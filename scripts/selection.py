"""Quality-not-quota pick selection (added 2026-10-05; v2 stop-from-close rules 2026-10-05 evening). Pure functions, no I/O.

A scored candidate becomes a technical pick only if it passes ALL of these rules (zero picks is a valid result):
  readiness   composite readiness (current formula, v2) >= CONFIG['minReadiness']          (a tuning guess: change it here only)
  base        a valid base: the pivot is not the last session, >= CONFIG['minSessionsSincePivot'] sessions since the pivot
  proximity   the last close is within CONFIG['maxBelowPivotPct'] % of the pivot (further below it is not set up yet)
  stop-valid  the stop (placed by place_stop) is BELOW the last close: a hard validity check, no exceptions
  risk        risk = (entry - stop) / entry <= CONFIG['maxRiskPct'] %
  rr          reward:risk >= CONFIG['minRR'] at the FIXED target of entry x (1 + CONFIG['targetPct'] %)
  earnings    no earnings within CONFIG['earningsBlackoutSessions'] trading sessions after the last close; a name whose
              earnings date could not be verified is rejected ("earnings date unverified")
The stop is placed, not copied: place_stop() takes the LOWEST of: 5% below the entry, the last session's low (only if it is 4-6% below
the entry), 1 x ATR14 below the LAST CLOSE and 0.5 x ADR20 below the LAST CLOSE. So the close-based floors widen the stop to the floor,
and the risk / R:R gates then decide whether that wider stop is acceptable. The universe filters (price, cap, volume, ADR, trend) are
applied upstream by scan_build.py. Qualifiers are ranked by readiness (then proximity, then ticker); beyond CONFIG['maxPicks'] the rest
become near-misses with the reason "cut at 10". Thresholds are not tuned to get a longer list.
"""
import datetime as dt, math
import market_time as mt
from health import MIN_STOP_ATR

CONFIG = {
    'minReadiness': 55,
    'maxPicks': 10,
    'minSessionsSincePivot': 2,
    'maxBelowPivotPct': 5.0,
    'stopMinAdr': 0.5,
    'stopMinAtr': MIN_STOP_ATR,
    'maxRiskPct': 8.0,
    'minRR': 1.5,
    'targetPct': 10.0,
    'earningsBlackoutSessions': 3,
    'tierBMinReadiness': 45,        # Tier B: valid but flagged; fills the list up to maxPicks in total
}
EARNINGS_STALE_DAYS = 45
SELECTION_VERSION = 'tiered-v3'                          # stored on every new ledger record (Tier A = the v2 rules; Tier B adds flagged names)
TIER_A_VERSION = 'quality-max10-v2-stop-from-close'
PREVIOUS_SELECTION_VERSION = 'quality-max10-v1'          # the first (reject-on-stop) rules; no ledger records were written under it
LEGACY_SELECTION_VERSION = 'top4-v1'                     # the 16 earlier records
LEVEL_RULES = ('proximity', 'stop-valid', 'risk', 'rr')
TIER_B_SOFT = ('readiness', 'proximity', 'risk', 'rr')       # rules a Tier B name may fail (they become chips; readiness only between the Tier B bar and the Tier A bar); every other rule is a validity check


def sessions_until(last_session, d):
    """Number of trading sessions after last_session up to and including date d (0 if d <= last_session)."""
    last, d = (dt.date.fromisoformat(x) if isinstance(x, str) else x for x in (last_session, d))
    n, cur = 0, last
    while cur < d:
        cur += dt.timedelta(days=1)
        if mt.is_trading_day(cur):
            n += 1
    return n


def place_stop(c, cfg=None):
    """(stop, which) - the lowest of the candidate stops, or (None, reason) if ATR14 / ADR20 are unknown."""
    cfg = {**CONFIG, **(cfg or {})}
    entry, close, atr, adr, low = c.get('entry'), c.get('lastClose'), c.get('atr14'), c.get('adr20Pct'), c.get('lastLow')
    if entry is None or close is None or not atr or not adr:
        return None, 'ATR14/ADR20 unknown'
    cands = {'5% below the entry': entry * 0.95,
             f"{cfg['stopMinAtr']:g}x ATR14 below the close": close - cfg['stopMinAtr'] * atr,
             f"{cfg['stopMinAdr']:g}x ADR20 below the close": close - cfg['stopMinAdr'] * adr / 100 * close}
    if low is not None and 0.04 <= (entry - low) / entry <= 0.06:
        cands["the last session's low"] = low
    which = min(cands, key=cands.get)
    return math.floor(cands[which] * 100 + 1e-9) / 100, which          # round DOWN to the cent: a rounded-up stop could sit just inside its own floor


def levels(c, cfg=None):
    """The levels the rules judge: placed stop, fixed target, risk %, R:R (None where the inputs are missing)."""
    cfg = {**CONFIG, **(cfg or {})}
    stop, which = place_stop(c, cfg)
    if stop is None:
        return {'stop': None, 'stopNote': which, 'target': None, 'riskPct': None, 'rr': None}
    entry = c['entry']
    target = round(entry * (1 + cfg['targetPct'] / 100), 2)
    risk = (entry - stop) / entry * 100
    return {'stop': stop, 'stopNote': which, 'target': target, 'riskPct': round(risk, 2),
            'rr': round((target - entry) / (entry - stop), 2) if entry > stop else None}


def check(c, cfg=None):
    """Rule results for one candidate -> {'passed', 'failed': [{'rule','reason'}], 'rules': {rule: bool}, 'levels': {...}}.

    c needs: readiness{composite}, timeInBaseDays, lastClose, pivot, entry, lastLow, atr14, adr20Pct, lastSession,
             nextEarnings (ISO date or None)."""
    cfg = {**CONFIG, **(cfg or {})}
    failed, rules = [], {}

    def rule(name, ok, reason):
        rules[name] = bool(ok)
        if not ok:
            failed.append({'rule': name, 'reason': reason})

    lv = levels(c, cfg)
    comp = (c.get('readiness') or {}).get('composite')
    rule('readiness', comp is not None and comp >= cfg['minReadiness'],
         f"readiness {comp} < {cfg['minReadiness']}" if comp is not None else 'not scored')
    tib = c.get('timeInBaseDays')
    rule('base', tib is not None and tib >= cfg['minSessionsSincePivot'],
         f"only {tib} session(s) since the pivot (need {cfg['minSessionsSincePivot']}): same-day or too-fresh breakout")
    last, pivot = c.get('lastClose'), c.get('pivot')
    below = None if (last is None or not pivot) else (pivot - last) / pivot * 100
    rule('proximity', below is not None and below <= cfg['maxBelowPivotPct'],
         'pivot unknown' if below is None else f"close is {below:.1f}% below the pivot (max {cfg['maxBelowPivotPct']:g}%): not set up yet")
    stop = lv['stop']
    rule('stop-valid', stop is not None and last is not None and stop < last,
         lv['stopNote'] if stop is None else f'stop {stop} is not below the last close {last}')
    rule('risk', lv['riskPct'] is not None and lv['riskPct'] <= cfg['maxRiskPct'],
         'risk unknown' if lv['riskPct'] is None else f"risk {lv['riskPct']:.1f}% of the entry (max {cfg['maxRiskPct']:g}%) with the stop placed at {lv['stopNote']}")
    rule('rr', lv['rr'] is not None and lv['rr'] >= cfg['minRR'],
         'R:R unknown' if lv['rr'] is None else f"R:R {lv['rr']:.2f} at the fixed +{cfg['targetPct']:g}% target (needs {cfg['minRR']:g})")
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
    return {'passed': not failed, 'failed': failed, 'rules': rules, 'levels': lv}


def rank_key(c):
    return (-(c['readiness']['composite']), -(c['readiness'].get('proximity') or 0), c['tk'])


def with_levels(c, cfg=None):
    """A copy of the candidate carrying the placed stop / fixed target (what the pick card shows and the ledger records)."""
    lv = levels(c, cfg)
    return {**c, 'stop': lv['stop'], 'stopNote': lv['stopNote'], 'target': lv['target'], 'riskPct': lv['riskPct'], 'rr': lv['rr']}


def select(cands, cfg=None):
    """Apply the rules to every scored candidate. Returns
    {'picks': [...top max by readiness, with the placed levels], 'cut': [...], 'rejected': [{'tk','failed':[...]}],
     'scored': n, 'qualified': n (before the cut), 'config': {...}}."""
    cfg = {**CONFIG, **(cfg or {})}
    ok, rej = [], []
    for c in cands:
        r = check(c, cfg)
        (ok if r['passed'] else rej).append((c, r))
    ok.sort(key=lambda t: rank_key(t[0]))
    picks = [with_levels(c, cfg) for c, _ in ok[:cfg['maxPicks']]]
    cut = [with_levels(c, cfg) for c, _ in ok[cfg['maxPicks']:]]
    return {'picks': picks, 'cut': cut,
            'rejected': [{'tk': c['tk'], 'failed': r['failed'], 'readiness': (c.get('readiness') or {}).get('composite'), 'levels': r['levels']} for c, r in rej],
            'scored': len(cands), 'qualified': len(ok), 'config': cfg}


def failure_summary(cands, cfg=None):
    """Why the list is as short as it is: how many names fail each rule, and which names reach the readiness bar but are stopped only by
    the level rules (proximity / stop / risk / R:R). The page uses this to explain an empty or short picks section."""
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
            'blockedOnlyLevels': [b for b in blocked if all(x in LEVEL_RULES for x in b['rules'])]}


def funnel(cands, cfg=None, order=('readiness', 'base', 'proximity', 'stop-valid', 'risk', 'rr', 'earnings')):
    """Sequential funnel: how many names are still alive after each gate, in the order given (for the sanity report)."""
    cfg = {**CONFIG, **(cfg or {})}
    alive = list(cands)
    out = [('scored', len(alive), [])]
    for g in order:
        keep = [c for c in alive if check(c, cfg)['rules'][g]]
        out.append((g, len(keep), [c['tk'] for c in alive if c not in keep][:0]))
        alive = keep
    return out, alive


def threshold_counts(cands, thresholds=(45, 55, 65), cfg=None):
    """How many names pass every rule at each readiness threshold (for tuning minReadiness), plus the score distribution."""
    out = {}
    for t in thresholds:
        out[str(t)] = sum(1 for c in cands if check(c, {**(cfg or {}), 'minReadiness': t})['passed'])
    scores = sorted(((c.get('readiness') or {}).get('composite') for c in cands if c.get('readiness')), reverse=True)
    return {'passing': out, 'scores': scores}


def header_line(sel):
    return f"{sel['qualified']} of {sel['scored']} scored names qualified (readiness ≥ {sel['config']['minReadiness']})"


def tier_b_chips(c, r, cfg):
    """Chips for a Tier B name: one per soft rule it failed, carrying the number. Red = tone 'bad'."""
    lv, chips = r['levels'], []
    for f in r['failed']:
        if f['rule'] == 'readiness':
            chips.append({'key': 'readiness', 'label': f"readiness {c['readiness']['composite']} (< {cfg['minReadiness']})", 'tone': 'warn'})
        elif f['rule'] == 'proximity':
            last, pivot = c['lastClose'], c['pivot']
            chips.append({'key': 'far-from-pivot', 'label': f"far from pivot ({(pivot - last) / pivot * 100:.1f}%)", 'tone': 'warn'})
        elif f['rule'] == 'risk':
            chips.append({'key': 'wide-stop', 'label': f"wide stop ({lv['riskPct']:.1f}%)", 'tone': 'warn'})
        elif f['rule'] == 'rr':
            chips.append({'key': 'rr', 'label': f"R:R {lv['rr']:.2f}", 'tone': 'bad' if lv['rr'] < 1 else 'warn'})
    return chips


def select_tiered(cands, cfg=None):
    """Tier A = every rule (the v2 list, at most maxPicks). Tier B = fills the list up to maxPicks in TOTAL with names that fail only soft rules
    (readiness >= tierBMinReadiness, proximity, risk, R:R) and pass the validity checks (a valid base, a stop below the close from the close-based floors,
    earnings outside the blackout and verified; the universe filters are applied upstream). A Tier B name carries chips instead of an exclusion; its levels are
    the same placed stop / fixed +10% target. Returns select()'s dict with `picks` = A then B, each with `tier`, plus `tierA`/`tierB` counts and `bRejected`."""
    cfg = {**CONFIG, **(cfg or {})}
    base = select(cands, cfg)
    a = [{**p, 'tier': 'A', 'chips': []} for p in base['picks']]
    room = cfg['maxPicks'] - len(a)
    bcfg = {**cfg, 'minReadiness': cfg['tierBMinReadiness']}
    b, b_rej = [], []
    taken = {p['tk'] for p in base['picks']} | {p['tk'] for p in base['cut']}
    for c in cands:
        if c['tk'] in taken:
            continue
        r = check(c, bcfg)
        hard = [f for f in r['failed'] if f['rule'] not in TIER_B_SOFT or f['rule'] == 'readiness']      # under the Tier B bar itself = not a candidate
        if hard:
            b_rej.append({'tk': c['tk'], 'hardFailed': [f['rule'] for f in hard]})
            continue
        r_full = check(c, cfg)
        b.append((c, {**with_levels(c, cfg), 'tier': 'B', 'chips': tier_b_chips(c, r_full, cfg)}))
    b.sort(key=lambda t: rank_key(t[0]))
    chosen = [x for _, x in b[:max(room, 0)]]
    for i, p in enumerate(a + chosen, 1):
        p['selectionRank'] = i
    return {**base, 'picks': a + chosen, 'tierA': len(a), 'tierB': len(chosen), 'tierBCut': [x['tk'] for _, x in b[max(room, 0):]],
            'bRejected': b_rej, 'rejected': [x for x in base['rejected'] if x['tk'] not in {p['tk'] for p in chosen}]}
