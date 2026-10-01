"""Validation, carry-forward and health reporting shared by both builders.

Philosophy
  * A data source failing must never crash the build or silently show old numbers.
  * Every section is validated on its own. Bad or missing -> reuse the last good copy of THAT section,
    mark it stale with its real as-of date, and say so in a banner.
  * Nothing is invented: if there is no fresh AND no previous copy, the section is 'failed' (empty state).
  * Numbers are sanity-checked because web-fetched pages are read by a summarizer that can garble them.
"""
import copy, datetime as dt, json, math, re
import market_time as mt

SECTIONS = ['indices', 'macro', 'sectors', 'industries', 'technical', 'catalysts', 'nearmiss']
CRITICAL = ['indices', 'sectors']      # without these the brief is not worth publishing
PINNED = {'industries', 'technical', 'nearmiss'}  # WebFetch is broken in the cloud sandbox (platform bug, confirmed
                                        # 2026-10-01 under Trusted/Custom/Full network access alike). The industry
                                        # drill-down needs Finviz's group/screener pages, the technical scan needs
                                        # Finviz + daily OHLC, and near-misses are leftovers of that same scan — none
                                        # of the three can be reproduced with FMP + WebSearch alone. Only sector
                                        # RANKING is automated (via WebSearch). Pinned sections are refreshed only
                                        # from an interactive session and simply carried forward verbatim otherwise.
LABEL = {'indices': 'Index & VIX quotes', 'macro': 'Rates & Fed', 'sectors': 'Sector strength',
         'industries': 'Industry drill-down', 'technical': 'Technical picks', 'catalysts': 'Catalyst alerts',
         'nearmiss': 'Near-miss watchlist'}
CATALYST_WINDOW = 5      # trading days


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_cap(s):
    m = re.match(r'\$?([\d.]+)\s*([BMT])', str(s or ''))
    return float(m.group(1)) * {'M': 1e6, 'B': 1e9, 'T': 1e12}[m.group(2)] if m else None


def parse_zone(z):
    try:
        lo, hi = [float(re.sub(r'[^0-9.]', '', p)) for p in str(z).split('–')]
        return lo, hi
    except Exception:
        return None


def iso(x):
    return dt.date.fromisoformat(x) if isinstance(x, str) else None


def issue(section, level, msg):
    return {'section': section, 'level': level, 'msg': msg}


# ───────────────────────── per-section validators ─────────────────────────
def check_indices(d, ctx):
    out, ix = [], {i.get('id'): i for i in d.get('indices', [])}
    for need in ('SPX', 'DJI', 'RUT', 'VIX'):
        q = (ix.get(need) or {}).get('q')
        if not q or not num(q.get('price')) or q['price'] <= 0:
            out.append(issue('indices', 'hard', f'{need} quote missing or invalid'))
            continue
        for k in ('ma50', 'ma200', 'yearHigh', 'yearLow'):
            if not num(q.get(k)):
                out.append(issue('indices', 'hard', f'{need}.{k} missing'))
        if num(q.get('change')) and num(q.get('pct')):
            prev = q['price'] - q['change']
            if prev > 0 and abs(q['change'] / prev * 100 - q['pct']) > 0.15:
                out.append(issue('indices', 'hard', f'{need} change and % disagree'))
    if 'VIX' in ix and ix['VIX'].get('q') and not (5 <= ix['VIX']['q'].get('price', 0) <= 90):
        out.append(issue('indices', 'hard', 'VIX outside 5-90'))
    if not (ix.get('QQQ') or {}).get('q'):
        out.append(issue('indices', 'warn', 'QQQ (NDX proxy) missing'))
    return out


