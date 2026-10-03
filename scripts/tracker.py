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
import argparse, copy, datetime as dt, json, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import market_time as mt  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

SCHEMA_VERSION = 1
SCORING_VERSION = 'v1'          # bump together with the formulas in CLAUDE.md "Readiness scoring"
EXPIRY_SESSIONS = 10            # pending -> expired after this many evaluable sessions without a trigger
MAX_HOLD_SESSIONS = 20          # trigger session = session 1; time-exit at the close of session 20
MIN_N = 20                      # fewer closed trades than this -> "n too small", never a percentage
HKT = dt.timezone(dt.timedelta(hours=8), 'HKT')

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
    """'28 Oct' -> the first such calendar date on/after the publish date. None if unparseable."""
    m = re.fullmatch(r'\s*(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s*', s or '')
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
        if d < from_date:
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


def build_record(pick, block, published_at, basis, rec_id):
    zl, zh = parse_zone(pick.get('entryZone'))
    rd = pick.get('readiness') if isinstance(pick.get('readiness'), dict) and num(pick['readiness'].get('composite')) else None
    label, score, total = regime_from_block(block)
    pub = pdt(published_at)
    nxt = parse_next_earnings(((block.get('bg') or {}).get(pick['tk']) or {}).get('next'), hkt_date(pub))
    return {
        'id': rec_id, 'publishedAt': pub.isoformat(), 'basisSession': basis, 'startSession': first_session_after(pub),
        'ticker': pick['tk'], 'sector': pick.get('sector'), 'industry': pick.get('industry'), 'setup': pick.get('setup'),
        'pivot': pick.get('pivot'), 'entryZoneLow': zl, 'entryZoneHigh': zh, 'entry': pick['entry'],
        'stop': pick['stop'], 'target': pick['target'],
        'readiness': ({k: rd[k] for k in ('composite', 'tightness', 'proximity', 'volumeDryUp', 'timeInBase')} if rd else None),
        'scoringVersion': SCORING_VERSION if rd else None,
        'regimeLabel': label, 'regimeScore': score, 'regimeChecks': total, 'nextEarningsDate': nxt,
        'status': 'pending', 'triggerDate': None, 'fillPrice': None, 'chased': None, 'exitDate': None,
        'exitPrice': None, 'exitReason': None, 'rMultiple': None, 'daysHeld': None, 'lastClose': None,
        'evaluatedThrough': None, 'repickDates': [], 'evidence': {}, 'notes': [],
    }


def ingest(ledger, block, published_at, source='fresh'):
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
        new = build_record(pick, block, pub.isoformat(), basis, new_id)
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
            **{k: int(round(v)) for k, v in comps.items()}, 'composite': int(round(sum(comps.values())))}


def closes_match(window, published_closes, tol=0.011):
    """Compare the tail of the fetched window to the closes the page published (an independent cross-check)."""
    mine = [r['close'] for r in window][-len(published_closes):]
    if len(mine) != len(published_closes):
        return [f'length {len(mine)} vs {len(published_closes)}']
    return [f'#{i}: {a} vs {b}' for i, (a, b) in enumerate(zip(mine, published_closes)) if abs(a - b) > tol]


# ───────────────────────────── stats ─────────────────────────────
def band(rec):
    rd = rec.get('readiness')
    if not rd:
        return 'unscored'
    c = rd['composite']
    return '<50' if c < 50 else '50-69' if c < 70 else '>=70'


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
        'skipped': sum(1 for r in recs if r['status'] == 'skipped'),
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
    recs = [r for r in records if r['status'] != 'replaced']
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
        'minN': MIN_N,
    }


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


def build_view(ledger, last_session, problems=None):
    recs = ledger['records']
    st = summarize(recs)
    chips = {}
    for r in recs:                              # latest record per ticker drives the card chip
        chips[r['ticker']] = {'chip': chip(r, last_session), 'openR': open_r(r), 'id': r['id'], 'status': r['status']}
    closed = sorted((r for r in recs if r['status'] in CLOSED), key=lambda r: (r['exitDate'], r['id']), reverse=True)[:20]
    return {
        'schemaVersion': SCHEMA_VERSION, 'lastSession': last_session, 'records': recs, 'stats': st, 'chips': chips,
        'open': [dict(id=r['id'], ticker=r['ticker'], publishedAt=r['publishedAt'], status=chip(r, last_session),
                      readiness=(r.get('readiness') or {}).get('composite'), entry=r['entry'], stop=r['stop'],
                      target=r['target'], fillPrice=r.get('fillPrice'), lastClose=r.get('lastClose'), openR=open_r(r),
                      evaluatedThrough=r.get('evaluatedThrough')) for r in recs if r['status'] in NON_TERMINAL],
        'closed20': [dict(id=r['id'], ticker=r['ticker'], publishedAt=r['publishedAt'], exitDate=r['exitDate'],
                          readiness=(r.get('readiness') or {}).get('composite'), result=r['status'], r=r['rMultiple'])
                     for r in closed],
        'problems': problems or [],
    }


# ───────────────────────────── CLI ─────────────────────────────
def _block_from_html(path):
    h = pathlib.Path(path).read_text(encoding='utf-8')
    m = re.search(r'<script id="brief-data" type="application/json">(.*?)</script>', h, re.S)
    return json.loads(m.group(1)) if m else None


def cmd_update(a):
    now = pdt(a.now) if a.now else dt.datetime.now(mt.UTC)
    last = mt.last_completed_session(now.astimezone(mt.UTC)).isoformat()
    ledger = load_ledger(a.ledger)
    problems, report = [], []
    prev = _block_from_html(a.previous_html) if a.previous_html and pathlib.Path(a.previous_html).exists() else None
    if prev and (prev.get('tracker') or {}).get('records'):
        ledger, conflicts = merge_ledgers(ledger, {'schemaVersion': SCHEMA_VERSION, 'records': prev['tracker']['records']})
        problems += [f'ledger copy conflict: {c}' for c in conflicts]
    data = json.loads(pathlib.Path(a.data).read_text(encoding='utf-8')) if a.data and pathlib.Path(a.data).exists() else {}
    pub_at = a.published_at or now.isoformat()
    if data.get('picks'):
        report += ingest(ledger, data, pub_at)
    elif prev and prev.get('picks'):
        report += ingest(ledger, prev, pub_at, source='previous published page')
    ohlc = {}
    if a.ohlc and pathlib.Path(a.ohlc).exists():
        ohlc = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8')).get('tickers', {})
    rep = evaluate(ledger, ohlc, last)
    for tk, ps in rep['excluded'].items():
        problems.append(f'{tk}: OHLC excluded/warned - ' + '; '.join(ps))
    problems += [f'conflict: {c}' for c in rep['conflicts']]
    save_ledger(a.ledger, ledger)
    view = build_view(ledger, last, problems)
    view['awaitingData'] = sorted(set(rep['awaiting']))
    if a.view:
        p = pathlib.Path(a.view)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(view, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'lastSession': last, 'ingest': report, 'updated': rep['updated'], 'awaiting': view['awaitingData'],
                      'problems': problems}, indent=1, ensure_ascii=False))
    return 0


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
    a = ap.parse_args(argv)
    return cmd_update(a)


if __name__ == '__main__':
    sys.exit(main())
