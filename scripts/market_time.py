"""US-market clock helpers and the run/skip guard for both routines.

Cloud cron is UTC-only. Hong Kong never changes its clock, but New York does, so the US open is
21:30 HKT while US daylight time is in force (13:30 UTC) and 22:30 HKT in winter (14:30 UTC).
The open-update routine targets 10 minutes AFTER the open (21:40 / 22:40 HKT) so opening prints
have usually posted by the time it fetches. Instead of trusting one cron time, it fires at BOTH
candidate UTC times and this guard decides whether *this* firing is the right one.

    python market_time.py guard brief          -> exit 0 = run, exit 10 = skip
    python market_time.py guard open-update    -> exit 0 = run, exit 10 = skip
    python market_time.py info                 -> today's clock facts as JSON

Set MARKET_NOW_UTC=2026-09-21T13:40:00Z to test any moment.
"""
import datetime as dt, json, os, sys

UTC = dt.timezone.utc
HKT = dt.timezone(dt.timedelta(hours=8), 'HKT')

# NYSE full-day closures (cross-checked against FMP marketHours/holidays-by-exchange, Sep 2026).
NYSE_CLOSED = {dt.date.fromisoformat(x) for x in [
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-11-26', '2026-12-25',
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05',
    '2027-09-06', '2027-11-25', '2027-12-24', '2027-12-31',
]}
LAST_HOLIDAY_YEAR = 2027   # beyond this the table is unknown -> guard warns instead of guessing
EARLY_CLOSE = {dt.date.fromisoformat(x) for x in ['2026-11-27', '2026-12-24']}  # 13:00 ET close; open unchanged


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
    return d + dt.timedelta(weeks=n - 1)


def ny_offset_hours(now_utc):
    """-4 during US daylight time, else -5. DST: 2nd Sunday of March 02:00 local -> 1st Sunday of Nov 02:00 local."""
    y = now_utc.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7, 0), UTC)    # 02:00 EST == 07:00 UTC
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6, 0), UTC)     # 02:00 EDT == 06:00 UTC
    return -4 if start <= now_utc < end else -5


def to_et(now_utc):
    return now_utc.astimezone(dt.timezone(dt.timedelta(hours=ny_offset_hours(now_utc))))


def is_trading_day(d):
    return d.weekday() < 5 and d not in NYSE_CLOSED


def prev_session(d):
    d = d - dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def us_open_utc(d):
    """09:30 New York on date d, as UTC."""
    noon = dt.datetime.combine(d, dt.time(12, 0), UTC)
    off = ny_offset_hours(noon)
    return dt.datetime.combine(d, dt.time(9, 30), dt.timezone(dt.timedelta(hours=off))).astimezone(UTC)


def close_utc(d):
    """16:00 New York (13:00 on early-close days) on date d, as UTC."""
    noon = dt.datetime.combine(d, dt.time(12, 0), UTC)
    tz = dt.timezone(dt.timedelta(hours=ny_offset_hours(noon)))
    return dt.datetime.combine(d, dt.time(13 if d in EARLY_CLOSE else 16, 0), tz).astimezone(UTC)


def last_completed_session(now_utc):
    """Most recent trading day whose closing bell has already rung."""
    d = to_et(now_utc).date()
    if is_trading_day(d) and now_utc >= close_utc(d):
        return d
    return prev_session(d)


def trading_days_back(d, n):
    """The n trading days ending at d (inclusive if d is a session), newest first."""
    out, cur = [], d
    while len(out) < n:
        if is_trading_day(cur):
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return out


def info(now_utc):
    et = to_et(now_utc)
    d = et.date()
    session = d if is_trading_day(d) else None
    open_utc = us_open_utc(d)
    return {
        'nowUTC': now_utc.isoformat(), 'nowHKT': now_utc.astimezone(HKT).isoformat(),
        'nowET': et.isoformat(), 'etDate': d.isoformat(), 'etOffset': ny_offset_hours(now_utc),
        'isDST': ny_offset_hours(now_utc) == -4, 'isTradingDay': session is not None,
        'usOpenUTC': open_utc.isoformat(), 'usOpenHKT': open_utc.astimezone(HKT).strftime('%H:%M'),
        'minutesToOpen': round((open_utc - now_utc).total_seconds() / 60, 1),
        'lastSession': last_completed_session(now_utc).isoformat(),
        'earlyClose': d in EARLY_CLOSE,
        'holidayTableCovers': d.year <= LAST_HOLIDAY_YEAR,
    }


def guard(routine, now_utc):
    """Return (run: bool, reason: str)."""
    i = info(now_utc)
    if not i['holidayTableCovers']:
        return True, f"holiday table ends {LAST_HOLIDAY_YEAR}; running anyway - update NYSE_CLOSED"
    if not i['isTradingDay']:
        return False, f"US market closed on {i['etDate']} (weekend/holiday)"
    m = i['minutesToOpen']
    if routine == 'open-update':
        # target: 10 minutes after the open. Fires at 13:40 UTC and 14:40 UTC; only one is in the window.
        if -15 <= m <= 20:
            return True, f"{m:+.0f} min to the US open (ET offset {i['etOffset']}h)"
        return False, f"wrong DST slot: open is {i['usOpenHKT']} HKT, {m:.0f} min away"
    if routine == 'brief':
        # 20:00 HKT is 07:00/08:00 ET, pre-market of a session that has not opened yet.
        if 0 < m <= 4 * 60:
            return True, f"{m:.0f} min before the US open"
        return False, f"not a pre-open slot ({m:.0f} min to open)"
    raise SystemExit(f'unknown routine {routine}')


def now_utc():
    v = os.environ.get('MARKET_NOW_UTC')
    return dt.datetime.fromisoformat(v.replace('Z', '+00:00')) if v else dt.datetime.now(UTC)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'info'
    n = now_utc()
    if cmd == 'info':
        print(json.dumps(info(n), indent=1))
    elif cmd == 'guard':
        ok, why = guard(sys.argv[2], n)
        print(json.dumps({'run': ok, 'reason': why, **{k: info(n)[k] for k in ('nowHKT', 'usOpenHKT', 'lastSession')}}))
        sys.exit(0 if ok else 10)