def check_macro(d, ctx):
    m, out = d.get('macro'), []
    if not m:
        return [issue('macro', 'hard', 'missing')]
    for k in ('y10', 'y10prev', 'y2', 'y30'):
        if not num(m.get(k)) or not (0 < m[k] < 15):
            out.append(issue('macro', 'hard', f'{k} invalid'))
    h = m.get('y10hist')
    if h is not None and (len(h) != 5 or not all(num(x.get('y10')) and 0 < x['y10'] < 15 and x.get('date') for x in h)):
        out.append(issue('macro', 'warn', 'y10hist malformed (need exactly 5 {date,y10} entries) — regime score will skip the rates check'))
    if not m.get('fed', {}).get('range'):
        out.append(issue('macro', 'hard', 'fed decision text missing'))
    return out


def check_sectors(d, ctx):
    out, secs = [], d.get('sectors') or []
    names = [s.get('name') for s in secs]
    if len(secs) != 11 or len(set(names)) != 11:
        out.append(issue('sectors', 'hard', f'expected 11 distinct sectors, got {len(secs)}'))
    for s in secs:
        if not num(s.get('pct')) or abs(s['pct']) > 60:
            out.append(issue('sectors', 'hard', f"sector {s.get('name')} pct implausible"))
    return out


def check_industries(d, ctx):
    """Structural check only — no cross-check against today's sectors. Industries are pinned (interactive-only,
    Finviz groups/screener), so they can legitimately be dated from an earlier run than the fresh sector ranking;
    `assemble()` separately flags (warn, not hard) when a pinned industry group has drifted out of today's top 3."""
    out, ind = [], d.get('industries') or []
    if len(ind) != 3:
        return [issue('industries', 'hard', 'need industries for exactly 3 sectors')]
    for g in ind:
        items = g.get('items', [])
        if len(items) != 3 or any(not num(i.get('pct')) or abs(i['pct']) > 100 for i in items):
            out.append(issue('industries', 'hard', f"{g.get('sector')}: need 3 industries with valid pct"))
        pcts = [i['pct'] for i in items if num(i.get('pct'))]
        if pcts != sorted(pcts, reverse=True):
            out.append(issue('industries', 'warn', f"{g.get('sector')}: industries not ranked best-first"))
    return out


def _levels(p, ctx):
    """Return list of (level, msg) for one technical pick; hard = drop the item. Needs OHLC (closes), which only
    comes from an interactive WebFetch refresh — technical picks are pinned, never built by the automated routine."""
    out, tk = [], p.get('tk', '?')
    zone = parse_zone(p.get('entryZone'))
    if not zone:
        return [('hard', f'{tk}: entry zone unparseable')]
    for k in ('entry', 'stop', 'target', 'px'):
        if not num(p.get(k)) or p[k] <= 0:
            return [('hard', f'{tk}: {k} missing or invalid')]
    if not (p['stop'] < p['entry'] < p['target']):
        out.append(('hard', f'{tk}: needs stop < entry < target'))
    if not (zone[0] - 1e-6 <= p['entry'] <= zone[1] + 1e-6):
        out.append(('hard', f'{tk}: entry outside its own zone'))
    risk = (p['entry'] - p['stop']) / p['entry'] * 100
    rr = (p['target'] - p['entry']) / max(p['entry'] - p['stop'], 1e-9)
    if not (1.5 <= risk <= 8):
        out.append(('warn', f'{tk}: risk {risk:.1f}% outside the 1.5-8% band'))
    if not (1.4 <= rr <= 4):
        out.append(('warn', f'{tk}: R:R {rr:.1f} unusual'))
    if p['px'] <= 5:
        out.append(('hard', f'{tk}: price <= $5'))
    cap = parse_cap(p.get('cap'))
    if cap is not None and cap < 5e9:
        out.append(('hard', f'{tk}: market cap {p.get("cap")} < $5B'))
    cs = p.get('closes') or []
    if len(cs) < 15 or not all(num(c) for c in cs):
        out.append(('hard', f'{tk}: needs >= 15 closes'))
    elif abs(cs[-1] / p['px'] - 1) > 0.03:
        out.append(('warn', f'{tk}: last close and px differ by >3%'))
    rd = p.get('readiness')
    if rd is not None:
        comps = ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')
        if any(not num(rd.get(k)) or not (0 <= rd[k] <= 100) for k in comps):
            out.append(('warn', f'{tk}: readiness score out of range, ignoring it'))
    return out


