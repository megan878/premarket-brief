"""One-off backfill of data/picks.json from every pick set that was ever published.

  python scripts/tracker_backfill.py --ohlc data/provenance/backfill-2026-10-03/ohlc.csv
        --fmp-last data/provenance/backfill-2026-10-03/fmp-last.json --now 2026-10-03T08:00:00Z
        [--ledger data/picks.json] [--report data/provenance/backfill-2026-10-03/report.json]

Sources: git history of data/brief-data.json (first commit that carried each set) and the committed artifact-v11 block
for the 2 Oct set. Refuses to run if the ledger already has records (the ledger is append-only).
Network-free: OHLC comes from the committed CSV (fetched via WebFetch, validated here before use).
"""
import argparse, csv, json, pathlib, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tracker as T  # noqa: E402
import market_time as mt  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

SETS = [
    {'name': '2026-09-21 set', 'git': '43e6df5', 'approx': True,
     'publishedAt': '2026-09-22T19:55:58+08:00',
     'why': 'first commit holding the set (43e6df5); the real artifact publish time is unknown but not later than this, '
            'so the first replayed session is conservative'},
    {'name': '2026-10-01 GOOGL set', 'git': 'dedbea3',
     'publishedAt': '2026-10-01T14:43:26+08:00',
     'why': 'commit time of dedbea3; the artifact publish followed within ~25 min, before the 21:30 HKT open'},
    {'name': '2026-10-01 QRVO set', 'git': '4e6fb8f',
     'publishedAt': '2026-10-01T22:15:33+08:00',
     'why': 'artifact v9 version id 1790864133 (epoch seconds) - 45 min after the US open, so replay starts the next session'},
    {'name': '2026-10-02 NVDA set', 'file': 'data/provenance/2026-10-02/artifact-v11.brief-data.json',
     'publishedAt': '2026-10-02T19:46:23+08:00', 'recompute': True,
     'why': 'artifact v10 version id 1790941583 (epoch seconds); only the v11 block is available, so regime and bg come from v11'},
]


def load_block(s):
    if 'git' in s:
        raw = subprocess.run(['git', 'show', f'{s["git"]}:data/brief-data.json'], cwd=ROOT, capture_output=True, check=True)
        return json.loads(raw.stdout.decode('utf-8'))
    return json.loads((ROOT / s['file']).read_text(encoding='utf-8'))


def load_ohlc(path):
    by = {}
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            by.setdefault(r['ticker'], []).append({'date': r['date'], 'open': float(r['open']), 'high': float(r['high']),
                                                   'low': float(r['low']), 'close': float(r['close']),
                                                   'volume': int(r['volume'])})
    return by


