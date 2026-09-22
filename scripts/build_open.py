"""Render open-update.html from the brief's levels + the fetched live snapshot.

The fetch itself (FMP / web calls) is done by the routine's agent, which writes data/open-live.json. This script is
deterministic: validate, degrade gracefully, render. It never calls the network.

  python scripts/build_open.py [--brief-data out/last-good/brief-data.json | --brief-html out/published-brief.html]
                               [--live data/open-live.json] [--previous-live PATH | --previous-html PATH]
                               [--brief-url https://...] [--now 2026-09-22T13:25:00Z] [--out out/open-update.html]

Exit: 0 built (banner if degraded) · 3 nothing publishable (no indices AND no quotes) · 4 no brief levels to work from.
"""
import argparse, json, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import market_time as mt, health as hl, health_live as hv

root = pathlib.Path(__file__).resolve().parent.parent


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        print(f'note: could not read {path}: {e}', file=sys.stderr)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--brief-data', default=str(root / 'out/last-good/brief-data.json'))
    ap.add_argument('--brief-html')
    ap.add_argument('--live', default=str(root / 'data/open-live.json'))
    ap.add_argument('--previous-live')
    ap.add_argument('--previous-html')
    ap.add_argument('--brief-url')
    ap.add_argument('--out', default=str(root / 'out/open-update.html'))
    ap.add_argument('--save-live', default=str(root / 'out/last-good/open-live.json'))
    ap.add_argument('--now')
    a = ap.parse_args(argv)

    now = mt.now_utc() if not a.now else mt.dt.datetime.fromisoformat(a.now.replace('Z', '+00:00'))
    et_date = mt.to_et(now).date()
    ctx = {'now': now, 'etDate': et_date, 'openUTC': mt.us_open_utc(et_date), 'lastSession': mt.last_completed_session(now)}

    brief = None
    if a.brief_html:
        try:
            brief = hl.load_json_from_html(a.brief_html)
        except Exception as e:
            print(f'note: brief html unreadable: {e}', file=sys.stderr)
    if brief is None:
        brief = load(a.brief_data)
    if not brief or not brief.get('picks') or not brief.get('watch'):
        print('FAILED: no usable brief data, so there are no trade levels to check', file=sys.stderr)
        return 4

    new = load(a.live)
    old = load(a.previous_live) if a.previous_live else None
    if old is None and a.previous_html:
        try:
            old = hl.load_json_from_html(a.previous_html, 'live-data')
        except Exception as e:
            print(f'note: previous open page unreadable: {e}', file=sys.stderr)
    live, health = hv.assemble(new, old, brief, ctx)
    if a.brief_url:
        live.setdefault('links', {})['brief'] = a.brief_url
    live['asOf'] = mt.to_et(now).strftime('%a %d %b %Y · %H:%M ET') + ' · ' + now.astimezone(mt.HKT).strftime('%H:%M HK')

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': health['status'], 'phase': health['phase'], 'sections': {k: v['status'] for k, v in health['sections'].items()}, 'issues': health['issues']}
    (out.parent / 'open-build-report.json').write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding='utf-8')
    if health['status'] == 'failed':
        print('FAILED: no index or stock quotes; not overwriting the published page', file=sys.stderr)
        return 3

    keep = ['tk', 'name', 'industry', 'sector', 'entryZone', 'entry', 'stop', 'target']
    subset = {'asOf': brief.get('asOf'), 'asOfLabel': brief.get('asOfLabel'), 'builtLabel': brief.get('builtLabel'),
              'indices': brief['indices'], 'meta': brief.get('meta'), 'watch': brief['watch'],
              'picks': [{k: p[k] for k in keep} for p in brief['picks']],
              'catalysts': [{k: p[k] for k in keep} for p in brief.get('catalysts', [])]}
    style = re.search(r'<style>(.*?)</style>', (root / 'templates/brief.template.html').read_text(encoding='utf-8'), re.S).group(1)
    tpl = (root / 'templates/open-update.template.html').read_text(encoding='utf-8')
    html = tpl.replace('/*__STYLE__*/', style).replace('__BRIEF_JSON__', hl.to_script_json(subset)).replace('__LIVE_JSON__', hl.to_script_json(live))
    out.write_text(html, encoding='utf-8')
    sv = pathlib.Path(a.save_live)
    sv.parent.mkdir(parents=True, exist_ok=True)
    sv.write_text(json.dumps(live, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'open page built: {health["status"]} · phase {health["phase"]} · sections {report["sections"]} · {len(health["issues"])} issue(s) -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