def _alert(p, ctx):
    """Catalyst alerts carry no computed trade levels (no OHLC without WebFetch) — just price, pct and cap,
    sourced from FMP's live quote/profile."""
    out, tk = [], p.get('tk', '?')
    for k in ('px', 'pct'):
        if not num(p.get(k)):
            return [('hard', f'{tk}: {k} missing or invalid')]
    if p['px'] <= 5:
        out.append(('hard', f'{tk}: price <= $5'))
    cap = parse_cap(p.get('cap'))
    if cap is None:
        out.append(('hard', f'{tk}: market cap missing'))
    elif cap < 5e9:
        out.append(('hard', f'{tk}: market cap {p.get("cap")} < $5B'))
    return out


def check_technical(d, ctx):
    return _check_items(d, ctx, 'technical', 'picks', _levels)


def check_catalysts(d, ctx):
    return _check_items(d, ctx, 'catalysts', 'catalysts', _alert)


def _check_items(d, ctx, sec, key, level_fn):
    items, out = d.get(key) or [], []
    if not items:
        return [issue(sec, 'hard', 'no items')]
    seen_tech = {p.get('tk') for p in d.get('picks', [])} if sec == 'catalysts' else set()
    window = {x.isoformat() for x in mt.trading_days_back(ctx['lastSession'], CATALYST_WINDOW)}
    for p in items:
        for lvl, msg in level_fn(p, ctx):
            out.append(issue(sec, 'item-hard' if lvl == 'hard' else 'warn', msg) | {'tk': p.get('tk')})
        if sec == 'catalysts':
            if p.get('cdateISO') not in window:
                out.append(issue(sec, 'item-hard', f"{p.get('tk')}: catalyst dated {p.get('cdateISO')} is outside the last {CATALYST_WINDOW} sessions") | {'tk': p.get('tk')})
            srcs = p.get('sources') or []
            if not srcs or not all(str(s.get('u', '')).startswith('https://') for s in srcs):
                out.append(issue(sec, 'item-hard', f"{p.get('tk')}: no https source") | {'tk': p.get('tk')})
            if p.get('tk') in seen_tech:
                out.append(issue(sec, 'item-hard', f"{p.get('tk')}: already a technical pick") | {'tk': p.get('tk')})
    return out


def check_nearmiss(d, ctx):
    items = d.get('nearmiss') or []
    if not items:
        return [issue('nearmiss', 'hard', 'empty')]
    return [issue('nearmiss', 'hard', f"{i.get('tk')}: px invalid") for i in items if not num(i.get('px'))]


CHECKS = {'indices': check_indices, 'macro': check_macro, 'sectors': check_sectors, 'industries': check_industries,
          'technical': check_technical, 'catalysts': check_catalysts, 'nearmiss': check_nearmiss}
KEYS = {'indices': ['indices'], 'macro': ['macro'], 'sectors': ['sectors', 'sectorSource'], 'industries': ['industries'],
        'technical': ['picks', 'pickWindow'], 'catalysts': ['catalysts'], 'nearmiss': ['nearmiss']}


