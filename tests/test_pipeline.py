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
live0 = json.loads((root / 'data/open-live.json').read_text(encoding='utf-8'))
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


def brief(name, new, prev=None, now=BRIEF_NOW, expect=0):
    out = OUT / f'{name}.html'
    if out.exists():
        out.unlink()
    argv = ['--data', jw(f'{name}.data.json', new) if new is not None else str(OUT / 'missing.json'),
            '--out', str(out), '--save-data', str(OUT / f'{name}.saved.json'), '--now', now]
    if prev is not None:
        argv += ['--previous', jw(f'{name}.prev.json', prev)]
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

# B2 sector source down: 'sectors' never delivered -> previous copy, flagged stale. 'industries' is pinned
# (never fetched automatically), so its absence is normal and it stays 'pinned', not 'stale'.
n = copy.deepcopy(fresh); [n.pop(k) for k in ('sectors', 'industries', 'sectorSource')]
d, _ = brief('B2_sector_source_down', n, prev)
h = d['health']['sections']['sectors']
check('B2 sector ranking falls back to previous copy', h['status'] == 'stale' and len(d['sectors']) == 11 and d['health']['status'] == 'degraded', h)
check('B2 industries stay pinned, not marked stale', d['health']['sections']['industries']['status'] == 'pinned' and len(d['industries']) == 3)

# B3 index quote garbage (negative price, VIX 400)
n = copy.deepcopy(fresh); n['indices'][0]['q']['price'] = -7650.5; n['indices'][4]['q']['price'] = 400
d, _ = brief('B3_garbage_indices', n, prev)
check('B3 garbage index quotes are rejected', d['health']['sections']['indices']['status'] == 'stale' and d['indices'][0]['q']['price'] == 7650.5)

# B4 the whole fetch step produced nothing. Automated sections fall back to stale; pinned sections (never
# fetched by the automated routine in the first place) stay 'pinned', not 'stale'.
d, _ = brief('B4_total_fetch_failure', None, prev)
check('B4 total failure republishes fetched sections as stale',
      all(d['health']['sections'][s]['status'] == 'stale' for s in ('indices', 'macro', 'sectors', 'catalysts'))
      and d['health']['status'] == 'degraded')
check('B4 pinned sections stay pinned even on total fetch failure',
      all(d['health']['sections'][s]['status'] == 'pinned' for s in ('industries', 'technical', 'nearmiss')))

# B5 total failure with no previous copy -> nothing publishable, exit 3, no page written
_, code = brief('B5_nothing_to_show', None, None, expect=3)
check('B5 no data and no history -> refuses to publish', code == 3 and not (OUT / 'B5_nothing_to_show.html').exists())

# B6 one pick has stop above entry; the rest survive
n = copy.deepcopy(fresh); n['picks'][1]['stop'] = n['picks'][1]['entry'] + 5
d, _ = brief('B6_one_bad_pick', n, prev)
check('B6 only the bad pick is dropped', [p['tk'] for p in d['picks']] == ['ANET', 'CVX', 'SNX'] and any('dropped' in i['msg'] for i in d['health']['issues']), [p['tk'] for p in d['picks']])

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
                   '--out', str(OUT / 'brief_final.html'), '--save-data', str(brief_final_path), '--now', BRIEF_NOW])
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
code, _ = run(build_open, ['--brief-data', str(OUT / 'nope.json'), '--live', str(root / 'data/open-live.json'), '--out', str(out), '--now', OPEN_NOW])
check('L12 without brief levels the open page refuses to build', code == 4)

# L13 catalyst alerts (hybrid shape: no entryZone/entry/stop/target) must not crash the open-page build
b = copy.deepcopy(brief_final)
b['catalysts'] = [{'tk': 'TEST', 'name': 'Test Co', 'industry': 'Software - Application', 'sector': 'Technology',
                    'ctype': 'Earnings beat', 'what': 'Beat on EPS.', 'px': 50.0, 'pct': 2.0, 'cap': '$10B', 'topSector': True}]
n = copy.deepcopy(live0); n['quotes']['TEST'] = {'px': 51.0, 'pct': 2.1, 'vol': 1000000, 'avgVol': 800000}
d, _ = opn('L13_catalyst_alert_no_levels', n, brief_data=b)
check('L13 catalyst alerts without computed levels build without crashing', any(c['tk'] == 'TEST' for c in page_json(str(OUT / 'L13_catalyst_alert_no_levels.html'), 'brief-data')['catalysts']))

print(f'{len(results)} scenarios passed')
for r in results:
    print('  ok -', r)
