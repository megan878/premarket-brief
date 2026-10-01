"""15-minute sector/watchlist news sweep — a STANDALONE interactive tool, not part of either routine.

Like every other builder in this repo, this script never calls the network. The agent (you, in an interactive
`claude` session, driven by `/loop`) does the actual fetching — the published artifact via the Artifact tool,
and the WebSearch sweeps; this script only extracts/reads the static context and dedupes/formats what you
found. See CLAUDE.md "15-minute sector news loop" for the exact `/loop` invocation and per-cycle instructions.

  python scripts/news_loop.py extract --html <path> [--out out/news-loop/live-data.json]
      Reads a saved copy of the published brief artifact's HTML (the file path the Artifact tool's `read`
      action reports), pulls out the `<script id="brief-data">` JSON block, and writes it to --out. Run this
      once at the start of a loop (and again whenever the published brief changes) so `context` always reflects
      the full merged dataset — sectors, technical picks, catalysts, near-misses — regardless of what the
      automated routine's own git commit of data/brief-data.json contains (that file is routinely a PARTIAL,
      automated-only subset; the full picture only exists in the published artifact. Confirmed 2026-10-01: after
      the first real automated firing, local data/brief-data.json had 0 sectors/picks/near-misses).

  python scripts/news_loop.py context [--data out/news-loop/live-data.json]
      Prints JSON: today's top-3 sectors and the full ~11-name watchlist to search for. Reads the extracted
      live-data cache by default (see `extract` above) — pass --data data/brief-data.json to fall back to the
      local working file instead (only reliable right after an interactive refresh, before the next automated
      routine firing overwrites it).

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


def default_live_data_path():
    return root / 'out' / 'news-loop' / 'live-data.json'


DATA_RE = re.compile(
    r'<script\s+id=["\']brief-data["\']\s+type=["\']application/json["\']\s*>(.*?)</script>',
    re.DOTALL,
)


def cmd_extract(a):
    html = pathlib.Path(a.html).read_text(encoding='utf-8')
    m = DATA_RE.search(html)
    if not m:
        print('error: could not find <script id="brief-data"> in the given HTML', file=sys.stderr)
        return 2
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        print(f'error: brief-data block was not valid JSON: {e}', file=sys.stderr)
        return 2
    out_path = pathlib.Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding='utf-8')
    secs = len(data.get('sectors') or [])
    picks = len(data.get('picks') or [])
    cats = len(data.get('catalysts') or [])
    nm = len(data.get('nearmiss') or [])
    print(f'extracted -> {out_path} (asOf {data.get("asOf", "?")}): '
          f'{secs} sectors, {picks} picks, {cats} catalysts, {nm} near-misses')
    return 0


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

    pe = sub.add_parser('extract')
    pe.add_argument('--html', required=True)
    pe.add_argument('--out', default=str(default_live_data_path()))

    pc = sub.add_parser('context')
    pc.add_argument('--data', default=str(default_live_data_path()))

    pr = sub.add_parser('record')
    pr.add_argument('--log', default=None)
    pr.add_argument('--in', dest='file_in', default=None,
                     help='Read the JSON array from this file instead of stdin (recommended on Windows — '
                          'piping through a shell can mangle non-ASCII characters in the headline text).')

    a = ap.parse_args(argv)
    if a.cmd == 'extract':
        return cmd_extract(a)
    if a.cmd == 'context':
        return cmd_context(a)
    if a.cmd == 'record':
        if a.log is None:
            a.log = str(default_log_path())
        return cmd_record(a)


if __name__ == '__main__':
    sys.exit(main())
