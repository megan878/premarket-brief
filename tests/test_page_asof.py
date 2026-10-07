"""Browser check: a live VIX print does not move the regime score (it uses the previous close); the tile says it is a live print.
Run: python tests/test_page_asof.py"""
import copy, datetime as dt, json, pathlib, subprocess, sys, tempfile

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
fresh = json.loads((ROOT / 'data/brief-data.example.json').read_text(encoding='utf-8'))
NOW = '2026-09-21T12:00:00Z'
ts_close = int(dt.datetime(2026, 9, 18, 21, 19, tzinfo=dt.timezone.utc).timestamp())
ts_live = int(dt.datetime(2026, 9, 21, 12, 10, tzinfo=dt.timezone.utc).timestamp())


def build(tmp, name, data):
    p = pathlib.Path(tmp) / f'{name}.json'
    p.write_text(json.dumps(data), encoding='utf-8')
    out = pathlib.Path(tmp) / f'{name}.html'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_brief.py'), '--data', str(p), '--out', str(out), '--save-data', str(pathlib.Path(tmp) / f'{name}.saved.json'), '--now', NOW],
                   check=True, capture_output=True)
    return out


def regime_text(pw_page, html):
    pw_page.goto(pathlib.Path(html).resolve().as_uri())
    pw_page.wait_for_selector('.regime')
    return pw_page.locator('.regime .score').inner_text(), pw_page.locator('.tile', has_text='VIX').inner_text()


with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page()
    live = copy.deepcopy(fresh)
    for ix in live['indices']:
        ix['ts'] = ts_close
    v = [i for i in live['indices'] if i['id'] == 'VIX'][0]
    v.update(ts=ts_live, prev=14.81)
    v['q'].update(price=17.0, change=2.19, pct=14.8)                       # a live print ABOVE its 50-day average (16.17); the close was below it
    t_live, tile_live = regime_text(pg, build(tmp, 'live', live))
    closes = copy.deepcopy(fresh)                                           # the same session with the VIX at its close
    for ix in closes['indices']:
        ix['ts'] = ts_close
    t_close, tile_close = regime_text(pg, build(tmp, 'closes', closes))
    asif = copy.deepcopy(live)                                              # what the score would have been had the live print been used as a close
    [i for i in asif['indices'] if i['id'] == 'VIX'][0].pop('ts')
    [i for i in asif['indices'] if i['id'] == 'VIX'][0].pop('prev')
    t_asif, _ = regime_text(pg, build(tmp, 'asif', asif))
    problems = []
    if t_live.split(' ')[0] != t_close.split(' ')[0]:
        problems.append(f'regime with a live print {t_live!r} differs from the closes-only score {t_close!r}')
    if t_live.split(' ')[0] == t_asif.split(' ')[0]:
        problems.append(f'the live print moved the score exactly as a close would have ({t_asif!r}): the test is not discriminating')
    if 'live print' not in tile_live:
        problems.append(f'tile does not say live print: {tile_live!r}')
    if 'live print' in tile_close:
        problems.append('a close was labelled live')
    print('closes-only score :', t_close.split(' checks')[0])
    print('live VIX (labelled):', t_live.split(' checks')[0], '| tile:', tile_live.replace('\n', ' | ')[-90:])
    print('live VIX as a close:', t_asif.split(' checks')[0])
    print('PROBLEMS:' if problems else 'ok', problems or '')
    b.close()
sys.exit(1 if problems else 0)