# ───────────────────────── assembly with carry-forward ─────────────────────────
def assemble(new, old, ctx):
    """Merge fresh data with the previous good copy. Returns (final_data, health)."""
    new, old = copy.deepcopy(new or {}), copy.deepcopy(old or {})
    final = {k: v for k, v in (old or {}).items() if k in ('source', 'notices')}
    final.update({k: v for k, v in new.items() if k in ('source', 'notices')})
    final.setdefault('notices', [])
    sections, issues = {}, []
    last = ctx['lastSession']
    for sec in SECTIONS:
        if sec in PINNED and not any(k in new for k in KEYS[sec]):
            # Not fetched by design (no WebFetch in the automated routine) — carry the last interactive copy forward
            # verbatim, dated, and labelled 'pinned' rather than 'stale' so a clean run still reads as healthy.
            prev_ok = old and all(k in old for k in KEYS[sec]) and \
                      not [i for i in CHECKS[sec](old, ctx) if i['level'] == 'hard']
            if prev_ok:
                for k in KEYS[sec]:
                    final[k] = old[k]
                pas = (old.get('sections', {}).get(sec) or {}).get('asOf', '?')
                sections[sec] = {'status': 'pinned', 'asOf': pas,
                                  'source': (old.get('sections', {}).get(sec) or {}).get('source', ''),
                                  'msg': f'Pinned to the last interactive refresh ({pas}). Refresh manually in an interactive session when stale.'}
            else:
                sections[sec] = {'status': 'failed', 'asOf': None, 'source': '',
                                  'msg': 'pinned section has no previous interactive copy to show — run an interactive refresh'}
                for k in KEYS[sec]:
                    final[k] = [] if k in ('picks', 'catalysts', 'nearmiss', 'sectors', 'industries', 'indices') else final.get(k)
                issues.append(issue(sec, 'failed', sections[sec]['msg']))
            continue
        fresh_issues = CHECKS[sec](new, ctx) if any(k in new for k in KEYS[sec]) else [issue(sec, 'hard', 'not delivered by the fetch step')]
        hard = [i for i in fresh_issues if i['level'] == 'hard']
        item_hard = [i for i in fresh_issues if i['level'] == 'item-hard']
        warns = [i for i in fresh_issues if i['level'] == 'warn']
        items_key = {'technical': 'picks', 'catalysts': 'catalysts'}.get(sec)
        if items_key and item_hard and not hard:                       # drop only the bad items
            bad = {i['tk'] for i in item_hard}
            new[items_key] = [p for p in new[items_key] if p.get('tk') not in bad]
            for i in item_hard:
                issues.append(issue(sec, 'warn', 'dropped ' + i['msg']))
            if not new[items_key]:
                hard = [issue(sec, 'hard', 'every item failed validation')]
        issues += [i for i in warns]
        asof = (new.get('sections', {}).get(sec) or {}).get('asOf')
        if not hard and (asof is None or iso(asof) is None or iso(asof) > ctx['today']):
            hard = [issue(sec, 'hard', f'as-of date {asof!r} missing or in the future')]
        if not hard:
            for k in KEYS[sec]:
                if k in new:
                    final[k] = new[k]
            stale = iso(asof) < last
            sections[sec] = {'status': 'stale' if stale else 'ok', 'asOf': asof,
                             'source': (new.get('sections', {}).get(sec) or {}).get('source', ''),
                             'msg': f'delivered data is from {asof}, older than the last session {last}' if stale else ''}
            continue
        reason = '; '.join(i['msg'] for i in hard[:2])
        prev_ok = all(k in old for k in KEYS[sec] if k != 'sectorSource' and k != 'pickWindow') and \
                  not [i for i in CHECKS[sec](old, ctx) if i['level'] == 'hard'] if old else False
        if prev_ok:
            for k in KEYS[sec]:
                if k in old:
                    final[k] = old[k]
            pas = (old.get('sections', {}).get(sec) or {}).get('asOf', '?')
            sections[sec] = {'status': 'stale', 'asOf': pas, 'source': (old.get('sections', {}).get(sec) or {}).get('source', ''),
                             'msg': f'Fresh data failed ({reason}); showing the previous copy from {pas}.'}
            issues.append(issue(sec, 'stale', sections[sec]['msg']))
        else:
            sections[sec] = {'status': 'failed', 'asOf': None, 'source': '', 'msg': f'no fresh data ({reason}) and no previous copy'}
            for k in KEYS[sec]:
                final[k] = [] if k in ('picks', 'catalysts', 'nearmiss', 'sectors', 'industries', 'indices') else final.get(k)
            issues.append(issue(sec, 'failed', sections[sec]['msg']))
    # expire catalysts that aged out of the window (carried forward copies)
    window = {x.isoformat() for x in mt.trading_days_back(last, CATALYST_WINDOW)}
    cats = final.get('catalysts') or []
    keep = [c for c in cats if c.get('cdateISO') in window]
    if len(keep) != len(cats):
        issues.append(issue('catalysts', 'warn', f'{len(cats) - len(keep)} carried-forward catalyst(s) expired (older than {CATALYST_WINDOW} sessions)'))
        final['catalysts'] = keep
        if not keep and sections['catalysts']['status'] != 'failed':
            sections['catalysts'].update(status='failed', msg='previous catalysts all expired and no fresh ones were delivered')
    # flag (warn, not hard) when the pinned industry drill-down no longer matches today's fresh top-3 sectors
    secs_final = final.get('sectors') or []
    top3_today = {s['name'] for s in sorted([s for s in secs_final if num(s.get('pct'))], key=lambda s: -s['pct'])[:3]}
    for g in final.get('industries') or []:
        if top3_today and g.get('sector') not in top3_today:
            issues.append(issue('industries', 'warn', f"{g.get('sector')} industry drill-down is pinned from an older run and is no longer a top-3 sector today — refresh interactively"))
    # company snapshots follow the tickers actually shown
    bg = {**(old.get('bg') or {}), **(new.get('bg') or {})}
    shown = [p['tk'] for p in (final.get('picks') or []) + (final.get('catalysts') or [])]
    final['bg'] = {t: bg[t] for t in shown if t in bg}
    for t in shown:
        if t not in bg:
            issues.append(issue('bg', 'warn', f'no company snapshot for {t}'))
    # derived blocks
    fl = new.get('fetchlog') or []
    failed_calls = [f for f in fl if not f.get('ok')]
    crit_bad = [s for s in CRITICAL if sections[s]['status'] == 'failed']
    # 'pinned' (no WebFetch, carried forward on purpose) does not by itself make a clean run 'degraded';
    # the pinned branch above only adds to `issues` when there was no previous copy to carry forward either.
    status = 'failed' if crit_bad else 'ok' if all(s['status'] in ('ok', 'pinned') for s in sections.values()) and not issues else 'degraded'
    health = {'status': status, 'sections': sections, 'issues': issues,
              'fetch': {'calls': len(fl), 'failed': len(failed_calls), 'failures': [f.get('id') for f in failed_calls][:25]},
              'lastSession': last.isoformat()}
    final['sections'] = {s: {'asOf': v['asOf'], 'source': v['source']} for s, v in sections.items()}
    final['health'] = health
    final['fetchlog'] = fl
    final['watch'] = build_watch(final)
    return final, health


def build_watch(d):
    def row(p):
        r = {'tk': p['tk'], 'px': p['px'], 'pct': p['pct']}
        if p.get('flag'):
            r['flag'] = p['flag']
        return r
    return {'groups': [
        {'name': 'Technical setups', 'items': [row(p) for p in d.get('picks', [])]},
        {'name': 'Catalyst setups', 'items': [row(p) for p in d.get('catalysts', [])]},
        {'name': 'Sector strength — near misses', 'near': True, 'items': d.get('nearmiss', [])}]}


def load_json_from_html(path, element_id='brief-data'):
    """Recover the previous run's data from a published HTML page (the artifact is the state store)."""
    txt = open(path, encoding='utf-8').read()
    m = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % element_id, txt, re.S)
    return json.loads(m.group(1).replace('<\\/', '</')) if m else None


def to_script_json(obj):
    return json.dumps(obj, ensure_ascii=False).replace('</', '<\\/')


def labels(now_utc, last):
    hk = now_utc.astimezone(mt.HKT)
    return {'asOf': last.isoformat(),
            'asOfLabel': last.strftime('%a %d %b %Y') + ' close',
            'builtLabel': hk.strftime('%a %d %b %Y %H:%M') + ' HKT'}
