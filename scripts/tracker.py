"""Pick tracker: an append-only ledger of every technical pick, followed to an outcome and summarised into stats.

Pure functions, no network. The agent fetches daily OHLC (interactive sessions only - the cloud routine has none and
marks records "awaiting data"); this module validates it, replays the outcome rules, merges ledger copies and computes
the stats. See CLAUDE.md "Pick tracker" for the ledger schema and the outcome rules.

  python scripts/tracker.py update --ledger data/picks.json --data data/brief-data.json
        [--previous-html out/published-brief.html] [--ohlc out/ohlc.json] [--published-at ISO] [--now ISO]
        [--view out/tracker.json]
      Merge the git ledger with the copy embedded in the previous published page, ingest the picks of this publish
      (from --data, else from the previous page), replay every non-terminal record against --ohlc, write the ledger
      and the page view (stats + tables) that build_brief.py embeds.

--ohlc file shape: {"tickers": {"NVDA": {"rows": [{"date","open","high","low","close"[,"volume"]}], "fmpLast": 230.86}}}
"""
import argparse, copy, datetime as dt, json, math, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import market_time as mt  # noqa: E402
from health import MIN_STOP_ATR  # noqa: E402  (defect (a): the one stop-floor config value lives in health.py)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

SCHEMA_VERSION = 1
SCORING_VERSION = 'v2'          # the CURRENT formula; bump together with CLAUDE.md "Readiness scoring"
LEGACY_SCORING_VERSION = 'v1'   # what a pick block without a scoringVersion was scored with (every page before 3 Oct 2026)
RUNUP_SESSIONS = 10             # v2: sessions before the pivot day that the base is measured against
TIGHT_STOP_ADR = 0.5            # tightStopAtPublish: stop within this x ADR20% of the basis close
EXPIRY_SESSIONS = 10            # pending -> expired after this many evaluable sessions without a trigger
MAX_HOLD_SESSIONS = 20          # trigger session = session 1; time-exit at the close of session 20
MIN_N = 20                      # fewer closed trades than this -> "n too small", never a percentage
HKT = dt.timezone(dt.timedelta(hours=8), 'HKT')

# The ONE definition of the "disciplined" stats view: a fill whose open is above entryZoneHigh by >= adrMultiple x ADR20%
# (measured at the basis session) or by more than maxPct is a skipped chase, not a trade. The literal ledger never changes.
CHASE_RULE = {'adrMultiple': 1.0, 'maxPct': 3.0}
SERIES_SESSIONS = 25            # validated closes kept per record for the sparkline, ending at the basis session

TERMINAL = {'invalidated', 'expired', 'stopped', 'target', 'time-exit', 'skipped', 'replaced'}
CLOSED = {'stopped', 'target', 'time-exit'}          # trades that were entered and have finished
NON_TERMINAL = {'pending', 'triggered'}


# ───────────────────────────── helpers ─────────────────────────────
def pdate(s):
    return dt.date.fromisoformat(s) if isinstance(s, str) else s


def pdt(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))


