"""Validation + degradation rules for the market-open page (open-live.json).

Live prices are only meaningful for the session they belong to, so unlike the brief there is NO carry-forward across
days: a quote from Friday is not a quote for Monday. Within the same ET date a section may be reused if a re-fetch fails.
"""
import copy, datetime as dt
import market_time as mt
from health import num, issue, iso

STALE_MIN = 45            # a snapshot older than this is flagged
SECTIONS = ['indices', 'futures', 'quotes', 'news']
LABEL = {'indices': 'Index & VIX quotes', 'futures': 'Index futures', 'quotes': 'Stock quotes', 'news': 'Overnight news', 'levels': 'Trade levels (from the brief)'}


def ts(x):
    try:
        return dt.datetime.fromisoformat(str(x).replace('Z', '+00:00'))
    except Exception:
        return None


def check_indices(L, fri, ctx):
    out, seen = [], {i.get('id'): i for i in L.get('indices', [])}
    for need in ('SPX', 'DJI', 'RUT', 'VIX'):
        i = seen.get(need)
        if not i or not num(i.get('px')) or i['px'] <= 0 or not num(i.get('pct')) or not num(i.get('prev')):
            out.append(issue('indices', 'hard', f'{need} missing or invalid'))
        elif abs((i['px'] / i['prev'] - 1) * 100 - i['pct']) > 0.2:
            out.append(issue('indices', 'hard', f'{need} price, prior close and % disagree'))
        elif abs(i['pct']) > (40 if need == 'VIX' else 10):
            out.append(issue('indices', 'hard', f'{need} move of {i["pct"]}% is implausible'))
    if 'QQQ' not in seen:
        out.append(issue('indices', 'warn', 'QQQ missing'))
    return out


def check_futures(L, fri, ctx):
    f, out = L.get('futures') or {}, []
    rows = {r.get('id'): r for r in f.get('rows', [])}
    for need in ('ES', 'NQ', 'YM', 'RTY'):
        r = rows.get(need)
        if not r or not num(r.get('px')) or not num(r.get('pct')) or abs(r['pct']) > 8:
            out.append(issue('futures', 'hard', f'{need} missing or implausible'))
        elif num(r.get('chk')) and abs(r['chk'] - r['pct']) > 0.25:
            out.append(issue('futures', 'warn', f'{need}: the two futures sources disagree ({r["pct"]}% vs {r["chk"]}%)'))
        elif not num(r.get('chk')):
            out.append(issue('futures', 'warn', f'{need}: single source, not cross-checked'))
    return out


def check_quote(tk, q, friday_px):
    if not isinstance(q, dict) or not num(q.get('px')) or q['px'] <= 0:
        return 'no valid price'
    if not num(q.get('pct')) or abs(q['pct']) > 40:
        return f'move {q.get("pct")}% implausible'
    if friday_px and abs((q['px'] / friday_px - 1) * 100 - q['pct']) > 0.6:
        return f'price {q["px"]} disagrees with its own % vs the brief close {friday_px}'
    if q.get('open') is not None and (not num(q['open']) or q['open'] <= 0 or (friday_px and abs(q['open'] / friday_px - 1) > 0.4)):
        return 'open print implausible'
    if not num(q.get('vol')) or q['vol'] < 0 or not num(q.get('avgVol')) or q['avgVol'] <= 0:
        return 'volume fields invalid'
    return None


