"""Render brief.html from data + template, with validation and carry-forward.

  python scripts/build_brief.py --data data/brief-data.json [--previous-html out/published-brief.html | --previous data/prev.json]
                                [--now 2026-09-22T12:07:00Z] [--out out/brief.html]
                                [--ledger data/picks.json [--tracker-warn "ledger not pushed"]]

--ledger embeds the pick tracker (the ledger merged with the copy in the previous page, stats, tables, validated sparkline
series) as the `tracker` key and adds a `tracker` health section. Omit it and the page simply has no Track record section.

Exit codes: 0 = built (health ok or degraded, banner shown if not ok) · 3 = nothing publishable (a critical section
has neither fresh nor previous data). In that case NO html is written, so the previously published page stays untouched.
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import market_time as mt, health as hl, tracker as tr, reset_policy as rp, selection as sel, pricedin as pi, flags as fl

root = pathlib.Path(__file__).resolve().parent.parent


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        print(f'note: could not read {path}: {e}', file=sys.stderr)
        return None


def add_tracker(final, health, ledger_path, old, last, extra_warnings):
    """Embed the tracker view and its health entry. Awaiting-data is expected (pinned-style); problems are warnings."""
    ledger, conflicts = tr.load_merged(ledger_path, old)
    basis = tr.parse_pick_window_end(final.get('pickWindow'))
    view = tr.build_view(ledger, last.isoformat(), [f'ledger copy conflict: {c}' for c in conflicts], pick_basis=basis)
    sec, msgs = tr.tracker_health(view, extra_warnings)
    health['sections']['tracker'] = sec
    final['sections']['tracker'] = {'asOf': sec['asOf'], 'source': sec['source']}
    for m in msgs:
        health['issues'].append(hl.issue('tracker', 'warn', m))
    if msgs and health['status'] == 'ok':
        health['status'] = 'degraded'
    # a card's old `closes` array must agree with the validated ledger series; say so when it does not (the page draws the series)
    for pk in final.get('picks') or []:
        ref = view['picks'].get(pk['tk'])
        if ref and ref.get('series') and pk.get('closes') and tr.card_closes_disagree(pk['closes'], ref['series']):
            health['issues'].append(hl.issue('technical', 'warn', f"{pk['tk']}: card closes disagree with the validated OHLC series - the page draws the series"))
    final['tracker'] = view


def accept_target(c, stored_target):
    """(target or None, health note or None) for a routine-written `analystTarget`.

    A target already stored by an interactive scan is never overwritten by the routine (it may only re-price against the new price).
    A new one is accepted only when the alert carries `targetKind: "consensus average"` and `targetSources`: at least two NAMED sources whose
    targets agree within 5% of each other, with `analystTarget` within 5% of their mean. A single broker's target (an upgrade note's price target)
    is not a consensus and is rejected."""
    tg = c.get('analystTarget')
    if tg is None:
        return None, None
    tk = c.get('tk', '?')
    if stored_target is not None:
        return None, f'{tk}: analystTarget {tg} ignored, the stored target {stored_target} from the interactive scan is kept (the routine only re-prices)'
    srcs = [x for x in (c.get('targetSources') or []) if isinstance(x, dict)]
    names = {x.get('t') for x in srcs if x.get('t')}
    vals = [x['target'] for x in srcs if isinstance(x.get('target'), (int, float)) and x['target'] > 0]
    if c.get('targetKind') == 'consensus average' and len(names) >= 2 and len(vals) >= 2 and isinstance(tg, (int, float)) and tg > 0:
        mean = sum(vals) / len(vals)
        if max(vals) / min(vals) - 1 <= 0.05 and abs(tg / mean - 1) <= 0.05:
            return tg, None
    return None, f'{tk}: analystTarget {tg} rejected (needs two named sources agreeing within 5% on the consensus average; a single broker target is not one)'


def load_verified_links(path):
    """`data/verified-links.json`: {"links": [{url, publisher, date, covers, checked: {at, how}}]}. Returns {url: record} (missing file = no verified links)."""
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}
    return {x['url']: x for x in raw.get('links', []) if isinstance(x, dict) and str(x.get('url', '')).startswith('https://') and x.get('publisher')}


def apply_link_policy(final, verified, notes):
    """A catalyst source keeps its link only when the URL is in the verified list (it was loaded and its text covers the event).
    Anything else, in particular a URL the routine wrote itself, is reduced to the publisher name and flagged `verified: false`."""
    for c in final.get('catalysts') or []:
        out = []
        for s in c.get('sources') or []:
            u = s.get('u')
            rec = verified.get(u) if u else None
            if rec:
                out.append({'t': rec['publisher'], 'u': u, 'verified': True})
            else:
                out.append({'t': s.get('t') or 'unnamed source', 'verified': False})
                notes.append(f"{c.get('tk', '?')}: link to {s.get('t') or 'a source'} not verified, shown as a name only")
        c['sources'] = out


def rescore_catalysts(final, last, old=None, notes=None):
    """Re-price every catalyst alert's priced-in score from what is knowable WITHOUT price history, so the cloud routine can keep it current:
    event-fixed inputs (a: move vs ADR, d: run-up) are carried from the interactive run that computed them; b and c are recomputed from the
    alert's current price, the days since its reaction session and an optional fresh average analyst target (`analystTarget`). A new alert with no
    stored inputs is scored from `price`/`analystTarget` only and is labelled partial. The catalyst flag is recomputed from the new score."""
    prior = {(o.get('tk'), o.get('cdateISO')): o for o in (old or {}).get('catalysts') or []}
    for c in final.get('catalysts') or []:
        o = prior.get((c.get('tk'), c.get('cdateISO')))          # the same event listed again by the routine: keep what the interactive run computed
        if o:
            for k in ('pricedIn', 'flags'):
                if k not in c and k in o:
                    c[k] = o[k]
        inp = dict((c.get('pricedIn') or {}).get('inputs') or {})
        if c.get('px') is not None:
            inp['price'] = c['px']
            if inp.get('eventHigh') is not None:
                inp['last'] = c['px']
        tgt, note = accept_target(c, inp.get('target'))
        if note and notes is not None:
            notes.append(note)
        if tgt is not None:
            inp['target'] = tgt
            inp['targetSource'] = 'consensus average, ' + ' + '.join(sorted({x.get('t') for x in c.get('targetSources') or [] if x.get('t')}))
        if inp.get('reactionDate'):
            n, d = 0, mt.dt.date.fromisoformat(inp['reactionDate'])
            while d <= last:
                n += 1 if mt.is_trading_day(d) else 0
                d += mt.dt.timedelta(days=1)
            inp['sessionsSince'] = n
        c['pricedIn'] = pi.score(inp)
        keep = [f for f in (c.get('flags') or []) if f.get('key') != 'catalyst']
        f, _ = fl.catalyst_flag(c, c['pricedIn'], last.isoformat())
        c['flags'] = keep + ([f] if f else [])
    final['catalysts'] = pi.rank_alerts(final.get('catalysts') or [])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(root / 'data/brief-data.json'))
    ap.add_argument('--previous')
    ap.add_argument('--previous-html')
    ap.add_argument('--template', default=str(root / 'templates/brief.template.html'))
    ap.add_argument('--out', default=str(root / 'out/brief.html'))
    ap.add_argument('--save-data', default=str(root / 'out/last-good/brief-data.json'))
    ap.add_argument('--now')
    ap.add_argument('--ledger')
    ap.add_argument('--verified-links', default=str(root / 'data/verified-links.json'))
    ap.add_argument('--run-kind', choices=['automated', 'interactive'], default='interactive', help='who is building this page (the cloud routine passes automated)')
    ap.add_argument('--last-automated', help='seed/override the last automated run as "ISO-time,status" (only for the first build that carries the field)')
    ap.add_argument('--tracker-warn', action='append', default=[], help='extra tracker health warning (repeatable)')
    a = ap.parse_args(argv)

    now = mt.now_utc() if not a.now else mt.dt.datetime.fromisoformat(a.now.replace('Z', '+00:00'))
    ctx = {'lastSession': mt.last_completed_session(now), 'today': now.astimezone(mt.HKT).date()}
    new = load(a.data)
    old = load(a.previous) if a.previous else None
    if old is None and a.previous_html:
        try:
            old = hl.load_json_from_html(a.previous_html)
        except Exception as e:
            print(f'note: previous html unreadable: {e}', file=sys.stderr)

    final, health = hl.assemble(new, old, ctx)
    link_notes = []
    apply_link_policy(final, load_verified_links(a.verified_links), link_notes)
    for m in link_notes:                                  # informational: the card itself says "link not verified"; it is not a data fault, so the page is not marked degraded for it
        health['issues'].append(hl.issue('catalysts', 'info', m))
    target_notes = []
    rescore_catalysts(final, ctx['lastSession'], old, target_notes)
    for m in target_notes:
        health['issues'].append(hl.issue('catalysts', 'warn', m))
    if target_notes and health['status'] == 'ok':
        health['status'] = 'degraded'
    if a.ledger:
        add_tracker(final, health, a.ledger, old, ctx['lastSession'], a.tracker_warn)
    # Label the page by the date of the MARKET DATA it actually carries (the indices section), not by the last completed session:
    # they are equal on a normal run, but a page republished with older data must not read "Data as of <a newer close>".
    data_day = hl.iso((final.get('sections', {}).get('indices') or {}).get('asOf'))
    final.update(hl.labels(now, data_day if data_day and data_day <= ctx['lastSession'] else ctx['lastSession']))
    # silent non-runs must be visible: the page shows when the cloud routine last ran and how that run ended
    gen = now.astimezone(mt.HKT).isoformat()
    last_auto = ((old or {}).get('runs') or {}).get('lastAutomated')
    if a.last_automated:
        at, _, st = a.last_automated.partition(',')
        last_auto = {'at': at, 'status': st or 'ok', 'seeded': True}
    if a.run_kind == 'automated':
        last_auto = {'at': gen, 'status': 'unpushed' if any('not pushed' in w for w in a.tracker_warn) else 'ok'}
    final['runs'] = {'lastAutomated': last_auto, 'thisBuild': {'kind': a.run_kind, 'at': gen},
                     'schedule': {'brief': {'fireUtc': '12:00', 'graceMinutes': 30}}}
    final['calendar'] = {'closed': sorted(d.isoformat() for d in mt.NYSE_CLOSED)}
    final['resetPolicy'] = {'clear': [c['what'] for c in rp.CLEAR], 'keep': [k['what'] for k in rp.KEEP]}
    final['selectionConfig'] = sel.CONFIG
    final['meta'] = {**(new or {}).get('meta', {}), 'generatedAt': now.astimezone(mt.HKT).isoformat(), 'lastSession': ctx['lastSession'].isoformat()}

    report = {'status': health['status'], 'sections': {k: v['status'] for k, v in health['sections'].items()},
              'issues': health['issues'], 'fetch': health['fetch']}
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / 'build-report.json').write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding='utf-8')
    if health['status'] == 'failed':
        print('FAILED: nothing publishable ->', [k for k, v in health['sections'].items() if v['status'] == 'failed'], file=sys.stderr)
        return 3
    tpl = pathlib.Path(a.template).read_text(encoding='utf-8')
    out.write_text(tpl.replace('__BRIEF_JSON__', hl.to_script_json(final)), encoding='utf-8')
    sd = pathlib.Path(a.save_data)              # degraded data is still the newest good state
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'brief built: {health["status"]} · sections {report["sections"]} · {len(health["issues"])} issue(s) -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