def sessions_between(a, b):
    a, b, out = pdate(a), pdate(b), []
    d = a
    while d <= b:
        if mt.is_trading_day(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def first_session_after(published):
    """First trading day whose 09:30 ET open is strictly after the publish instant (no lookahead)."""
    pub = published if isinstance(published, dt.datetime) else pdt(published)
    d = mt.to_et(pub.astimezone(mt.UTC)).date()
    while True:
        if mt.is_trading_day(d) and mt.us_open_utc(d) > pub.astimezone(mt.UTC):
            return d.isoformat()
        d += dt.timedelta(days=1)


def hkt_date(published):
    pub = published if isinstance(published, dt.datetime) else pdt(published)
    return pub.astimezone(HKT).date().isoformat()


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float('inf')


def parse_zone(z):
    nums = [float(x.replace(',', '')) for x in re.findall(r'\d[\d,]*\.?\d*', z or '')]
    return (nums[0], nums[-1]) if nums else (None, None)


_MONTHS = {m: i + 1 for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


def parse_next_earnings(s, on_or_after):
    """'28 Oct' (also '3 Nov, after close') -> the first such calendar date on/after the publish date. None if unparseable."""
    m = re.match(r'\s*(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?(?:\s*,.*)?\s*$', s or '')
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    ref = pdate(on_or_after)
    for y in (ref.year, ref.year + 1):
        try:
            d = dt.date(y, _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
        if d >= ref:
            return d.isoformat()
    return None


def parse_pick_window_end(s):
    """'25 trading days to 1 Oct 2026, ...' -> '2026-10-01'."""
    m = re.search(r'to\s+(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})', s or '')
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def sort_rows(rows):
    """Rows sorted by date (stable). Exact duplicate rows collapse; a repeated date with different values is kept
    so validate_rows() fails loudly instead of silently picking one."""
    out, seen = [], set()
    for r in sorted(rows or [], key=lambda r: str(r.get('date'))):
        key = json.dumps(r, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def build_closes(rows, upto, n=SERIES_SESSIONS, fmp_last=None):
    """The ONLY way a `closes` array should be built: sort by date, run the same validator as the ledger replay, and
    return the n closes ending at session `upto` ((closes, problems); closes is None when validation fails)."""
    upto = pdate(upto).isoformat()
    srt = [r for r in sort_rows(rows) if str(r.get('date')) <= upto]
    start = mt.trading_days_back(pdate(upto), n)[-1].isoformat()
    ok, probs, clean = validate_rows(srt, start, upto, fmp_last)
    return ([r['close'] for r in clean] if ok else None), probs


def adr20_pct(rows, upto):
    """ADR% = 100 x (mean of High/Low over the 20 sessions ending `upto` - 1); None if 20 validated sessions are not available."""
    upto = pdate(upto).isoformat()
    srt = [r for r in sort_rows(rows) if str(r.get('date')) <= upto]
    start = mt.trading_days_back(pdate(upto), 20)[-1].isoformat()
    ok, _, clean = validate_rows(srt, start, upto)
    if not ok or len(clean) != 20:
        return None
    return round(100 * (sum(r['high'] / r['low'] for r in clean) / 20 - 1), 3)


def card_closes_disagree(card_closes, series, tol=0.011):
    """True when a card's `closes` (ending at the basis session) differ from the validated ledger series."""
    ser = series.get('closes') or []
    if len(ser) < SERIES_SESSIONS or not card_closes:
        return False
    m = min(len(card_closes), SERIES_SESSIONS)
    return any(abs(a - b) > tol for a, b in zip(card_closes[-m:], ser[SERIES_SESSIONS - m:SERIES_SESSIONS]))


def make_series(rows, rec, through):
    """Validated closes for the sparkline: SERIES_SESSIONS sessions ending at the basis session, extended to `through`."""
    start = mt.trading_days_back(pdate(rec['basisSession']), SERIES_SESSIONS)[-1].isoformat()
    srt = [r for r in sort_rows(rows) if str(r.get('date')) <= through]
    ok, probs, clean = validate_rows(srt, start, through)
    if not ok:
        return None, probs
    return {'from': start, 'through': through, 'closes': [r['close'] for r in clean]}, []


# ───────────────────────────── regime at publish (port of the page JS) ─────────────────────────────
def regime_from_block(block):
    """(label, score, total) exactly as brief.template.html computes it; (None, None, None) if inputs are missing."""
    try:
        by = {i['id']: i['q'] for i in block['indices']}
        checks = [by['SPX']['price'] > by['SPX']['ma50'], by['SPX']['price'] > by['SPX']['ma200'],
                  by['DJI']['price'] > by['DJI']['ma50'], by['DJI']['price'] > by['DJI']['ma200'],
                  by['RUT']['price'] > by['RUT']['ma50'], by['RUT']['price'] > by['RUT']['ma200'],
                  by['VIX']['price'] < 20, by['VIX']['price'] < by['VIX']['ma50']]
    except (KeyError, TypeError):
        return None, None, None
    h = (block.get('macro') or {}).get('y10hist') or []
    if len(h) == 5 and all(num(x.get('y10')) for x in h):
        latest, one_ago, five_ago = h[-1]['y10'], h[-2]['y10'], h[0]['y10']
        checks.append(not (latest > one_ago and latest > five_ago))
    score, total = sum(1 for c in checks if c), len(checks)
    on_min = 7 if total >= 9 else 6
    label = 'RISK-ON' if score >= on_min else 'MIXED' if score >= 4 else 'RISK-OFF'
    return label, score, total


# ───────────────────────────── OHLC validation ─────────────────────────────
def validate_rows(rows, from_date, last_session, fmp_last=None):
    """Validate daily rows with date >= from_date. Returns (ok, problems, clean_rows).

    Rows dated after last_session (e.g. today's partial row from an intraday fetch) are ignored, never used.
    Hard checks: numeric o/h/l/c, low <= open/close <= high, strictly increasing dates, only trading days, no missing
    session between from_date and last_session, last date == last_session. fmp_last (an independent last price) is a
    sanity check on the final close: >3% apart is a hard failure, >0.5% is reported as a warning in the problems list
    prefixed 'warn:' (the ticker is still usable).
    """
    probs, clean = [], []
    from_date, last_session = pdate(from_date), pdate(last_session)
    for r in rows or []:
        try:
            d = pdate(r['date'])
        except Exception:
            probs.append(f'unparseable date {r.get("date")!r}')
            continue
        if d < from_date or d > last_session:      # rows after the last completed session are a partial/unfinished day
            continue
        o, h, l, c = (r.get(k) for k in ('open', 'high', 'low', 'close'))
        if not all(num(x) and x > 0 for x in (o, h, l, c)):
            probs.append(f'{d}: non-numeric or non-positive price')
            continue
        if not (l <= o <= h and l <= c <= h):
            probs.append(f'{d}: low/open/close/high out of order (o={o} h={h} l={l} c={c})')
            continue
        v = r.get('volume')
        if v is not None and not (num(v) and v >= 0):
            probs.append(f'{d}: bad volume {v!r}')
            continue
        clean.append({'date': d.isoformat(), 'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
                      'volume': v})
    dates = [r['date'] for r in clean]
    if any(b <= a for a, b in zip(dates, dates[1:])):
        probs.append('dates are not strictly increasing')
    expected = set(sessions_between(from_date, last_session))
    got = set(dates)
    extra = sorted(got - expected)
    if extra:
        probs.append(f'non-session dates: {extra[:4]}')
    missing = sorted(expected - got)
    if missing and expected:
        probs.append(f'missing sessions: {missing[:6]}{"..." if len(missing) > 6 else ""}')
    if clean and clean[-1]['date'] != last_session.isoformat():
        probs.append(f'last date {clean[-1]["date"]} != last session {last_session}')
    hard = [p for p in probs if not p.startswith('warn:')]
    if clean and fmp_last and num(fmp_last):
        dev = abs(clean[-1]['close'] / fmp_last - 1)
        if dev > 0.03:
            hard.append(f'last close {clean[-1]["close"]} vs FMP {fmp_last} differ by {dev * 100:.1f}%')
        elif dev > 0.005:
            probs.append(f'warn: last close {clean[-1]["close"]} vs FMP {fmp_last} differ by {dev * 100:.2f}%')
    if not clean and expected:
        hard.append('no rows in range')
    ok = not hard
    return ok, probs + [p for p in hard if p not in probs], clean


# ───────────────────────────── outcome rules ─────────────────────────────
def _row_ev(r):
    return {k: r[k] for k in ('date', 'open', 'high', 'low', 'close')}


def replay(rec, rows):
    """Replay the outcome rules for one record against validated rows (date >= rec['startSession'], oldest first).

    Pure and idempotent: it always starts from 'pending', so the same rows give the same result however many times
    (or in however many slices) it is run. Returns the outcome fields to overlay on the record.
    """
    entry, stop, target, zone_hi = rec['entry'], rec['stop'], rec['target'], rec['entryZoneHigh']
    risk = entry - stop
    out = {'status': 'pending', 'triggerDate': None, 'fillPrice': None, 'chased': None, 'exitDate': None,
           'exitPrice': None, 'exitReason': None, 'rMultiple': None, 'daysHeld': None, 'lastClose': None,
           'evaluatedThrough': None, 'evidence': {}}
    seen = held = 0
    fill = None
    for r in rows:
        if r['date'] < rec['startSession']:
            continue
        seen += 1
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        out['evaluatedThrough'] = r['date']
        out['lastClose'] = c
        if out['status'] == 'pending':
            if o <= stop:                       # stop broke before any trigger
                out.update(status='invalidated', exitDate=r['date'], exitReason='open-at-or-below-stop')
                out['evidence']['exit'] = _row_ev(r)
                break
            if h >= entry:
                if o >= target:                 # gapped past the target: a real missed trade, not a loss
                    out.update(status='skipped', exitDate=r['date'], exitReason='gapped-past-target')
                    out['evidence']['exit'] = _row_ev(r)
                    break
                fill = max(o, entry)
                out.update(status='triggered', triggerDate=r['date'], fillPrice=fill, chased=fill > zone_hi)
                out['evidence']['trigger'] = _row_ev(r)
            elif l <= stop:                     # traded down through the stop without ever reaching entry
                out.update(status='invalidated', exitDate=r['date'], exitReason='stop-before-trigger')
                out['evidence']['exit'] = _row_ev(r)
                break
            else:
                if seen >= EXPIRY_SESSIONS:
                    out.update(status='expired', exitDate=r['date'], exitReason=f'no-trigger-{EXPIRY_SESSIONS}-sessions')
                    out['evidence']['exit'] = _row_ev(r)
                    break
                continue
        # triggered: this row is session `held` of the trade (the trigger session is session 1)
        held += 1
        exit_px = reason = None
        if l <= stop:                           # stop first when both are hit in one session (conservative)
            exit_px, reason = min(o, stop), 'stopped'
        elif h >= target:
            exit_px, reason = target, 'target'
        elif held >= MAX_HOLD_SESSIONS:
            exit_px, reason = c, 'time-exit'
        if reason:
            out.update(status=reason, exitDate=r['date'], exitPrice=exit_px, exitReason=reason, daysHeld=held,
                       rMultiple=round((exit_px - fill) / risk, 4))
            out['evidence']['exit'] = _row_ev(r)
            break
    if out['status'] in TERMINAL:
        out['evaluatedThrough'] = out['exitDate']
    return out


def is_terminal(rec):
    return rec.get('status') in TERMINAL


# ───────────────────────────── ledger I/O ─────────────────────────────
def empty_ledger():
    return {'schemaVersion': SCHEMA_VERSION, 'records': []}


def load_ledger(path):
    p = pathlib.Path(path)
    if not p.exists():
        return empty_ledger()
    d = json.loads(p.read_text(encoding='utf-8'))
    d.setdefault('schemaVersion', SCHEMA_VERSION)
    d.setdefault('records', [])
    return d


def dump_ledger(ledger):
    lines = ',\n'.join('  ' + json.dumps(r, ensure_ascii=False, separators=(', ', ': ')) for r in ledger['records'])
    return '{"schemaVersion": %d, "records": [\n%s\n]}\n' % (ledger.get('schemaVersion', SCHEMA_VERSION), lines)


def save_ledger(path, ledger):
    pathlib.Path(path).write_text(dump_ledger(ledger), encoding='utf-8', newline='\n')


# ───────────────────────────── ingest ─────────────────────────────
def _new_id(ledger, publish_date, tk):
    base = f'{publish_date}-{tk}'
    ids = {r['id'] for r in ledger['records']}
    if base not in ids:
        return base
    n = 2
    while f'{base}-{n}' in ids:
        n += 1
    return f'{base}-{n}'


def build_record(pick, block, published_at, basis, rec_id, adr=None, approx=False, basis_close=None):
    zl, zh = parse_zone(pick.get('entryZone'))
    rd = pick.get('readiness') if isinstance(pick.get('readiness'), dict) and num(pick['readiness'].get('composite')) else None
    label, score, total = regime_from_block(block)
    pub = pdt(published_at)
    nxt = parse_next_earnings(((block.get('bg') or {}).get(pick['tk']) or {}).get('next'), hkt_date(pub))
    bc = basis_close if basis_close is not None else (pick['closes'][-1] if pick.get('closes') else None)
    inv, why, tight = level_flags(pick['stop'], bc, adr)
    return {
        'id': rec_id, 'publishedAt': pub.isoformat(), 'basisSession': basis, 'startSession': first_session_after(pub),
        'ticker': pick['tk'], 'sector': pick.get('sector'), 'industry': pick.get('industry'), 'setup': pick.get('setup'),
        'pivot': pick.get('pivot'), 'entryZoneLow': zl, 'entryZoneHigh': zh, 'entry': pick['entry'],
        'stop': pick['stop'], 'target': pick['target'],
        'readiness': ({k: rd[k] for k in ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')} if rd else None),
        'scoringVersion': (pick.get('scoringVersion') or block.get('scoringVersion') or LEGACY_SCORING_VERSION) if rd else None,
        'regimeLabel': label, 'regimeScore': score, 'regimeChecks': total, 'nextEarningsDate': nxt,
        'adr20Pct': adr, 'publishTimeApproximate': bool(approx),
        'basisClose': bc, 'levelsInvalidAtPublish': inv, 'levelsInvalidReason': why, 'tightStopAtPublish': tight,
        'status': 'pending', 'triggerDate': None, 'fillPrice': None, 'chased': None, 'exitDate': None,
        'exitPrice': None, 'exitReason': None, 'rMultiple': None, 'daysHeld': None, 'lastClose': None,
        'evaluatedThrough': None, 'repickDates': [], 'evidence': {}, 'notes': [],
    }


def ingest(ledger, block, published_at, source='fresh', ohlc=None, approx=False):
    """Add this block's picks to the ledger (in place). Returns a list of human-readable action strings.

    Identity is (ticker, basisSession): a carry-forward republish of the same pick set is a no-op. A fresh scan that
    re-picks a ticker with a new basisSession closes a still-pending old record as 'replaced' and opens a new one; if
    the old record is triggered, the live trade is kept and only the re-pick date is added to repickDates.
    """
    actions = []
    pub = pdt(published_at)
    pdate_hkt = hkt_date(pub)
    win_end = parse_pick_window_end(block.get('pickWindow'))
    fallback = mt.last_completed_session(pub.astimezone(mt.UTC)).isoformat()
    basis = win_end or fallback
    basis_note = None
    if win_end and win_end != fallback:
        basis_note = f'pickWindow says basis {win_end} but last completed session before publish was {fallback}'
    for pick in block.get('picks') or []:
        tk = pick.get('tk')
        if not tk or not all(num(pick.get(k)) for k in ('entry', 'stop', 'target')):
            actions.append(f'skip {tk}: missing entry/stop/target')
            continue
        recs = ledger['records']
        if any(r['ticker'] == tk and r['basisSession'] == basis for r in recs):
            continue
        live = [r for r in recs if r['ticker'] == tk and not is_terminal(r)]
        rec = None
        for old in live:
            if old['status'] == 'triggered':
                if pdate_hkt not in old['repickDates']:
                    old['repickDates'].append(pdate_hkt)
                actions.append(f'{tk}: re-picked while live ({old["id"]}); kept the open trade, noted {pdate_hkt}')
                rec = 'kept'
        if rec == 'kept':
            continue
        new_id = _new_id(ledger, pdate_hkt, tk)
        rows = ((ohlc or {}).get(tk) or {}).get('rows')
        bc = next((r['close'] for r in (rows or []) if r.get('date') == basis), None)
        new = build_record(pick, block, pub.isoformat(), basis, new_id, adr=adr20_pct(rows, basis) if rows else None, approx=approx,
                           basis_close=bc)
        if basis_note:
            new['notes'].append(basis_note)
        if source != 'fresh':
            new['notes'].append(f'ingested from {source}; publishedAt is the first-ingest time, not the original publish')
        for old in live:                        # pending ones are superseded by the new levels
            old.update(status='replaced', exitDate=pdate_hkt, exitReason='re-picked', replacedBy=new_id,
                       evaluatedThrough=old.get('evaluatedThrough'))
            actions.append(f'{tk}: pending {old["id"]} replaced by {new_id}')
        recs.append(new)
        actions.append(f'{tk}: new record {new_id} (basis {basis}, first session {new["startSession"]})')
    return actions


# ───────────────────────────── evaluate ─────────────────────────────
OUTCOME_FIELDS = ('status', 'triggerDate', 'fillPrice', 'chased', 'exitDate', 'exitPrice', 'exitReason', 'rMultiple',
                  'daysHeld', 'lastClose', 'evaluatedThrough', 'evidence')


def evaluate(ledger, ohlc, last_session, verify_terminal=False):
    """Replay non-terminal records against ohlc ({tk: {'rows': [...], 'fmpLast': x}}). Mutates the ledger.

    Returns {'updated': [ids], 'awaiting': [ids], 'excluded': {tk: [problems]}, 'conflicts': [str]}.
    A ticker with no rows, or rows that fail validation, is left untouched (-> 'awaiting data') and logged.
    verify_terminal=True also replays terminal records and reports (never applies) any disagreement.
    """
    rep = {'updated': [], 'awaiting': [], 'excluded': {}, 'conflicts': []}
    by_tk = {}
    for r in ledger['records']:
        if r['status'] == 'replaced' or r['startSession'] > last_session:   # nothing to evaluate yet
            continue
        if not is_terminal(r) or verify_terminal:
            by_tk.setdefault(r['ticker'], []).append(r)
    for tk, recs in by_tk.items():
        data = (ohlc or {}).get(tk)
        if not data or not data.get('rows'):
            rep['awaiting'] += [r['id'] for r in recs if not is_terminal(r)]
            continue
        from_date = min(r['startSession'] for r in recs)
        ok, probs, rows = validate_rows(data['rows'], from_date, last_session, data.get('fmpLast'))
        if not ok:
            rep['excluded'][tk] = probs
            rep['awaiting'] += [r['id'] for r in recs if not is_terminal(r)]
            continue
        if probs:
            rep['excluded'].setdefault(tk, []).extend(probs)    # warnings only; the ticker was still used
        for r in recs:
            res = replay(r, [x for x in rows if x['date'] >= r['startSession']])
            if is_terminal(r):
                for k in ('status', 'exitDate', 'exitPrice', 'fillPrice'):
                    if res[k] != r.get(k):
                        rep['conflicts'].append(f'{r["id"]}: terminal {k} {r.get(k)!r} but replay gives {res[k]!r}')
                        break
                continue
            for k in OUTCOME_FIELDS:
                r[k] = res[k]
            if r.get('adr20Pct') is None:
                r['adr20Pct'] = adr20_pct(data['rows'], r['basisSession'])
            ser, sprobs = make_series(data['rows'], r, r['evaluatedThrough'] or r['basisSession'])
            if ser:
                r['series'] = ser
            elif sprobs:
                rep['excluded'].setdefault(tk, []).append('warn: sparkline series skipped - ' + '; '.join(sprobs[:2]))
            if res['evaluatedThrough'] and res['evaluatedThrough'] < last_session and res['status'] in NON_TERMINAL:
                rep['awaiting'].append(r['id'])
            rep['updated'].append(r['id'])
    return rep


# ───────────────────────────── merge ─────────────────────────────
def merge_ledgers(a, b):
    """Merge two ledger copies by id. Returns (merged, conflicts).

    Later evaluatedThrough wins for non-terminal fields; a terminal outcome is immutable (and beats a non-terminal
    copy); two terminal copies that disagree keep the first (a) and log a conflict. repickDates/notes are unioned.
    """
    conflicts, out, seen = [], [], set()
    bmap = {r['id']: r for r in b['records']}
    amap = {r['id']: r for r in a['records']}
    for r in a['records'] + [x for x in b['records'] if x['id'] not in amap]:
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        x, y = amap.get(r['id']), bmap.get(r['id'])
        if x is None or y is None:
            out.append(copy.deepcopy(r))
            continue
        tx, ty = is_terminal(x), is_terminal(y)
        if tx and ty:
            keep = x
            if any(x.get(k) != y.get(k) for k in ('status', 'exitDate', 'exitPrice', 'fillPrice')):
                conflicts.append(f'{x["id"]}: terminal outcomes differ between ledger copies ({x["status"]} vs {y["status"]}); kept the first')
        elif tx != ty:
            keep = x if tx else y
        else:
            keep = x if (x.get('evaluatedThrough') or '') >= (y.get('evaluatedThrough') or '') else y
        m = copy.deepcopy(keep)
        m['repickDates'] = sorted(set(x.get('repickDates', [])) | set(y.get('repickDates', [])))
        m['notes'] = list(dict.fromkeys(x.get('notes', []) + y.get('notes', [])))
        out.append(m)
    return {'schemaVersion': SCHEMA_VERSION, 'records': out}, conflicts


# ───────────────────────────── v1 readiness reference (verification only) ─────────────────────────────
def _pw(x, anchors):
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


_LADDER = [(-20, 0), (0, 5), (20, 15), (40, 25), (60, 25)]
_TIB = [(0, 0), (3, 5), (5, 25), (15, 25), (20, 15), (30, 5), (40, 0)]


def round_half_up(v):
    """5.5 -> 6 and 4.5 -> 5 (Python's round() would send 4.5 to 4); the epsilon absorbs float noise on exact ties."""
    return int(math.floor(v + 0.5 + 1e-9))


def readiness_v1(window):
    """The documented v1 formula (CLAUDE.md "Readiness scoring") over a window of validated rows, oldest first.

    Verification/recompute only - the formula itself is unchanged. Returns unrounded components + rounded composite.
    """
    hi = [r['high'] for r in window]
    lo = [r['low'] for r in window]
    cl = [r['close'] for r in window]
    vol = [r.get('volume') or 0 for r in window]
    pivot = max(hi)
    pidx = hi.index(pivot)
    last = len(window) - 1
    tib = last - pidx
    tr = []
    for i in range(len(window)):
        tr.append(hi[i] - lo[i] if i == 0 else max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    price = cl[-1]
    atr_pct = (sum(tr[-14:]) / len(tr[-14:])) / price * 100
    l10 = tr[-10:]
    early, late = sum(l10[:5]) / 5, sum(l10[5:]) / 5
    decline = (early - late) / early * 100 if early else 0
    tight = _pw(decline, _LADDER)
    x = (price / pivot - 1) * 100
    prox = _pw(x, sorted({(-10, 0), (-5, 10), (-2, 25), (0.5, 25), (atr_pct, 10), (2 * atr_pct + 1, 0)}))
    if tib >= 2:
        base = vol[pidx + 1:]
        imp = vol[max(0, pidx - 4):pidx + 1]
        bv, iv = sum(base) / len(base), sum(imp) / len(imp)
        dry = _pw((iv - bv) / iv * 100 if iv else 0, _LADDER)
    else:
        dry = 0.0
    tb = _pw(tib, _TIB)
    comps = {'tightness': tight, 'proximity': prox, 'volumeDryUp': dry, 'timeInBase': tb}
    return {'pivot': pivot, 'timeInBaseDays': tib, 'unrounded': comps,
            **{k: round_half_up(v) for k, v in comps.items()}, 'composite': round_half_up(sum(comps.values()))}


def closes_match(window, published_closes, tol=0.011):
    """Compare the tail of the fetched window to the closes the page published (an independent cross-check)."""
    mine = [r['close'] for r in window][-len(published_closes):]
    if len(mine) != len(published_closes):
        return [f'length {len(mine)} vs {len(published_closes)}']
    return [f'#{i}: {a} vs {b}' for i, (a, b) in enumerate(zip(mine, published_closes)) if abs(a - b) > tol]


# ───────────────────────────── levels sanity: ATR, stop floor, publish-time flags ─────────────────────────────
def true_ranges(rows):
    """True range per row (the first row has no previous close, so it is just high - low)."""
    out = []
    for i, r in enumerate(rows):
        if i == 0:
            out.append(r['high'] - r['low'])
        else:
            pc = rows[i - 1]['close']
            out.append(max(r['high'] - r['low'], abs(r['high'] - pc), abs(r['low'] - pc)))
    return out


def atr14(rows):
    """Mean of the last 14 true ranges of the given (validated, oldest-first) rows; None if there are fewer than 14."""
    tr = true_ranges(rows)
    return round(sum(tr[-14:]) / 14, 4) if len(tr) >= 14 else None


def stop_check(stop, last_close, atr, k=None):
    """Defect (a): the stop must sit at least k x ATR14 below the last close, or the pick is rejected.

    Returns {'ok', 'lastClose', 'atr14', 'atrBelow' (how many ATRs the stop is below the close), 'minAtr'}."""
    k = MIN_STOP_ATR if k is None else k
    below = (last_close - stop) / atr if atr else None
    return {'ok': bool(atr) and stop < last_close and below >= k, 'lastClose': last_close, 'atr14': atr,
            'atrBelow': None if below is None else round(below, 2), 'minAtr': k}


def level_flags(stop, basis_close, adr20):
    """(levelsInvalidAtPublish, reason, tightStopAtPublish) for a pick, judged at its basis-session close.

    invalid: the stop is at or above the close (the pick was already past its own stop when published: a scan defect, not a market outcome).
    tight:   the stop is within TIGHT_STOP_ADR x ADR20% of the close (a normal day's range can take it out). None/None/None if unknown."""
    if basis_close is None or stop is None:
        return None, None, None
    if stop >= basis_close:
        return True, f'stop {stop} is at/above the basis-session close {basis_close}', False
    dist = (basis_close - stop) / basis_close * 100
    return False, None, (adr20 is not None and dist <= TIGHT_STOP_ADR * adr20)


# ───────────────────────────── readiness v2 (current) ─────────────────────────────
def readiness_v2(window):
    """Readiness v2: v1 with tightness and volume dry-up measured against the RUN-UP, excluding the pivot day.

    v1 compared the last 5 true ranges with the 5 before them (and the 5 sessions of volume up to AND INCLUDING the pivot), so one wide,
    heavy breakout day inside that window scored as a contraction (MPWR scored 100 on 2 Oct). v2:
      run-up = the RUNUP_SESSIONS sessions immediately before the pivot day (the pivot day itself is excluded);
      base   = the sessions after the pivot day through the last one (needs >= 2, and >= 5 run-up sessions, else those two score 0).
      tightness = decline of mean true range, base vs run-up, on the v1 anchor ladder;
      dry-up    = decline of mean volume, base vs run-up, on the v1 anchor ladder.
    Proximity and time-in-base are unchanged from v1. Returns the same shape as readiness_v1 plus the intermediates."""
    hi = [r['high'] for r in window]
    cl = [r['close'] for r in window]
    vol = [r.get('volume') or 0 for r in window]
    pivot = max(hi)
    pidx = hi.index(pivot)
    last = len(window) - 1
    tib = last - pidx
    tr = true_ranges(window)
    price = cl[-1]
    atr_pct = (sum(tr[-14:]) / len(tr[-14:])) / price * 100
    runup = list(range(max(0, pidx - RUNUP_SESSIONS), pidx))
    base = list(range(pidx + 1, last + 1))
    info = {'runupSessions': len(runup), 'baseSessions': len(base)}
    if tib >= 2 and len(runup) >= 5:
        ru_tr, b_tr = sum(tr[i] for i in runup) / len(runup), sum(tr[i] for i in base) / len(base)
        ru_v, b_v = sum(vol[i] for i in runup) / len(runup), sum(vol[i] for i in base) / len(base)
        decline = (ru_tr - b_tr) / ru_tr * 100 if ru_tr else 0
        dry_pct = (ru_v - b_v) / ru_v * 100 if ru_v else 0
        tight, dry = _pw(decline, _LADDER), _pw(dry_pct, _LADDER)
        info.update(runupTR=ru_tr, baseTR=b_tr, trDeclinePct=decline, runupVol=ru_v, baseVol=b_v, volDeclinePct=dry_pct)
    else:
        tight = dry = 0.0
    x = (price / pivot - 1) * 100
    prox = _pw(x, sorted({(-10, 0), (-5, 10), (-2, 25), (0.5, 25), (atr_pct, 10), (2 * atr_pct + 1, 0)}))
    tb = _pw(tib, _TIB)
    comps = {'tightness': tight, 'proximity': prox, 'volumeDryUp': dry, 'timeInBase': tb}
    return {'pivot': pivot, 'timeInBaseDays': tib, 'unrounded': comps, 'details': info,
            **{k: round_half_up(v) for k, v in comps.items()}, 'composite': round_half_up(sum(comps.values()))}


def readiness(window, version=None):
    """The score for a window under the named formula version (default: the current one)."""
    return readiness_v1(window) if (version or SCORING_VERSION) == 'v1' else readiness_v2(window)


# ───────────────────────────── augment (add fields to existing records; never change one) ─────────────────────────────
def augment(ledger, ohlc, approx_published=()):
    """Add adr20Pct / publishTimeApproximate / series to records that lack them. Existing values are never touched."""
    added = []
    for r in ledger['records']:
        rows = ((ohlc or {}).get(r['ticker']) or {}).get('rows')
        if 'publishTimeApproximate' not in r:
            r['publishTimeApproximate'] = r['publishedAt'] in set(approx_published)
            added.append((r['id'], 'publishTimeApproximate'))
        if r.get('adr20Pct') is None and rows:
            v = adr20_pct(rows, r['basisSession'])
            if v is not None:
                r['adr20Pct'] = v
                added.append((r['id'], 'adr20Pct'))
        elif 'adr20Pct' not in r:
            r['adr20Pct'] = None
        if r.get('basisClose') is None and rows:
            bc = next((x['close'] for x in rows if x.get('date') == r['basisSession']), None)
            if bc is not None:
                r['basisClose'] = bc
                added.append((r['id'], 'basisClose'))
        elif 'basisClose' not in r:
            r['basisClose'] = None
        if r.get('levelsInvalidAtPublish') is None and r.get('basisClose') is not None:
            inv, why, tight = level_flags(r['stop'], r['basisClose'], r.get('adr20Pct'))
            r['levelsInvalidAtPublish'], r['levelsInvalidReason'], r['tightStopAtPublish'] = inv, why, tight
            added.append((r['id'], 'levelsInvalidAtPublish'))
        else:
            for k in ('levelsInvalidAtPublish', 'levelsInvalidReason', 'tightStopAtPublish'):
                r.setdefault(k, None)
        if 'series' not in r and rows:
            ser, _ = make_series(rows, r, r.get('evaluatedThrough') or r['basisSession'])
            if ser:
                r['series'] = ser
                added.append((r['id'], 'series'))
    return added


# ───────────────────────────── stats ─────────────────────────────
def band(rec):
    rd = rec.get('readiness')
    if not rd:
        return 'unscored'
    c = rd['composite']
    return '<50' if c < 50 else '50-69' if c < 70 else '>=70'


def chase_excess_pct(rec):
    """How far above entryZoneHigh the fill was, in % of the zone top (0 if the fill was not above the zone)."""
    if rec.get('fillPrice') is None or not rec.get('chased'):
        return 0.0
    return (rec['fillPrice'] / rec['entryZoneHigh'] - 1) * 100


def is_chase_skip(rec, rule=None):
    """True when the fill would be refused under CHASE_RULE (open above the zone by >= adrMultiple x ADR20% or > maxPct)."""
    rule = rule or CHASE_RULE
    x = chase_excess_pct(rec)
    if x <= 0:
        return False
    adr = rec.get('adr20Pct')
    return x > rule['maxPct'] or (adr is not None and x >= rule['adrMultiple'] * adr)


def disciplined_records(records, rule=None):
    """Copies of the records where every skipped-chase fill is a skipped record instead of a trade. The ledger itself is untouched."""
    out = []
    for r in records:
        if r['status'] != 'replaced' and is_chase_skip(r, rule):
            c = copy.deepcopy(r)
            c.update(status='skipped', exitReason='chase-skipped', fillPrice=None, exitPrice=None, rMultiple=None,
                     daysHeld=None, chased=None, chaseSkipped=True)
            out.append(c)
        else:
            out.append(r)
    return out


def _stat(recs):
    """One bucket. Percentages are withheld (None + tooSmall) below MIN_N closed trades."""
    closed = [r for r in recs if r['status'] in CLOSED]
    resolved_filled = [r for r in recs if r.get('fillPrice') is not None]
    denom = len(resolved_filled) + sum(1 for r in recs if r['status'] in ('invalidated', 'expired'))
    wins = [r for r in closed if r['exitReason'] == 'target' or r['exitPrice'] > r['fillPrice']]
    rs = [r['rMultiple'] for r in closed]
    win_rs = [r['rMultiple'] for r in wins]
    loss_rs = [r['rMultiple'] for r in closed if r not in wins]
    small = len(closed) < MIN_N
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    return {
        'picks': len(recs), 'sets': len({r['publishedAt'] for r in recs}), 'closed': len(closed),
        'closedSets': len({r['publishedAt'] for r in closed}), 'open': sum(1 for r in recs if r['status'] in NON_TERMINAL),
        'trades': len(resolved_filled),
        'skipped': sum(1 for r in recs if r['status'] == 'skipped' and not r.get('chaseSkipped')),
        'chaseSkipped': sum(1 for r in recs if r.get('chaseSkipped')),
        'invalidated': sum(1 for r in recs if r['status'] == 'invalidated'),
        'expired': sum(1 for r in recs if r['status'] == 'expired'),
        'triggerRate': None if (small or not denom) else len(resolved_filled) / denom,
        'winRate': None if small else (len(wins) / len(closed)),
        'avgR': None if small else avg(rs), 'avgWinR': None if small else avg(win_rs),
        'avgLossR': None if small else avg(loss_rs),
        'expectancy': None if small else avg(rs),
        'avgDaysHeld': None if small else avg([r['daysHeld'] for r in closed]),
        'tooSmall': small,
        'label': f'n too small (<{MIN_N})' if small else None,
        'raw': {'wins': len(wins), 'triggerDenominator': denom, 'filled': len(resolved_filled)},
    }


def summarize(records):
    """Stats over the literal ledger. Records published with invalid levels (stop >= basis close: a scan defect, not a market
    outcome) are excluded here and reported on their own line by summarize_both."""
    recs = [r for r in records if r['status'] != 'replaced' and not r.get('levelsInvalidAtPublish')]

    def split(keyf, order=None):
        groups = {}
        for r in recs:
            groups.setdefault(keyf(r), []).append(r)
        keys = sorted(groups, key=lambda k: (order.index(k) if order and k in order else 99, str(k)))
        return {str(k): _stat(groups[k]) for k in keys}
    return {
        'overall': _stat(recs),
        'byReadinessBand': split(band, ['<50', '50-69', '>=70', 'unscored']),
        'byRegime': split(lambda r: r.get('regimeLabel') or 'unknown', ['RISK-ON', 'MIXED', 'RISK-OFF', 'unknown']),
        'bySector': split(lambda r: r.get('sector') or 'unknown'),
        'byScoringVersion': split(lambda r: r.get('scoringVersion') or 'unscored'),
        'byFill': split(lambda r: 'no fill' if r.get('chased') is None else ('chased fill' if r['chased'] else 'clean fill'),
                        ['clean fill', 'chased fill', 'no fill']),
        'byStopDistance': split(lambda r: 'tight stop' if r.get('tightStopAtPublish') else 'normal stop', ['tight stop', 'normal stop']),
        'minN': MIN_N,
    }


def summarize_both(records, rule=None):
    """{'literal': summarize(ledger), 'disciplined': summarize(ledger with skipped-chase fills removed), 'rule': ...}."""
    rule = rule or CHASE_RULE
    bad = [r for r in records if r['status'] != 'replaced' and r.get('levelsInvalidAtPublish')]
    return {'literal': summarize(records), 'disciplined': summarize(disciplined_records(records, rule)), 'rule': dict(rule),
            'invalidLevels': {'n': len(bad), 'ids': [r['id'] for r in bad],
                              'reasons': {r['id']: r.get('levelsInvalidReason') for r in bad}}}


def chip(rec, last_session):
    """Status chip for a pick card: waiting | awaiting | live | stopped | target | expired | invalidated | time-exit | skipped | replaced."""
    s = rec['status']
    if s in NON_TERMINAL and rec['startSession'] <= last_session and (rec.get('evaluatedThrough') or '') < last_session:
        return 'awaiting'
    return {'pending': 'waiting', 'triggered': 'live'}.get(s, s)


def open_r(rec):
    if rec['status'] == 'triggered' and rec.get('lastClose') is not None:
        return round((rec['lastClose'] - rec['fillPrice']) / (rec['entry'] - rec['stop']), 2)
    return None


def build_view(ledger, last_session, problems=None, pick_basis=None):
    """Everything the page needs: records (also a replicated ledger copy), stats (literal + disciplined), tables, per-pick chips."""
    recs = ledger['records']
    both = summarize_both(recs)
    picks = {}
    for r in recs:                              # the record behind each card currently on the page: same ticker + basis session
        if pick_basis and r['basisSession'] == pick_basis:
            picks[r['ticker']] = {'id': r['id'], 'chip': chip(r, last_session), 'openR': open_r(r), 'status': r['status'],
                                  'fillPrice': r.get('fillPrice'), 'chased': r.get('chased'),
                                  'chaseSkip': is_chase_skip(r), 'series': r.get('series'),
                                  'levelsInvalid': bool(r.get('levelsInvalidAtPublish')), 'tightStop': bool(r.get('tightStopAtPublish'))}
    closed = sorted((r for r in recs if r['status'] in CLOSED), key=lambda r: (r['exitDate'], r['id']), reverse=True)[:20]

    def row(r):
        return dict(id=r['id'], ticker=r['ticker'], publishedAt=r['publishedAt'], approx=bool(r.get('publishTimeApproximate')),
                    readiness=(r.get('readiness') or {}).get('composite'), chased=r.get('chased'), chaseSkip=is_chase_skip(r))
    awaiting = sorted({r['id'] for r in recs if chip(r, last_session) == 'awaiting'})
    return {
        'schemaVersion': SCHEMA_VERSION, 'lastSession': last_session, 'records': recs, 'stats': both,
        'evaluatedThrough': max([r['evaluatedThrough'] for r in recs if r.get('evaluatedThrough')] or [None]),
        'picks': picks, 'awaitingData': awaiting,
        'open': [dict(row(r), status=chip(r, last_session), entry=r['entry'], stop=r['stop'], target=r['target'],
                      fillPrice=r.get('fillPrice'), lastClose=r.get('lastClose'), openR=open_r(r),
                      evaluatedThrough=r.get('evaluatedThrough')) for r in recs if r['status'] in NON_TERMINAL],
        'closed20': [dict(row(r), exitDate=r['exitDate'], result=r['status'], r=r['rMultiple']) for r in closed],
        'problems': problems or [],
    }


def tracker_health(view, warnings=()):
    """(section, messages) for the page's health block: awaiting-data is an expected state (shown like PINNED), problems are warnings."""
    msgs = list(view.get('problems') or []) + list(warnings or [])
    n = len(view.get('awaitingData') or [])
    evald = [r['evaluatedThrough'] for r in view['records'] if r.get('evaluatedThrough')]
    sec = {'status': 'pinned' if n else 'ok', 'asOf': max(evald) if evald else view.get('lastSession'),
           'source': 'pick ledger (data/picks.json)',
           'msg': (f'{n} record(s) awaiting OHLC - outcomes refresh in an interactive session (the cloud routine has no price history).'
                   if n else '')}
    return sec, msgs


def load_merged(ledger_path, previous_block=None):
    """The git ledger merged with the copy embedded in the previous published page. Returns (ledger, conflicts)."""
    ledger = load_ledger(ledger_path)
    conflicts = []
    prev = ((previous_block or {}).get('tracker') or {}).get('records')
    if prev:
        ledger, conflicts = merge_ledgers(ledger, {'schemaVersion': SCHEMA_VERSION, 'records': prev})
    return ledger, conflicts


# ───────────────────────────── CLI ─────────────────────────────
def _block_from_html(path):
    h = pathlib.Path(path).read_text(encoding='utf-8')
    m = re.search(r'<script id="brief-data" type="application/json">(.*?)</script>', h, re.S)
    return json.loads(m.group(1)) if m else None


def cmd_update(a):
    now = pdt(a.now) if a.now else dt.datetime.now(mt.UTC)
    last = mt.last_completed_session(now.astimezone(mt.UTC)).isoformat()
    prev = _block_from_html(a.previous_html) if a.previous_html and pathlib.Path(a.previous_html).exists() else None
    ledger, conflicts = load_merged(a.ledger, prev)
    problems = [f'ledger copy conflict: {c}' for c in conflicts]
    report = []
    data = json.loads(pathlib.Path(a.data).read_text(encoding='utf-8')) if a.data and pathlib.Path(a.data).exists() else {}
    pub_at = a.published_at or now.isoformat()
    ohlc = {}
    if a.ohlc and pathlib.Path(a.ohlc).exists():
        ohlc = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8')).get('tickers', {})
    if data.get('picks'):
        report += ingest(ledger, data, pub_at, ohlc=ohlc)
    elif prev and prev.get('picks'):
        report += ingest(ledger, prev, pub_at, source='previous published page', ohlc=ohlc)
    rep = evaluate(ledger, ohlc, last)
    for tk, ps in rep['excluded'].items():
        problems.append(f'{tk}: OHLC excluded/warned - ' + '; '.join(ps))
    problems += [f'conflict: {c}' for c in rep['conflicts']]
    save_ledger(a.ledger, ledger)
    basis = parse_pick_window_end((data if data.get('picks') else (prev or {})).get('pickWindow'))
    view = build_view(ledger, last, problems, pick_basis=basis)
    if a.view:
        p = pathlib.Path(a.view)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(view, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'lastSession': last, 'ingest': report, 'updated': rep['updated'], 'awaiting': view['awaitingData'],
                      'problems': problems}, indent=1, ensure_ascii=False))
    return 0


def cmd_closes(a):
    rows = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8')).get('tickers', {}).get(a.ticker, {}).get('rows', [])
    closes, probs = build_closes(rows, a.upto, a.n)
    print(json.dumps({'ticker': a.ticker, 'upto': a.upto, 'closes': closes, 'problems': probs}, ensure_ascii=False))
    return 0 if closes else 2


def cmd_score(a):
    """Mechanical pick metrics from validated OHLC: the scan uses this instead of doing the arithmetic by hand."""
    rows = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8')).get('tickers', {}).get(a.ticker, {}).get('rows', [])
    closes, probs = build_closes(rows, a.basis, a.n)
    if closes is None:
        print(json.dumps({'ticker': a.ticker, 'basis': a.basis, 'ok': False, 'problems': probs}, ensure_ascii=False))
        return 2
    srt = [r for r in sort_rows(rows) if str(r.get('date')) <= a.basis]
    start = mt.trading_days_back(pdate(a.basis), a.n)[-1].isoformat()
    _, _, win = validate_rows(srt, start, a.basis)
    rd = readiness(win, a.version)
    atr = atr14(srt[-14 - 1:] if len(srt) > 14 else srt)
    out = {'ticker': a.ticker, 'basis': a.basis, 'ok': True, 'window': a.n, 'scoringVersion': a.version or SCORING_VERSION,
           'closes': closes, 'pivot': rd['pivot'], 'timeInBaseDays': rd['timeInBaseDays'],
           'readiness': {k: rd[k] for k in ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')},
           'atr14': atr, 'adr20Pct': adr20_pct(srt, a.basis), 'lastClose': win[-1]['close']}
    if a.stop is not None:
        out['stopCheck'] = stop_check(a.stop, win[-1]['close'], atr)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if (a.stop is None or out['stopCheck']['ok']) else 3


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    u = sub.add_parser('update')
    u.add_argument('--ledger', default='data/picks.json')
    u.add_argument('--data', default='data/brief-data.json')
    u.add_argument('--previous-html', default=None)
    u.add_argument('--ohlc', default=None)
    u.add_argument('--published-at', default=None)
    u.add_argument('--now', default=None)
    u.add_argument('--view', default=None)
    c = sub.add_parser('closes', help='print a sorted, validated closes array for a card (the only sanctioned way to build one)')
    c.add_argument('--ohlc', required=True)
    c.add_argument('--ticker', required=True)
    c.add_argument('--upto', required=True, help='last session of the window (the basis session)')
    c.add_argument('--n', type=int, default=SERIES_SESSIONS)
    sc = sub.add_parser('score', help='readiness (current formula), pivot, ATR14, ADR20, validated closes and the stop-floor check for one pick')
    sc.add_argument('--ohlc', required=True)
    sc.add_argument('--ticker', required=True)
    sc.add_argument('--basis', required=True, help='last session of the window (the basis session)')
    sc.add_argument('--n', type=int, default=SERIES_SESSIONS)
    sc.add_argument('--stop', type=float, default=None, help='check this stop against the ATR floor (exit 3 = reject the pick)')
    sc.add_argument('--version', default=None, help='scoring version (default: current)')
    a = ap.parse_args(argv)
    return {'update': cmd_update, 'closes': cmd_closes, 'score': cmd_score}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
