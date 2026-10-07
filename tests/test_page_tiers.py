"""Browser check: Tier A picks first, a 'Technical setups (flagged)' divider, Tier B cards carry their chips; an unverified catalyst link renders as a name.
Run: python tests/test_page_tiers.py"""
import copy, json, pathlib, subprocess, sys, tempfile

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
fresh = json.loads((ROOT / 'data/brief-data.example.json').read_text(encoding='utf-8'))
NOW = '2026-09-21T12:00:00Z'


def build(tmp, name, data):
    p = pathlib.Path(tmp) / f'{name}.json'
    p.write_text(json.dumps(data), encoding='utf-8')
    out = pathlib.Path(tmp) / f'{name}.html'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_brief.py'), '--data', str(p), '--out', str(out), '--save-data', str(pathlib.Path(tmp) / f'{name}.saved.json'),
                    '--now', NOW, '--verified-links', str(pathlib.Path(tmp) / 'none.json')], check=True, capture_output=True)
    return out


problems = []
with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
    d = copy.deepcopy(fresh)
    base = d['picks'][0]
    a = {**copy.deepcopy(base), 'tier': 'A', 'chips': [], 'selectionRank': 1}
    b = {**copy.deepcopy(base), 'tk': 'BBBB', 'name': 'B Corp', 'tier': 'B', 'selectionRank': 2,
         'chips': [{'key': 'rr', 'label': 'R:R 0.80', 'tone': 'bad'}, {'key': 'wide-stop', 'label': 'wide stop (9.1%)', 'tone': 'warn'}]}
    rd0 = {'composite': 60, 'tightness': 15, 'proximity': 15, 'volumeDryUp': 15, 'timeInBase': 15}
    a['readiness'] = rd0
    b['readiness'] = {**rd0, 'composite': 99}                       # higher readiness than A: it must still sort below the Tier A card
    d['picks'] = [b, a]
    d['selection'] = {'version': 'tiered-v3', 'scored': 20, 'qualified': 1, 'cut': 0, 'tierA': 1, 'tierB': 1, 'config': {'minReadiness': 55, 'maxPicks': 10, 'tierBMinReadiness': 45,
                      'minSessionsSincePivot': 2, 'maxBelowPivotPct': 5, 'stopMinAtr': 1, 'stopMinAdr': 0.5, 'maxRiskPct': 8, 'minRR': 1.5, 'targetPct': 10, 'earningsBlackoutSessions': 3}}
    html = build(tmp, 'tiers', d)
    br = pw.chromium.launch()
    pg = br.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
    pg.goto(pathlib.Path(html).resolve().as_uri())
    pg.wait_for_selector('article.pick.tech')
    ids = pg.eval_on_selector_all('article.pick.tech', 'els => els.map(e => e.id)')
    if ids != [f'pick-{a["tk"]}', 'pick-BBBB']:
        problems.append(f'order {ids}: Tier A must come first even with lower readiness')
    if 'Technical setups (flagged)' not in pg.inner_text('body'):
        problems.append('no flagged-setups divider')
    card_b = pg.inner_text('#pick-BBBB')
    for needle in ('R:R 0.80', 'wide stop (9.1%)', 'TECHNICAL · B'):
        if needle not in card_b:
            problems.append(f'Tier B card lacks {needle!r}')
    if 'R:R 0.80' in pg.inner_text(f'#pick-{a["tk"]}'):
        problems.append('Tier A card shows a Tier B chip')
    cat = ' '.join(pg.locator('article.cat').all_inner_texts())
    if 'link not verified' not in cat:
        problems.append('catalyst sources with no verified link are not shown as names ("link not verified")')
    if pg.locator('article.cat a[href]').count():
        problems.append('an unverified catalyst link is still a clickable link')
    if errs:
        problems.append(f'console errors: {errs[:3]}')
    br.close()
print('PROBLEMS:' if problems else 'ok', problems or '')
sys.exit(1 if problems else 0)
