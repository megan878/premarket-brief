"""15-minute sector/watchlist news sweep — a STANDALONE interactive tool, not part of either routine.

Like every other builder in this repo, this script never calls the network. The agent (you, in an interactive
`claude` session, driven by `/loop`) does the actual WebSearch sweeps; this script only reads the static context
to search for and dedupes/formats what you found. See CLAUDE.md "15-minute sector news loop" for the exact
`/loop` invocation and the WebSearch instructions each cycle follows.

  python scripts/news_loop.py context [--data data/brief-data.json]
      Prints JSON: today's top-3 sectors and the full ~11-name watchlist to search for.

  python scripts/news_loop.py record [--in items.json] [--log out/news-loop/seen-<date>.json]
      Reads a JSON array of candidate items (see shape below) — from --in if given, else stdin — drops anything
      already seen today, appends the rest to the log, and prints only the NEW items in the timestamped
      human-readable format. Exits 0 either way (an empty sweep is a normal result, not an error).
      Prefer --in with a file you wrote yourself over piping JSON through a shell: a shell pipe can mangle
      non-ASCII characters (smart quotes, em dashes) in headline text on Windows.

  Candidate item shape (what the agent pipes into `record`):
    {"scope": "GOOGL" | "Technology (sector)" | "Energy (emerging)",
     "headline": "...", "source": "Outlet name", "url": "https://...",
     "tag": "bull" | "bear" | "neutral",
     "invalidates": false,                 # true if this could invalidate a current pick/catalyst
     "note": ""}                           # required when invalidates is true

Dedup key: lowercased, whitespace-collapsed "scope + headline" — exact-ish match, not semantic. The same story
reworded by a different outlet will show up again; that's an acceptable false negative for a 15-minute loop.
"""
import argparse, datetime as dt, json, pathlib, re, sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

root = pathlib.Path(__file__).resolve().parent.parent
HKT = dt.timezone(dt.timedelta(hours=8), 'HKT')


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        print(f'error: could not read {path}: {e}', file=sys.stderr)
        return None


def today_hkt():
    return dt.datetime.now(HKT).date().isoformat()


def default_log_path():
    return root / 'out' / 'news-loop' / f'seen-{today_hkt()}.json'


def cmd_context(a):
    d = load(a.data)
    if not d:
        return 2
    secs = [s for s in (d.get('sectors') or []) if isinstance(s.get('pct'), (int, float))]
    top3 = [s['name'] for s in sorted(secs, key=lambda s: -s['pct'])[:3]]

    watchlist = []
    for p in d.get('picks') or []:
        watchlist.append({'tk': p['tk'], 'name': p.get('name'), 'group': 'technical'})
    for c in d.get('catalysts') or []:
        watchlist.append({'tk': c['tk'], 'name': c.get('name'), 'group': 'catalyst'})
    for n in d.get('nearmiss') or []:
        watchlist.append({'tk': n['tk'], 'name': None, 'group': 'nearmiss'})

    out = {
        'asOf': (d.get('meta') or {}).get('lastSession') or d.get('asOf'),
        'topSectors': top3,
        'allSectors': [s['name'] for s in sorted(secs, key=lambda s: -s['pct'])],
        'watchlist': watchlist,
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def norm_key(scope, headline):
    s = f'{scope} {headline}'.lower()
    s = re.sub(r'[^a-z0-9 ]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def cmd_record(a):
    try:
        raw = pathlib.Path(a.file_in).read_text(encoding='utf-8') if a.file_in else sys.stdin.read()
        items = json.loads(raw or '[]')
    except Exception as e:
        print(f'error: input was not valid JSON: {e}', file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print('error: expected a JSON array on stdin', file=sys.stderr)
        return 2

    log_path = pathlib.Path(a.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = load(log_path) if log_path.exists() else {'seen': {}, 'sweeps': 0}
    if log is None:
        log = {'seen': {}, 'sweeps': 0}
    seen = log.setdefault('seen', {})

    now = dt.datetime.now(HKT)
    stamp = now.strftime('%H:%M')
    new_items = []
    for it in items:
        scope, headline = it.get('scope', '?'), it.get('headline', '')
        if not headline:
            continue
        key = norm_key(scope, headline)
        if key in seen:
            continue
        seen[key] = {'firstSeen': now.isoformat(), 'scope': scope, 'headline': headline}
        new_items.append(it)

    log['sweeps'] = log.get('sweeps', 0) + 1
    log['lastSweepUTC'] = dt.datetime.now(dt.timezone.utc).isoformat()
    log_path.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding='utf-8')

    header = f'=== Sector News Sweep — {now.strftime("%Y-%m-%d %H:%M")} HKT ({len(new_items)} new, sweep #{log["sweeps"]}) ==='
    print(header)
    if not new_items:
        print('No new items this sweep.')
        return 0
    for it in new_items:
        tag = (it.get('tag') or 'neutral').upper()
        src = it.get('source', 'unsourced')
        line = f'[{stamp}] {it.get("scope", "?")} — {it.get("headline", "")} ({src}) [{tag}]'
        print(line)
        if it.get('url'):
            print(f'  {it["url"]}')
        if it.get('invalidates'):
            note = it.get('note') or '(no note given)'
            print(f'  ⚠ POSSIBLE INVALIDATION: {note}')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    pc = sub.add_parser('context')
    pc.add_argument('--data', default=str(root / 'data/brief-data.json'))

    pr = sub.add_parser('record')
    pr.add_argument('--log', default=None)
    pr.add_argument('--in', dest='file_in', default=None,
                     help='Read the JSON array from this file instead of stdin (recommended on Windows — '
                          'piping through a shell can mangle non-ASCII characters in the headline text).')

    a = ap.parse_args(argv)
    if a.cmd == 'context':
        return cmd_context(a)
    if a.cmd == 'record':
        if a.log is None:
            a.log = str(default_log_path())
        return cmd_record(a)


if __name__ == '__main__':
    sys.exit(main())