def assemble(new, old, brief, ctx):
    """Return (live, health). `brief` is the FINAL brief data (already validated). ctx has now, etDate, openUTC."""
    new, old = copy.deepcopy(new or {}), copy.deepcopy(old or {})
    fri = {}
    for g in (brief.get('watch') or {}).get('groups', []):
        for it in g.get('items', []):
            fri[it['tk']] = it.get('px')
    live = {k: new.get(k) for k in ('note', 'notes', 'footnotes', 'links') if k in new}
    live.setdefault('notes', {})
    sections, issues = {}, []
    now = ctx['now']

    def usable_old(sec):
        s = (old.get('sections') or {}).get(sec) or {}
        t = ts(s.get('asOfUTC'))
        return t is not None and mt.to_et(t).date() == ctx['etDate'] and t <= now and sec in old

    for sec in SECTIONS:
        meta = (new.get('sections') or {}).get(sec) or {}
        t = ts(meta.get('asOfUTC'))
        if sec not in new or not new.get(sec):
            errs = [issue(sec, 'hard', 'not delivered by the fetch step')]
        elif t is None or t > now + dt.timedelta(minutes=2):
            errs = [issue(sec, 'hard', f"as-of time {meta.get('asOfUTC')!r} missing or in the future")]
        elif mt.to_et(t).date() != ctx['etDate'] and sec in ('indices', 'futures', 'quotes'):
            errs = [issue(sec, 'hard', f'snapshot is from {mt.to_et(t).date()}, not today')]
        else:
            errs = {'indices': check_indices, 'futures': check_futures}.get(sec, lambda *_: [])(new, fri, ctx)
        if sec == 'quotes' and not errs:
            bad = {tk: check_quote(tk, q, fri.get(tk)) for tk, q in new['quotes'].items()}
            for tk, why in bad.items():
                if why:
                    issues.append(issue('quotes', 'warn', f'{tk}: {why}; no live status shown'))
                    del new['quotes'][tk]
            if not new['quotes']:
                errs = [issue('quotes', 'hard', 'every quote failed validation')]
        if sec == 'news' and not errs:
            keep = [n for n in new['news'] if n.get('sources') and all(str(s.get('u', '')).startswith('https://') for s in n['sources'])]
            if len(keep) != len(new['news']):
                issues.append(issue('news', 'warn', f'{len(new["news"]) - len(keep)} news item(s) dropped for lacking an https source'))
            new['news'] = keep
            if not keep:
                errs = [issue('news', 'hard', 'no sourced news items')]
        hard = [e for e in errs if e['level'] == 'hard']
        issues += [e for e in errs if e['level'] == 'warn']
        if not hard:
            live[sec] = new[sec]
            age = (now - t).total_seconds() / 60
            status = 'stale' if age > STALE_MIN else 'ok'
            sections[sec] = {'status': status, 'asOfUTC': meta['asOfUTC'], 'source': meta.get('source', ''),
                             'msg': f'snapshot is {age:.0f} min old' if status == 'stale' else ''}
            if status == 'stale':
                issues.append(issue(sec, 'stale', sections[sec]['msg']))
            if sec == 'futures' and 'contract' in new.get('futures', {}):
                pass
            continue
        why = '; '.join(e['msg'] for e in hard[:2])
        if usable_old(sec):
            live[sec] = old[sec]
            a = old['sections'][sec]['asOfUTC']
            age = (now - ts(a)).total_seconds() / 60
            sections[sec] = {'status': 'stale', 'asOfUTC': a, 'source': old['sections'][sec].get('source', ''),
                             'msg': f'Re-fetch failed ({why}); showing this morning\'s earlier snapshot, {age:.0f} min old'}
            issues.append(issue(sec, 'stale', sections[sec]['msg']))
        else:
            live[sec] = {} if sec in ('quotes', 'futures') else []
            sections[sec] = {'status': 'failed', 'asOfUTC': None, 'source': '', 'msg': f'unavailable ({why})'}
            issues.append(issue(sec, 'failed', sections[sec]['msg']))

    # the trade levels come from the brief; say how old they are
    bh, bmeta = brief.get('health') or {}, brief.get('meta') or {}
    blast = iso(bmeta.get('lastSession'))
    lvl = {'status': 'ok', 'asOf': str(blast), 'msg': ''}
    if blast is None or blast < ctx['lastSession']:
        lvl.update(status='stale', msg=f'levels come from the brief for the {blast} close, not the latest session {ctx["lastSession"]}')
    stale_secs = [k for k, v in (bh.get('sections') or {}).items() if k in ('technical', 'catalysts') and v['status'] != 'ok']
    if stale_secs:
        lvl.update(status='stale', msg=(lvl['msg'] + ' ' if lvl['msg'] else '') + 'the brief itself flagged ' + ', '.join(stale_secs) + ' as not fresh')
    sections['levels'] = lvl
    if lvl['status'] != 'ok':
        issues.append(issue('levels', 'stale', lvl['msg']))

    phase = 'open' if now >= ctx['openUTC'] else 'pre-open'
    failed = [k for k, v in sections.items() if v['status'] == 'failed']
    status = 'failed' if ('quotes' in failed and 'indices' in failed) else 'ok' if not issues else 'degraded'
    health = {'status': status, 'phase': phase, 'sections': sections, 'issues': issues}
    live['sections'] = {k: {'asOfUTC': v.get('asOfUTC'), 'source': v.get('source', '')} for k, v in sections.items() if k in SECTIONS}
    live['meta'] = {'snapshotUTC': max([v['asOfUTC'] for v in sections.values() if v.get('asOfUTC')] or [None]) if any(v.get('asOfUTC') for v in sections.values()) else None,
                    'builtUTC': now.isoformat()}
    live['health'] = health
    return live, health
