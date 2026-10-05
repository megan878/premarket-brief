"""Sanity report on STORED scan data (no fetching): the sequential funnel and its sensitivity to the thresholds.

    python scripts/scan_funnel.py --cands out/scan/candidates.json --stats out/scan/stats.json
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import selection as sel, scan_build as sb

ORDER = ('readiness', 'base', 'proximity', 'stop-valid', 'risk', 'rr', 'earnings')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--cands', required=True); ap.add_argument('--stats', required=True)
    a = ap.parse_args(argv)
    d = json.loads(pathlib.Path(a.cands).read_text(encoding='utf-8'))
    st = json.loads(pathlib.Path(a.stats).read_text(encoding='utf-8'))['tickers']
    scored, _ = sb.prepare(d, st)
    print(f"considered {d['considered']} names over $5B -> universe filters {sum(1 for c in d['candidates'] if not c['universeFail'] or c['universeFail'] == ['not above SMA200'])} -> SMA200 -> {len(scored)} scored\n")
    for t in (45, 55, 65):
        print(f'--- sequential funnel at readiness >= {t} (proximity {sel.CONFIG["maxBelowPivotPct"]:g}%)')
        rows, alive = sel.funnel(scored, {'minReadiness': t}, ORDER)
        print('  ' + '  ->  '.join(f'{g} {n}' for g, n, _ in rows))
        print('  survivors:', [c['tk'] for c in alive])
    print('\n--- each gate on its own, readiness >= 55 (how many of the 53 fail it)')
    by = sel.failure_summary(scored)['byRule']
    print('  ', {g: by.get(g, 0) for g in ORDER})
    print('\n--- final list vs proximity gate x readiness threshold')
    for t in (45, 55, 65):
        for p in (4, 5, 6):
            sv = [c['tk'] for c in scored if sel.check(c, {'minReadiness': t, 'maxBelowPivotPct': p})['passed']]
            print(f'  readiness >= {t}, within {p}% of pivot: {sv}')
    print('\n--- the names that reach readiness 55: every gate')
    for c in sorted(scored, key=lambda c: -c['readiness']['composite']):
        if c['readiness']['composite'] < 55:
            continue
        r = sel.check(c)
        lv = r['levels']
        print(f"  {c['tk']:5} rdy {c['readiness']['composite']:>3} below pivot {(c['pivot'] - c['lastClose']) / c['pivot'] * 100:4.1f}% entry {c['entry']:8.2f} stop {lv['stop']} ({lv['stopNote']}) "
              f"risk {lv['riskPct']}% R:R {lv['rr']} earnings {c.get('nextEarnings')} -> {'PASS' if r['passed'] else 'fails ' + ', '.join(f['rule'] for f in r['failed'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