def window_to(rows, basis, n):
    upto = [r for r in rows if r['date'] <= basis]
    return upto[-n:]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--ohlc', required=True)
    ap.add_argument('--fmp-last', required=True)
    ap.add_argument('--now', required=True)
    ap.add_argument('--ledger', default=str(ROOT / 'data/picks.json'))
    ap.add_argument('--report', default=None)
    ap.add_argument('--augment', action='store_true',
                    help='only ADD missing fields (adr20Pct, publishTimeApproximate, series) to the existing ledger; never change one')
    a = ap.parse_args(argv)

    ledger = T.load_ledger(a.ledger)
    now = T.pdt(a.now)
    last = mt.last_completed_session(now.astimezone(mt.UTC)).isoformat()
    ohlc_rows = load_ohlc(a.ohlc)
    if a.augment:
        approx = [s['publishedAt'] for s in SETS if s.get('approx')]
        before = json.loads(json.dumps(ledger))
        added = T.augment(ledger, {tk: {'rows': rows} for tk, rows in ohlc_rows.items()}, approx_published=approx)
        for r0, r1 in zip(before['records'], ledger['records']):
            assert all(r1.get(k) == v for k, v in r0.items()), f'augment changed an existing field of {r0["id"]}'
        T.save_ledger(a.ledger, ledger)
        print(json.dumps({'added': len(added), 'byField': {f: sum(1 for _, k in added if k == f) for f in {k for _, k in added}}}))
        return 0
    if ledger['records']:
        sys.exit('ledger already has records - the ledger is append-only, refusing to backfill over it')
    ohlc_all = {tk: {'rows': rows} for tk, rows in ohlc_rows.items()}
    fmp = json.loads(pathlib.Path(a.fmp_last).read_text(encoding='utf-8'))
    report = {'lastSession': last, 'sets': [], 'readinessCheck': [], 'closesCheck': [], 'ohlc': {}, 'replay': []}

    blocks = {}
    for s in SETS:
        b = load_block(s)
        blocks[s['name']] = b
        before = {r['id'] for r in ledger['records']}
        acts = T.ingest(ledger, b, s['publishedAt'], ohlc=ohlc_all, approx=s.get('approx', False))
        new = [r for r in ledger['records'] if r['id'] not in before]
        for r in new:
            r['notes'].append(f'publishedAt: {s["why"]}')
        report['sets'].append({'set': s['name'], 'publishedAt': s['publishedAt'], 'actions': acts})

    # ---- v1 readiness reproducibility check on every scored set (+ recompute for the 2 Oct set) ----
    for s in SETS:
        b = blocks[s['name']]
        for pk in b.get('picks') or []:
            rec = next(r for r in ledger['records'] if r['ticker'] == pk['tk'] and r['publishedAt'].startswith(T.pdt(s['publishedAt']).isoformat()[:19]))
            rows = ohlc_rows.get(pk['tk'], [])
            n = len(pk['closes'])
            win = window_to(rows, rec['basisSession'], n)
            cm = T.closes_match(win, pk['closes'])
            same_multiset = sorted(round(r['close'], 2) for r in win) == sorted(round(c, 2) for c in pk['closes'])
            report['closesCheck'].append({'set': s['name'], 'ticker': pk['tk'], 'window': n, 'differingPositions': len(cm),
                                          'sameValuesDifferentOrder': bool(cm) and same_multiset})
            if not rec['readiness'] and not s.get('recompute'):
                continue
            entry = {'set': s['name'], 'ticker': pk['tk'], 'basis': rec['basisSession'], 'window': n,
                     'closesMismatch': cm, 'published': pk.get('readiness')}
            if len(win) == n:
                rc = T.readiness_v1(win)
                entry['recomputed'] = {k: rc[k] for k in ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')}
                entry['recomputedUnrounded'] = {k: round(v, 2) for k, v in rc['unrounded'].items()}
                entry['pivot'] = rc['pivot']
                entry['timeInBaseDays'] = rc['timeInBaseDays']
                pub = pk.get('readiness')
                entry['match'] = bool(pub) and all(pub[k] == entry['recomputed'][k] for k in entry['recomputed'])
                if pub:
                    entry['diff'] = {k: entry['recomputed'][k] - pub[k] for k in entry['recomputed'] if entry['recomputed'][k] != pub[k]}
                rec['readinessCheck'] = {'window': n, 'recomputed': entry['recomputed'], 'match': entry['match'],
                                         'closesMatchPublished': not cm}
                if s.get('recompute') and not cm:
                    rec['readinessAsPublished'] = pk.get('readiness')
                    rec['readiness'] = dict(entry['recomputed'])
                    rec['scoringVersion'] = T.LEGACY_SCORING_VERSION
                    rec['notes'].append('readiness recomputed with the documented v1 formula (CLAUDE.md "Readiness scoring") from '
                                        'validated OHLC; the published numbers came from the rebuilt formula in score.py of the '
                                        '2026-10-02 scan (provenance zip, not yet committed) and are kept in readinessAsPublished')
            else:
                entry['error'] = f'only {len(win)} of {n} window rows available'
            report['readinessCheck'].append(entry)

    # ---- validate + replay ----
    ohlc = {tk: {'rows': rows, 'fmpLast': fmp.get(tk)} for tk, rows in ohlc_rows.items()}
    rep = T.evaluate(ledger, ohlc, last)
    report['ohlc'] = {'excluded': rep['excluded'], 'awaiting': rep['awaiting'], 'conflicts': rep['conflicts']}
    for r in ledger['records']:
        report['replay'].append({k: r.get(k) for k in ('id', 'status', 'startSession', 'triggerDate', 'fillPrice', 'chased',
                                                       'exitDate', 'exitPrice', 'exitReason', 'rMultiple', 'daysHeld',
                                                       'lastClose', 'evaluatedThrough')})
    T.save_ledger(a.ledger, ledger)
    if a.report:
        pathlib.Path(a.report).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'records': len(ledger['records']), 'excluded': rep['excluded'], 'awaiting': rep['awaiting'],
                      'conflicts': rep['conflicts']}, indent=1, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
