"""Browser check: the open-update page shows the brief's last automated run, red when none is recorded since the last scheduled fire.
Run: python tests/test_page_open_runline.py"""
import copy, json, pathlib, subprocess, sys, tempfile

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
raw = json.loads((ROOT / 'data/brief-data.example.json').read_text(encoding='utf-8'))
_tmp = tempfile.mkdtemp()
(pathlib.Path(_tmp) / 'raw.json').write_text(json.dumps(raw), encoding='utf-8')
subprocess.run([sys.executable, str(ROOT / 'scripts/build_brief.py'), '--data', str(pathlib.Path(_tmp) / 'raw.json'), '--out', str(pathlib.Path(_tmp) / 'b.html'),
                '--save-data', str(pathlib.Path(_tmp) / 'b.json'), '--now', '2026-09-21T12:00:00Z'], check=True, capture_output=True)
brief = json.loads((pathlib.Path(_tmp) / 'b.json').read_text(encoding='utf-8'))      # the assembled brief, as the open build expects
live = json.loads((ROOT / 'data/open-live.example.json').read_text(encoding='utf-8'))
NOW = '2026-09-21T13:52:00Z'
problems = []


def build(tmp, name, b):
    bp, lp, out = (pathlib.Path(tmp) / f'{name}.brief.json'), (pathlib.Path(tmp) / f'{name}.live.json'), pathlib.Path(tmp) / f'{name}.html'
    bp.write_text(json.dumps(b), encoding='utf-8'); lp.write_text(json.dumps(live), encoding='utf-8')
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_open.py'), '--brief-data', str(bp), '--live', str(lp), '--out', str(out),
                    '--save-live', str(pathlib.Path(tmp) / f'{name}.saved.json'), '--now', NOW], check=True, capture_output=True)
    return out


with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page()
    pg.clock.install(time='2026-09-21T13:52:00Z')          # the page judges 'missed' against the browser clock
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
    for name, la, want_red in (('none', None, True), ('old', {'at': '2026-09-17T12:03:00+00:00', 'status': 'ok'}, True), ('fresh', {'at': '2026-09-21T12:04:00+00:00', 'status': 'ok'}, False)):
        b = copy.deepcopy(brief)
        b['runs'] = {'lastAutomated': la, 'schedule': {'brief': {'fireUtc': '12:00', 'graceMinutes': 30}}}
        b['calendar'] = {'closed': []}
        pg.goto(build(tmp, name, b).resolve().as_uri())
        pg.wait_for_selector('#runLine')
        txt = pg.inner_text('#runLine')
        red = 'MISSED' in txt
        if red != want_red:
            problems.append(f'{name}: runline {txt!r}, red={red}, wanted {want_red}')
    if errs:
        problems.append(f'console errors {errs[:3]}')
    br.close()
print('PROBLEMS:' if problems else 'ok', problems or '')
sys.exit(1 if problems else 0)
