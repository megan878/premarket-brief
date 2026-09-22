"""Render brief.html from data + template, with validation and carry-forward.

  python scripts/build_brief.py --data data/brief-data.json [--previous-html out/published-brief.html | --previous data/prev.json]
                                [--now 2026-09-22T12:07:00Z] [--out out/brief.html]

Exit codes: 0 = built (health ok or degraded, banner shown if not ok) · 3 = nothing publishable (a critical section
has neither fresh nor previous data). In that case NO html is written, so the previously published page stays untouched.
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import market_time as mt, health as hl

root = pathlib.Path(__file__).resolve().parent.parent


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        print(f'note: could not read {path}: {e}', file=sys.stderr)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(root / 'data/brief-data.json'))
    ap.add_argument('--previous')
    ap.add_argument('--previous-html')
    ap.add_argument('--template', default=str(root / 'templates/brief.template.html'))
    ap.add_argument('--out', default=str(root / 'out/brief.html'))
    ap.add_argument('--save-data', default=str(root / 'out/last-good/brief-data.json'))
    ap.add_argument('--now')
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
    final.update(hl.labels(now, ctx['lastSession']))
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
