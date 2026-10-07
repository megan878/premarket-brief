"""Failure-scenario tests for both builders. Run:  python tests/test_pipeline.py
Each scenario writes its page to out/scenarios/ so it can also be opened and eyeballed."""
import copy, io, json, os, pathlib, sys, contextlib

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / 'scripts'))
import build_brief, build_open, health as hl

OUT = root / 'out' / 'scenarios'
OUT.mkdir(parents=True, exist_ok=True)
BRIEF_NOW = '2026-09-21T12:00:00Z'          # Mon 20:00 HKT -> last session = Fri 18 Sep
OPEN_NOW = '2026-09-21T13:52:00Z'           # 09:52 ET, 22 min after the open
fresh = json.loads((root / 'data/brief-data.example.json').read_text(encoding='utf-8'))
live0 = json.loads((root / 'data/open-live.example.json').read_text(encoding='utf-8'))
results = []


def jw(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return str(p)


def run(mod, argv):
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = mod.main(argv)
    return code, err.getvalue()


def page_json(path, element='brief-data'):
    return hl.load_json_from_html(path, element)


def all_links(*datas):
    """Every catalyst source URL in the fixtures, as a verified-links file (so the link policy stays out of scenarios that are about something else)."""
    links = {}
    for d in datas:
        for c in (d or {}).get('catalysts') or []:
            for x in c.get('sources') or []:
                if x.get('u'):
                    links[x['u']] = {'url': x['u'], 'publisher': x.get('t') or 'src', 'date': '2026-09-18', 'covers': c.get('tk'), 'checked': {'at': '2026-10-06', 'how': 'fixture'}}
    return {'links': list(links.values())}


def brief(name, new, prev=None, now=BRIEF_NOW, expect=0, verified='auto'):
    out = OUT / f'{name}.html'
    if out.exists():
        out.unlink()
    argv = ['--data', jw(f'{name}.data.json', new) if new is not None else str(OUT / 'missing.json'),
            '--out', str(out), '--save-data', str(OUT / f'{name}.saved.json'), '--now', now]
    if prev is not None:
        argv += ['--previous', jw(f'{name}.prev.json', prev)]
    argv += ['--verified-links', jw(f'{name}.links.json', all_links(new, prev) if verified == 'auto' else verified)]
    code, err = run(build_brief, argv)
    assert code == expect, f'{name}: exit {code}, wanted {expect}\n{err}'
    return (page_json(str(out)) if code == 0 else None), code


def check(name, cond, msg=''):
    assert cond, f'{name}: {msg}'
    results.append(name)


# a valid "previous" state = a healthy build of the same data
prev, _ = brief('00_previous', fresh)
prev = json.loads((OUT / '00_previous.saved.json').read_text(encoding='utf-8'))

# B1 happy path
d, _ = brief('B1_happy', fresh, prev)
check('B1 happy path is clean', d['health']['status'] == 'ok' and not d['health']['issues'], d['health'])

# B2 sectors and industries are PINNED with the scan (since 5 Oct the routine never writes them): their absence is normal,
# the interactive copy is carried forward verbatim and labelled 'pinned', not 'stale'.
n = copy.deepcopy(fresh); [n.pop(k) for k in ('sectors', 'industries', 'sectorSource')]
d, _ = brief('B2_sector_source_down', n, prev)
h = d['health']['sections']['sectors']
check('B2 sector ranking is carried forward, pinned with the scan (same chip as the industries)', h['status'] == 'pinned' and len(d['sectors']) == 11 and h['asOf'] == prev['health']['sections']['sectors']['asOf'], h)
n2 = copy.deepcopy(fresh); n2['sectors'] = n2['sectors'][:9]
d2, _ = brief('B2b_sectors_delivered_but_bad', n2, prev)
check('B2b a sector table that IS delivered but invalid still falls back to the previous copy as stale', d2['health']['sections']['sectors']['status'] == 'stale' and len(d2['sectors']) == 11)
check('B2 industries stay pinned, not marked stale', d['health']['sections']['industries']['status'] == 'pinned' and len(d['industries']) == 3)

# B3 index quote garbage (negative price, VIX 400)
n = copy.deepcopy(fresh); n['indices'][0]['q']['price'] = -7650.5; n['indices'][4]['q']['price'] = 400
d, _ = brief('B3_garbage_indices', n, prev)
check('B3 garbage index quotes are rejected', d['health']['sections']['indices']['status'] == 'stale' and d['indices'][0]['q']['price'] == 7650.5)

# B4 the whole fetch step produced nothing. Automated sections fall back to stale; pinned sections (never
# fetched by the automated routine in the first place) stay 'pinned', not 'stale'.
d, _ = brief('B4_total_fetch_failure', None, prev)
check('B4 total failure republishes fetched sections as stale',
      all(d['health']['sections'][s]['status'] == 'stale' for s in ('indices', 'macro', 'catalysts'))
      and d['health']['status'] == 'degraded')
check('B4 pinned sections stay pinned even on total fetch failure',
      all(d['health']['sections'][s]['status'] == 'pinned' for s in ('sectors', 'industries', 'technical', 'nearmiss')))

# B5 total failure with no previous copy -> nothing publishable, exit 3, no page written
_, code = brief('B5_nothing_to_show', None, None, expect=3)
check('B5 no data and no history -> refuses to publish', code == 3 and not (OUT / 'B5_nothing_to_show.html').exists())

# B13 y10hist malformed (wrong length) -> macro section still OK, just warns; rates check has nothing to show
n = copy.deepcopy(fresh); n['macro']['y10hist'] = n['macro']['y10hist'][:3]
d, _ = brief('B13_bad_y10hist', n, prev)
check('B13 malformed y10hist warns but macro stays ok', d['health']['sections']['macro']['status'] == 'ok' and any('y10hist' in i['msg'] for i in d['health']['issues']))

# B6 one pick has stop above entry; the rest survive
n = copy.deepcopy(fresh); n['picks'][1]['stop'] = n['picks'][1]['entry'] + 5
d, _ = brief('B6_one_bad_pick', n, prev)
check('B6 only the bad pick is dropped', [p['tk'] for p in d['picks']] == ['ANET', 'CVX', 'SNX'] and any('dropped' in i['msg'] for i in d['health']['issues']), [p['tk'] for p in d['picks']])

# B12 a pick's readiness score is out of range -> warned, not dropped (readiness is informational, not a gate)
n = copy.deepcopy(fresh); n['picks'][0]['readiness'] = {'composite': 150, 'tightness': 25, 'proximity': 25, 'volumeDryUp': 25, 'timeInBase': 25}
d, _ = brief('B12_bad_readiness', n, prev)
check('B12 out-of-range readiness warns but keeps the pick', 'ANET' in [p['tk'] for p in d['picks']] and any('readiness' in i['msg'] for i in d['health']['issues']))

# B7 catalysts all older than 5 sessions, and previous copy too
n = copy.deepcopy(fresh)
for c in n['catalysts']: c['cdateISO'] = '2026-09-01'
p2 = copy.deepcopy(prev)
for c in p2['catalysts']: c['cdateISO'] = '2026-09-01'
d, _ = brief('B7_catalysts_expired', n, p2)
check('B7 expired catalysts never shown as current', d['catalysts'] == [] and d['health']['sections']['catalysts']['status'] == 'failed')

# B8 as-of date in the future (the FRED-style garbling seen during the build)
n = copy.deepcopy(fresh); n['sections']['macro']['asOf'] = '2026-09-23'
d, _ = brief('B8_future_date', n, prev)
check('B8 future-dated data is rejected', d['health']['sections']['macro']['status'] == 'stale')

# B9 delivered data is older than the last session (fetch returned a cached page)
d, _ = brief('B9_old_data', fresh, prev, now='2026-09-22T22:00:00Z')      # Tue after the bell -> last session Tue 22 Sep
check('B9 old data is flagged stale, not shown as fresh', d['health']['sections']['indices']['status'] == 'stale')

# B10 a catalyst without an https source is dropped, no unsourced claims
n = copy.deepcopy(fresh); n['catalysts'][0]['sources'] = [{'t': 'x', 'u': 'http://insecure'}]
d, _ = brief('B10_unsourced', n, prev)
check('B10 unsourced catalyst dropped', 'GNRC' not in [c['tk'] for c in d['catalysts']])

# B11 a small-cap sneaks in
n = copy.deepcopy(fresh); n['picks'][0]['cap'] = '$4.2B'
d, _ = brief('B11_small_cap', n, prev)
check('B11 market cap under $5B is dropped', 'ANET' not in [p['tk'] for p in d['picks']])

# ── open page ────────────────────────────────────────────────────────────
# Build brief_final ourselves (never read the ambient out/last-good/brief-data.json default — a real pipeline run
# in this same working directory overwrites that path, which once silently poisoned this fixture).
brief_final_path = OUT / 'brief_final.saved.json'
run(build_brief, ['--data', jw('brief_final.data.json', fresh), '--previous', jw('brief_final.prev.json', prev),
                   '--out', str(OUT / 'brief_final.html'), '--save-data', str(brief_final_path), '--now', BRIEF_NOW,
                   '--verified-links', jw('brief_final.links.json', all_links(fresh, prev))])
brief_final = json.loads(brief_final_path.read_text(encoding='utf-8'))
brief_final.pop('fetchlog', None)


def opn(name, live, prev_live=None, now=OPEN_NOW, expect=0, brief_data=None):
    out = OUT / f'{name}.html'
    if out.exists():
        out.unlink()
    argv = ['--brief-data', jw(f'{name}.brief.json', brief_data or brief_final),
            '--live', jw(f'{name}.live.json', live) if live is not None else str(OUT / 'missing.json'),
            '--out', str(out), '--save-live', str(OUT / f'{name}.saved.json'), '--now', now]
    if prev_live is not None:
        argv += ['--previous-live', jw(f'{name}.prev.json', prev_live)]
    code, err = run(build_open, argv)
    assert code == expect, f'{name}: exit {code}, wanted {expect}\n{err}'
    return (page_json(str(out), 'live-data') if code == 0 else None), code


d, _ = opn('L1_happy', live0)
check('L1 happy path is clean', d['health']['status'] == 'ok', d['health']['issues'])

# L2 one quote is garbage (price 10x off) and another is missing entirely
n = copy.deepcopy(live0); n['quotes']['MTSI']['px'] = 2817.4; del n['quotes']['ETSY']
d, _ = opn('L2_bad_and_missing_quote', n)
check('L2 bad/missing quotes are dropped, others kept', 'MTSI' not in d['quotes'] and 'ETSY' not in d['quotes'] and 'GNRC' in d['quotes'] and d['health']['status'] == 'degraded')

# L3 futures sources unreachable
n = copy.deepcopy(live0); del n['futures']; del n['sections']['futures']
d, _ = opn('L3_futures_down', n)
check('L3 futures failure is flagged, page still builds', d['health']['sections']['futures']['status'] == 'failed' and d['health']['status'] == 'degraded')

# L4 futures re-fetch failed but earlier this morning's snapshot exists
n = copy.deepcopy(live0); del n['futures']; del n['sections']['futures']
d, _ = opn('L4_futures_reuse_earlier', n, prev_live=live0)
check('L4 same-day earlier futures reused and flagged stale', d['health']['sections']['futures']['status'] == 'stale' and d['futures']['rows'])

# L5 nothing at all was fetched
_, code = opn('L5_everything_down', None, expect=3)
check('L5 no quotes and no indices -> not published', code == 3 and not (OUT / 'L5_everything_down.html').exists())

# L6 pre-open run: no opening prints yet
n = copy.deepcopy(live0)
for q in n['quotes'].values(): q.pop('open', None)
for k in n['sections']: n['sections'][k]['asOfUTC'] = '2026-09-21T13:24:00Z'
d, _ = opn('L6_pre_open', n, now='2026-09-21T13:25:00Z')
check('L6 pre-open page builds with no open prints', d['health']['phase'] == 'pre-open')

# L7 snapshot is 70 minutes old
d, _ = opn('L7_old_snapshot', live0, now='2026-09-21T14:58:00Z')
check('L7 an old snapshot is flagged stale', d['health']['sections']['quotes']['status'] == 'stale')

# L8 the quotes are from yesterday: meaningless for today, so refused
n = copy.deepcopy(live0)
for k in ('indices', 'futures', 'quotes'): n['sections'][k]['asOfUTC'] = '2026-09-18T19:55:00Z'
_, code = opn('L8_yesterdays_quotes', n, expect=3)
check('L8 yesterday\'s quotes are not presented as today\'s', code == 3)

# L9 the brief the levels come from is older than the last session
# L9 technical picks are pinned (interactive-only, hybrid design) and dated before today's session -> levels shows
# PINNED, the expected everyday state, not an error
b = copy.deepcopy(brief_final)
b['health']['sections']['technical'] = dict(b['health']['sections']['technical'], asOf='2026-09-10')
d, _ = opn('L9_pinned_levels', live0, brief_data=b)
check('L9 pinned technical levels show PINNED, not a failure', d['health']['sections']['levels']['status'] == 'pinned' and d['health']['status'] != 'failed')

# L10 the brief's technical section failed outright (no usable picks at all) -> levels FAILED, page still builds
b = copy.deepcopy(brief_final)
b['health']['sections']['technical'] = {'status': 'failed', 'asOf': None, 'msg': 'every pick failed validation'}
d, _ = opn('L10_technical_failed', live0, brief_data=b)
check('L10 technical failed in the brief -> levels FAILED, page still builds', d['health']['sections']['levels']['status'] == 'failed' and d['health']['status'] == 'degraded')

# L11 news without an https source
n = copy.deepcopy(live0); n['news'][0]['sources'] = [{'t': 'x', 'u': 'ftp://nope'}]
d, _ = opn('L11_unsourced_news', n)
check('L11 unsourced news dropped', len(d['news']) == len(live0['news']) - 1)

# L12 no brief at all -> no levels, cannot build
out = OUT / 'L12.html'
code, _ = run(build_open, ['--brief-data', str(OUT / 'nope.json'), '--live', str(root / 'data/open-live.example.json'), '--out', str(out), '--now', OPEN_NOW])
check('L12 without brief levels the open page refuses to build', code == 4)

# L14 a PINNED technical pick's brief price is weeks old; a valid live quote with a normal daily move must not be
# rejected just because it has drifted a lot from that stale reference (the bug check_quote used to have)
n = copy.deepcopy(live0)
n['quotes']['ANET'] = {'px': 203.59, 'pct': 0.36, 'change': 0.73, 'vol': 4875937, 'avgVol': 7278370}
d, _ = opn('L14_pinned_reference_drift', n)
check('L14 a normal live quote is kept even when it has drifted far from a pinned brief price', 'ANET' in d['quotes'])

# L13 catalyst alerts (hybrid shape: no entryZone/entry/stop/target) must not crash the open-page build
b = copy.deepcopy(brief_final)
b['catalysts'] = [{'tk': 'TEST', 'name': 'Test Co', 'industry': 'Software - Application', 'sector': 'Technology',
                    'ctype': 'Earnings beat', 'what': 'Beat on EPS.', 'px': 50.0, 'pct': 2.0, 'cap': '$10B', 'topSector': True}]
n = copy.deepcopy(live0); n['quotes']['TEST'] = {'px': 51.0, 'pct': 2.1, 'vol': 1000000, 'avgVol': 800000}
d, _ = opn('L13_catalyst_alert_no_levels', n, brief_data=b)
check('L13 catalyst alerts without computed levels build without crashing', any(c['tk'] == 'TEST' for c in page_json(str(OUT / 'L13_catalyst_alert_no_levels.html'), 'brief-data')['catalysts']))


# ───────────── pick tracker integration (build_brief --ledger) ─────────────
import tracker as tr

def ledger_with(*recs):
    led = tr.empty_ledger()
    tr.ingest(led, fresh, '2026-09-21T09:00:00+08:00')          # the example picks: ANET MTCH CVX SNX, basis 18 Sep
    for r in led['records']:
        r['status'] = 'pending'
    led['records'].extend(recs)
    return led


def brief_t(name, ledger, new=None, prev=None, extra=(), now=BRIEF_NOW):
    out = OUT / f'{name}.html'
    if out.exists():
        out.unlink()
    lp = OUT / f'{name}.ledger.json'
    tr.save_ledger(lp, ledger)
    argv = ['--data', jw(f'{name}.data.json', new if new is not None else fresh), '--out', str(out),
            '--save-data', str(OUT / f'{name}.saved.json'), '--now', now, '--ledger', str(lp), *extra]
    if prev is not None:
        argv += ['--previous', jw(f'{name}.prev.json', prev)]
    argv += ['--verified-links', jw(f'{name}.links.json', all_links(new if new is not None else fresh, prev))]
    code, err = run(build_brief, argv)
    assert code == 0, f'{name}: exit {code}\n{err}'
    return page_json(str(out))


# T1 --ledger embeds the tracker, adds a clean health section, and leaves a healthy build healthy
d = brief_t('T1_tracker_embedded', ledger_with(), prev=prev)
check('T1 the ledger is embedded as the tracker block', len(d['tracker']['records']) == 4 and set(d['tracker']['stats']) == {'literal', 'disciplined', 'rule', 'invalidLevels'})
check('T1 a waiting-only ledger leaves the build healthy', d['health']['sections']['tracker']['status'] == 'ok' and d['health']['status'] == 'ok', d['health'])
check('T1 every pick on the page has a tracker chip', {p['tk'] for p in d['picks']} == set(d['tracker']['picks']) and
      all(v['chip'] == 'waiting' for v in d['tracker']['picks'].values()), d['tracker']['picks'])

# T2 records that cannot be evaluated (the cloud routine has no OHLC) are 'awaiting data': pinned-style, never amber
led = ledger_with()
for r in led['records']:
    r.update(status='triggered', startSession='2026-09-17', evaluatedThrough='2026-09-17', fillPrice=r['entry'], lastClose=r['entry'])
d = brief_t('T2_tracker_awaiting', led, prev=prev)
check('T2 awaiting-data is expected, not a warning', d['health']['sections']['tracker']['status'] == 'pinned' and
      d['health']['status'] == 'ok' and len(d['tracker']['awaitingData']) == 4, d['health'])
check('T2 the page says the records are awaiting data', all(v['chip'] == 'awaiting' for v in d['tracker']['picks'].values()))

# T3 a conflicting terminal outcome between the git ledger and the page copy is a health warning, never a silent overwrite
a = ledger_with()
a['records'][0].update(status='stopped', exitDate='2026-09-22', exitPrice=196.89, fillPrice=206.2)
pg = copy.deepcopy(prev)
cp = copy.deepcopy(a['records'][0]); cp.update(status='target', exitPrice=226.82)
pg['tracker'] = {'records': [cp]}
d = brief_t('T3_tracker_conflict', a, prev=pg)
check('T3 conflicting ledger copies warn and degrade the page', d['health']['status'] == 'degraded' and
      any(i['section'] == 'tracker' and 'conflict' in i['msg'] for i in d['health']['issues']), d['health']['issues'])
check('T3 the git ledger copy is the one kept', [r for r in d['tracker']['records'] if r['id'] == a['records'][0]['id']][0]['status'] == 'stopped')

# T4 --tracker-warn ("ledger not pushed") reaches the page's health block
d = brief_t('T4_tracker_warn', ledger_with(), prev=prev, extra=['--tracker-warn', 'ledger not pushed'])
check('T4 an extra tracker warning is shown in health', d['health']['status'] == 'degraded' and
      any(i['section'] == 'tracker' and i['msg'] == 'ledger not pushed' for i in d['health']['issues']))

# T5 without --ledger there is no tracker at all (and no health row for it)
d, _ = brief('T5_no_ledger', fresh, prev)
check('T5 no --ledger means no tracker block', 'tracker' not in d and 'tracker' not in d['health']['sections'])

# T6 a card whose old `closes` disagree with the validated ledger series is flagged
led = ledger_with()
base_closes = [100.0 + i for i in range(25)]
for r in led['records']:
    if r['ticker'] == 'ANET':
        r['series'] = {'from': '2026-08-14', 'through': '2026-09-18', 'closes': base_closes}
n = copy.deepcopy(fresh)
for p in n['picks']:
    if p['tk'] == 'ANET':
        p['closes'] = base_closes[:5][::-1] + base_closes[5:]          # the first five points out of order
d = brief_t('T6_card_closes_disagree', led, new=n, prev=prev)
check('T6 a misordered card is flagged against the validated series', any('card closes disagree' in i['msg'] and 'ANET' in i['msg'] for i in d['health']['issues']), d['health']['issues'])
n2 = copy.deepcopy(fresh)
for p in n2['picks']:
    if p['tk'] == 'ANET':
        p['closes'] = list(base_closes)
d = brief_t('T6b_card_closes_agree', led, new=n2, prev=prev)
check('T6 an agreeing card raises no warning', not any('card closes disagree' in i['msg'] for i in d['health']['issues']))


# ───────────── stop-floor defects caught at build time (no scan involved) ─────────────
def with_pick(tk, **fields):
    n = copy.deepcopy(fresh)
    for p in n['picks']:
        if p['tk'] == tk:
            p.update(fields)
    return n


# T7 a pick whose stop is at/above its last close is flagged "stop above last close: TICKER" before anyone publishes it
last_anet = fresh['picks'][0]['closes'][-1]
n = with_pick('ANET', stop=round(last_anet + 0.5, 2))
d, _ = brief('T7_stop_above_last_close', n, prev)
check('T7 a stop above the last close warns by ticker', any(i['msg'] == 'stop above last close: ANET' and i['level'] == 'warn' for i in d['health']['issues'])
      and d['health']['status'] == 'degraded', d['health']['issues'])
check('T7 the pick is still shown (a warning, not a drop, when no ATR is declared)', 'ANET' in [p['tk'] for p in d['picks']])
d, _ = brief('T7b_stop_below_last_close', fresh, prev)
check('T7 a healthy stop raises no such warning', not any('stop above last close' in i['msg'] for i in d['health']['issues']))

# T8 a pick that declares atr14 must keep its stop >= 1 ATR below the last close, or it is rejected
n = with_pick('ANET', atr14=round((last_anet - fresh['picks'][0]['stop']) / 0.5, 2))      # the stop is only 0.5 ATR below the close
d, _ = brief('T8_stop_within_one_atr', n, prev)
check('T8 a stop closer than 1 ATR rejects the pick', 'ANET' not in [p['tk'] for p in d['picks']] and
      any('ANET' in i['msg'] and 'ATR below the last close' in i['msg'] for i in d['health']['issues']), d['health']['issues'])
n = with_pick('ANET', atr14=round((last_anet - fresh['picks'][0]['stop']) / 1.5, 2))      # 1.5 ATR below: fine
d, _ = brief('T8b_stop_clears_the_floor', n, prev)
check('T8 a stop at least 1 ATR below the close is kept', 'ANET' in [p['tk'] for p in d['picks']] and
      not any('ATR below the last close' in i['msg'] for i in d['health']['issues']))
n = with_pick('ANET', atr14=round((last_anet - fresh['picks'][0]['stop']) / 1.0, 2))      # exactly 1.0 ATR: allowed
d, _ = brief('T8c_stop_exactly_one_atr', n, prev)
check('T8 exactly 1 ATR is allowed', 'ANET' in [p['tk'] for p in d['picks']])
check('T8 the floor is one config value in health.py', hl.MIN_STOP_ATR == 1.0)


# T9 the page is labelled with the date of the market data it carries, not the last completed session
import datetime as _dt
old_day = '2026-09-17'                                        # one session older than the last completed one (18 Sep)
n = copy.deepcopy(fresh); n['sections']['indices']['asOf'] = old_day
for q in n['indices']:
    pass
d, _ = brief('T9_label_follows_the_data_date', n, prev)
check('T9 an older-data page is labelled with the data date, not the newer close', d['asOfLabel'] == 'Thu 17 Sep 2026 close' and d['asOf'] == old_day, (d['asOfLabel'], d['asOf']))
d, _ = brief('T9b_label_normal_run', fresh, prev)
check('T9 a normal run is labelled with the last completed session', d['asOfLabel'] == 'Fri 18 Sep 2026 close', d['asOfLabel'])


# L-zero the open page still builds when the brief's scan found nobody, but only when the scan said so explicitly
bz = copy.deepcopy(brief_final); bz['picks'] = []; bz['selection'] = {'qualified': 0, 'scored': 40}
d, _ = opn('L9_zero_pick_brief', live0, brief_data=bz)
check('L9 an open page builds from a brief with zero qualified picks, levels read ok', d['health']['sections']['levels']['status'] == 'ok', d['health']['sections']['levels'])
bz2 = copy.deepcopy(brief_final); bz2['picks'] = []
opn('L9b_empty_without_selection', live0, brief_data=bz2, expect=4)
check('L9b an empty pick list WITHOUT a selection summary is still a failure (exit 4)', True)

def ct(v):
    return {'analystTarget': v, 'targetKind': 'consensus average', 'targetSources': [{'t': 'FactSet', 'target': v}, {'t': 'LSEG', 'target': v}]}


# ───────────── 5 Oct additions: last automated run, catalyst re-score, zero-pick scans ─────────────
# T10 an automated build records "last automated run"; a failed ledger push reads "unpushed"; an interactive build keeps the old value
d, _ = brief('T10_interactive', fresh, prev)
check('T10 an interactive build records no automated run', (d['runs']['lastAutomated'] is None) and d['runs']['thisBuild']['kind'] == 'interactive')
ledger_empty = tr.empty_ledger()
d = brief_t('T10_automated_ok', ledger_empty, extra=['--run-kind', 'automated'])
check('T10 an automated build records its time and status ok', d['runs']['lastAutomated']['status'] == 'ok' and d['runs']['lastAutomated']['at'].endswith('+08:00'), d['runs'])
d = brief_t('T10_automated_unpushed', ledger_empty, extra=['--run-kind', 'automated', '--tracker-warn', 'ledger not pushed'])
check('T10 a failed push is recorded as unpushed', d['runs']['lastAutomated']['status'] == 'unpushed')
d2 = brief_t('T10_then_interactive', ledger_empty, prev=d)
check('T10 an interactive build after it keeps the automated record', d2['runs']['lastAutomated'] == d['runs']['lastAutomated'])
check('T10 the page carries the calendar the browser needs to count sessions', '2026-09-07' in d['calendar']['closed'])

# T11 the routine re-scores catalyst alerts without price history
cat = {**fresh['catalysts'][0]}
cat['tk'] = 'NEWA'
n = copy.deepcopy(fresh); n['catalysts'] = [{**cat, 'px': 95.0, **ct(100.0)}]
d, _ = brief('T11_new_alert', n, prev)
pi_ = d['catalysts'][0]['pricedIn']
check('T11 a new alert is scored from price and target only and is partial', pi_['n'] == 1 and pi_['partial'] and pi_['score'] == 100 and 'partial' in pi_['label'], pi_)
check('T11 a partial score cannot earn the catalyst flag', not any(f['key'] == 'catalyst' for f in d['catalysts'][0]['flags']))
prior = {**cat, 'px': 100.0, 'pricedIn': {'inputs': {'movePct': 12, 'adr20Pct': 4, 'eventHigh': 108, 'eventLow': 100, 'last': 101, 'reactionDate': '2026-09-17', 'sessionsSince': 2, 'runUpPct': 0, 'price': 101, 'target': 130}}}
n = copy.deepcopy(fresh); n['catalysts'] = [{**prior, 'px': 106.0, 'pct': 1.0}]
d, _ = brief('T11_carried_inputs', n, prev)
p2 = d['catalysts'][0]['pricedIn']
check('T11 event-fixed inputs are carried and price-dependent ones re-computed', p2['n'] == 4 and p2['inputs']['price'] == 106.0 and p2['inputs']['sessionsSince'] == 2 and p2['components']['a'] == 25 and p2['components']['d'] == 0, p2)

# T11b the routine lists the same event again (it rewrites `catalysts`): the interactive run's inputs and sector/financials flags survive
prev_c = copy.deepcopy(prev); prev_c['catalysts'] = [{**prior, 'px': 100.0, 'flags': [{'key': 'financials', 'label': 'Financials', 'tip': 't', 'inputs': {}}]}]
n = copy.deepcopy(fresh); n['catalysts'] = [{k: v for k, v in {**prior, 'px': 106.0}.items() if k != 'pricedIn'}]
d, _ = brief('T11b_relisted', n, prev_c)
check('T11b a re-listed alert keeps its carried inputs and flags', d['catalysts'][0]['pricedIn']['n'] == 4 and [f['key'] for f in d['catalysts'][0]['flags']] == ['financials', 'catalyst'], d['catalysts'][0].get('flags'))      # the catalyst flag is recomputed from the fresh score

# T12 a scan that found nobody is a healthy page, not a failure
zero = copy.deepcopy(fresh); zero['picks'] = []; zero['selection'] = {'version': 'quality-max10-v1', 'scored': 40, 'qualified': 0, 'cut': 0, 'config': {'minReadiness': 55, 'maxPicks': 10}}
zero['sections']['technical'] = {'asOf': fresh['sections']['technical']['asOf'], 'source': 'test'}
d, _ = brief('T12_zero_picks', zero, prev)
check('T12 zero qualified picks builds and the technical section is ok', d['health']['sections']['technical']['status'] == 'ok' and d['picks'] == [] and d['selection']['qualified'] == 0, d['health']['sections']['technical'])
d3, _ = brief('T12_zero_then_routine', {k: v for k, v in fresh.items() if k not in ('picks', 'pickWindow', 'industries', 'nearmiss')}, d)
check('T12 the routine (which omits picks) carries the zero-pick result forward as pinned', d3['health']['sections']['technical']['status'] == 'pinned' and d3['picks'] == [] and d3['selection']['qualified'] == 0, d3['health']['sections']['technical'])
two = copy.deepcopy(fresh)
hi = {**fresh['catalysts'][0], 'tk': 'HIGH', 'px': 100.0, **ct(100.0)}
lo = {**fresh['catalysts'][0], 'tk': 'LOWW', 'px': 70.0, **ct(100.0)}
none = {k: v for k, v in {**fresh['catalysts'][0], 'tk': 'NONE', 'px': 70.0}.items() if k != 'target'}
two['catalysts'] = [hi, none, lo]
d, _ = brief('T12_ranking', two, prev)
check('T12 alerts are ranked lowest priced-in first, unscored last', [c['tk'] for c in d['catalysts']] == ['LOWW', 'HIGH', 'NONE'], [(c['tk'], c['pricedIn']['score']) for c in d['catalysts']])

# ───────────── 6 Oct: one as-of for the tiles and the regime score (a live print never sits next to closes) ─────────────
import datetime as _dt2
_close = _dt2.datetime(2026, 9, 18, 20, 0, tzinfo=_dt2.timezone.utc)                    # the last completed session at BRIEF_NOW
_ts_close = int((_close + _dt2.timedelta(hours=1, minutes=19)).timestamp())
_ts_live = int(_dt2.datetime(2026, 9, 21, 12, 10, tzinfo=_dt2.timezone.utc).timestamp())    # Monday premarket
n = copy.deepcopy(fresh)
for ix in n['indices']:
    ix['ts'] = _ts_close
vix = [i for i in n['indices'] if i['id'] == 'VIX'][0]
vix.update(ts=_ts_live, prev=14.81)
vix['q'].update(price=17.0, change=2.19, pct=14.8)
d, _ = brief('T13_vix_live_print', n, prev)
v = [i for i in d['indices'] if i['id'] == 'VIX'][0]
check('T13 a live print is labelled as the odd one out', v.get('asOfKind') == 'live' and 'live print' in v['asOfNote'], v)
check('T13 the regime score uses its previous close, not the live print', v.get('inRegime') == 'previous close' and v['regimeQ']['price'] == 14.81 and v['q']['price'] == 17.0, v)
check('T13 the closes are left alone', all('asOfKind' not in i for i in d['indices'] if i['id'] != 'VIX'))
check('T13 health says so', any('live print' in i['msg'] and i['section'] == 'indices' for i in d['health']['issues']), d['health']['issues'])
vix.pop('prev')
d, _ = brief('T13b_live_without_prev', n, prev)
v = [i for i in d['indices'] if i['id'] == 'VIX'][0]
check('T13b a live print with no previous close is kept out of the regime score', v.get('inRegime') is False and 'regimeQ' not in v)
n2 = copy.deepcopy(fresh)
for ix in n2['indices']:
    ix['ts'] = _ts_close
d, _ = brief('T13c_all_closes', n2, prev)
check('T13c indices stamped at the close are untouched', not any('asOfKind' in i for i in d['indices']))

# ───────────── 6 Oct: analyst targets are enforced in the builder, not in the prompt ─────────────
base_c = {k: v for k, v in {**fresh['catalysts'][0], 'tk': 'TGTX', 'px': 156.0}.items() if k != 'target'}
stored = {**base_c, 'pricedIn': {'inputs': {'movePct': 0.17, 'adr20Pct': 2.177, 'eventHigh': 159.92, 'eventLow': 155.96, 'last': 156.0, 'sessionsSince': 3,
                                            'runUpPct': -2.77, 'price': 156.0, 'target': 164.68, 'reactionDate': '2026-09-17', 'targetSource': 'stockanalysis.com consensus (single source)'}}}
prev_t = copy.deepcopy(prev); prev_t['catalysts'] = [stored]
n = copy.deepcopy(fresh); n['catalysts'] = [{**base_c, 'analystTarget': 190}]        # the routine writes one broker's target for an alert whose target is already stored
d, _ = brief('T14_stored_target_kept', n, prev_t)
c0 = d['catalysts'][0]
check('T14 a stored target is never overwritten by the routine', c0['pricedIn']['inputs']['target'] == 164.68, c0['pricedIn']['inputs'])
check('T14 the page says it was ignored', any('ignored' in i['msg'] and 'TGTX' in i['msg'] for i in d['health']['issues']), d['health']['issues'])
n = copy.deepcopy(fresh); n['catalysts'] = [{**base_c, 'analystTarget': 190}]        # a NEW alert, one broker, no sources
d, _ = brief('T14b_single_broker_rejected', n, prev)
c0 = d['catalysts'][0]
check('T14b a single broker target is rejected', c0['pricedIn']['inputs'].get('target') is None and any('rejected' in i['msg'] for i in d['health']['issues']), c0['pricedIn'])
good = {**base_c, 'analystTarget': 190, 'targetKind': 'consensus average', 'targetSources': [{'t': 'FactSet', 'target': 188.0}, {'t': 'LSEG', 'target': 192.0}]}
n = copy.deepcopy(fresh); n['catalysts'] = [good]
d, _ = brief('T14c_two_sources_accepted', n, prev)
c0 = d['catalysts'][0]
check('T14c two named sources within 5% on the consensus average are accepted', c0['pricedIn']['inputs']['target'] == 190 and 'FactSet + LSEG' in c0['pricedIn']['inputs']['targetSource'], c0['pricedIn']['inputs'])
for label, srcs, kind in (('disagree', [{'t': 'FactSet', 'target': 170.0}, {'t': 'LSEG', 'target': 192.0}], 'consensus average'), ('one outlet twice', [{'t': 'FactSet', 'target': 190.0}, {'t': 'FactSet', 'target': 191.0}], 'consensus average'),
                          ('not a consensus', [{'t': 'FactSet', 'target': 190.0}, {'t': 'LSEG', 'target': 191.0}], 'single broker')):
    n = copy.deepcopy(fresh); n['catalysts'] = [{**good, 'targetSources': srcs, 'targetKind': kind}]
    d, _ = brief('T14d_' + label.replace(' ', '_'), n, prev)
    check(f'T14d sources that {label} are rejected', d['catalysts'][0]['pricedIn']['inputs'].get('target') is None, d['catalysts'][0]['pricedIn']['inputs'])

# ───────────── 6 Oct: a catalyst link is shown only when it is on the verified list ─────────────
n = copy.deepcopy(fresh)
c0 = n['catalysts'][0]
keep_u, drop_u = c0['sources'][0]['u'], c0['sources'][1]['u']
vl = {'links': [{'url': keep_u, 'publisher': 'Verified Pub', 'date': '2026-09-17', 'covers': c0['tk'], 'checked': {'at': '2026-10-06', 'how': 'WebFetch, text names the event'}}]}
d, _ = brief('T15_link_policy', n, prev, verified=vl)
s0 = next(c for c in d['catalysts'] if c['tk'] == c0['tk'])['sources']
check('T15 a verified link is kept, with the publisher from the verified list', s0[0] == {'t': 'Verified Pub', 'u': keep_u, 'verified': True}, s0)
check('T15 an unlisted link is dropped to a name, flagged unverified', 'u' not in s0[1] and s0[1]['verified'] is False and s0[1]['t'] == c0['sources'][1]['t'], s0)
check('T15 health notes which link was not verified, without degrading the page', any('not verified' in i['msg'] and c0['tk'] in i['msg'] and i['level'] == 'info' for i in d['health']['issues']) and d['health']['status'] == 'ok', d['health'])
d, _ = brief('T15b_no_list', n, prev, verified={'links': []})
check('T15b with no verified list every URL is gone and the page still builds', all('u' not in s for c in d['catalysts'] for s in c['sources']), d['catalysts'][0]['sources'])
html_txt = (OUT / 'T15b_no_list.html').read_text(encoding='utf-8')
check('T15b no routine-written URL appears anywhere in the page source', drop_u not in html_txt and keep_u not in html_txt)

# ───────────── 6 Oct: QQQ is carried (labelled), screenNotes are carried unless the routine delivers non-empty ─────────────
n = copy.deepcopy(fresh); n['indices'] = [i for i in n['indices'] if i['id'] != 'QQQ']
pv = copy.deepcopy(prev)
check('T16 fixture: previous page has a QQQ tile', any(i['id'] == 'QQQ' for i in pv['indices']))
d, _ = brief('T16_qqq_carry', n, pv)
qq = [i for i in d['indices'] if i['id'] == 'QQQ']
check('T16 the last QQQ tile is carried, labelled as an earlier close, outside the regime score',
      len(qq) == 1 and qq[0]['asOfKind'] == 'carried' and qq[0]['inRegime'] is False and 'not refreshed' in qq[0]['asOfNote'], qq)
check('T16 the carry is a health warning, and a second carry keeps the original date',
      any('QQQ carried forward' in i['msg'] for i in d['health']['issues']))
d2, _ = brief('T16b_qqq_carry_twice', n, d)
qq2 = [i for i in d2['indices'] if i['id'] == 'QQQ'][0]
check('T16b carrying a carried tile keeps the original close date', qq2['carried'] == qq[0]['carried'], (qq2, qq))
d3, _ = brief('T16c_qqq_fresh_wins', fresh, pv)
check('T16c a fresh QQQ replaces the carried one', [i for i in d3['indices'] if i['id'] == 'QQQ'][0].get('asOfKind') != 'carried')
pv2 = copy.deepcopy(prev); pv2['screenNotes'] = ['pinned scan note']
n2 = copy.deepcopy(fresh); n2['screenNotes'] = []
d, _ = brief('T17_screennotes_carry', n2, pv2)
check('T17 an empty screenNotes from the routine does not blank the pinned notes', d.get('screenNotes') == ['pinned scan note'], d.get('screenNotes'))
n3 = copy.deepcopy(fresh); n3.pop('screenNotes', None)
d, _ = brief('T17b_screennotes_absent', n3, pv2)
check('T17b an absent screenNotes carries the pinned notes', d.get('screenNotes') == ['pinned scan note'], d.get('screenNotes'))
n4 = copy.deepcopy(fresh); n4['screenNotes'] = ['new note']
d, _ = brief('T17c_screennotes_new', n4, pv2)
check('T17c a non-empty screenNotes replaces them', d.get('screenNotes') == ['new note'], d.get('screenNotes'))

# routine writes names only: two named sources are enough, one is not, a non-https URL is still refused
n = copy.deepcopy(fresh)
tk0 = n['catalysts'][0]['tk']
n['catalysts'][0]['sources'] = [{'t': 'Reuters'}, {'t': 'Bloomberg'}]
d, _ = brief('T18_names_only', n, prev, verified={'links': []})
check('T18 an alert with two named sources and no URL is kept', any(c['tk'] == tk0 for c in d['catalysts']), [c['tk'] for c in d['catalysts']])
n['catalysts'][0]['sources'] = [{'t': 'Reuters'}]
d, _ = brief('T18b_one_name', n, prev, verified={'links': []})
check('T18b an alert with one named source is dropped', all(c['tk'] != tk0 for c in d['catalysts']), [c['tk'] for c in d['catalysts']])
n['catalysts'][0]['sources'] = [{'t': 'Reuters', 'u': 'http://x.test/a'}, {'t': 'Bloomberg'}]
d, _ = brief('T18c_http', n, prev, verified={'links': []})
check('T18c a non-https URL is refused', all(c['tk'] != tk0 for c in d['catalysts']), [c['tk'] for c in d['catalysts']])

print(f'{len(results)} scenarios passed')
for r in results:
    print('  ok -', r)
