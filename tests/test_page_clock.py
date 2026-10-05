"""Browser-side data-age and missed-run logic, with the page clock pinned (Playwright). Also checks the JS session counter
against market_time.py (same holiday calendar, same answer). Usage:  python tests/test_page_clock.py out/scan/brief.html"""
import datetime as dt, pathlib, sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'scripts'))
import market_time as mt

html = pathlib.Path(sys.argv[1]).resolve()
UTC = dt.timezone.utc
# (label, fixed UTC time, expected scan age for data as of 2026-10-02, expected colour class, run MISSED?)
CASES = [
    ('Mon 5 Oct pre-open (run not due yet)', dt.datetime(2026, 10, 5, 7, 20, tzinfo=UTC), 0, '', False),
    ('Mon 5 Oct 12:20Z (run is inside its 30 min grace)', dt.datetime(2026, 10, 5, 12, 20, tzinfo=UTC), 0, '', False),
    ('Tue 6 Oct 13:00Z (Monday ran? no record -> after Tue grace the Tue run is missed)', dt.datetime(2026, 10, 6, 13, 0, tzinfo=UTC), 1, '', True),
    ('Wed 7 Oct 15:00Z', dt.datetime(2026, 10, 7, 15, 0, tzinfo=UTC), 2, 'amber', True),
    ('Thu 8 Oct 15:00Z', dt.datetime(2026, 10, 8, 15, 0, tzinfo=UTC), 3, 'red', True),
]
fails = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    for label, t, want_age, want_cls, want_missed in CASES:
        ctx = b.new_context(viewport={'width': 1200, 'height': 900})
        page = ctx.new_page()
        errs = []
        page.on('pageerror', lambda e, errs=errs: errs.append(str(e)))
        page.on('console', lambda m, errs=errs: errs.append(m.text) if m.type == 'error' else None)
        page.clock.install(time=t)
        page.goto(html.as_uri())
        page.wait_for_selector('#ageRow')
        chip = page.locator('#ageRow .agechip.big')
        txt = chip.inner_text()
        cls = chip.get_attribute('class')
        run = page.locator('#runLine').inner_text()
        js_last = page.evaluate('window.__briefAge.lastSession')
        py_last = mt.last_completed_session(t).isoformat()
        js_age = page.evaluate("window.__briefAge.ageOf('2026-10-02')")
        # python reference age
        n, d = 0, dt.date(2026, 10, 2)
        while d < dt.date.fromisoformat(py_last):
            d += dt.timedelta(days=1)
            n += mt.is_trading_day(d)
        problems = []
        if js_last != py_last:
            problems.append(f'last session js {js_last} != py {py_last}')
        if js_age != n or js_age != want_age:
            problems.append(f'age js {js_age} py {n} expected {want_age}')
        if want_cls and want_cls not in cls:
            problems.append(f'class {cls!r} lacks {want_cls}')
        if not want_cls and ('amber' in cls or 'red' in cls):
            problems.append(f'class {cls!r} should be neutral')
        if ('MISSED' in run) != want_missed:
            problems.append(f'missed-run flag wrong: {run!r}')
        if errs:
            problems.append(f'console errors {errs}')
        print(('FAIL ' if problems else 'ok   ') + label, '|', txt, '|', run[:90], problems or '')
        fails += problems
        ctx.close()
    b.close()
sys.exit(1 if fails else 0)
