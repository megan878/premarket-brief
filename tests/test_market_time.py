import datetime as dt, os, sys, zoneinfo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import market_time as mt

U = dt.timezone.utc
def at(s): return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
def g(routine, s): return mt.guard(routine, at(s))[0]

# 1. summer (EDT): open = 21:30 HKT, so the 13:25 UTC firing runs and the 14:25 one skips
assert g('open-update', '2026-09-21T13:25:00Z') is True
assert g('open-update', '2026-09-21T14:25:00Z') is False
assert mt.info(at('2026-09-21T13:25:00Z'))['usOpenHKT'] == '21:30'

# 2. winter (EST): open = 22:30 HKT, so it flips
assert g('open-update', '2026-11-02T13:25:00Z') is False
assert g('open-update', '2026-11-02T14:25:00Z') is True
assert mt.info(at('2026-11-02T14:25:00Z'))['usOpenHKT'] == '22:30'

# 3. the DST switch itself: Sun 1 Nov 2026 06:00 UTC (02:00 EDT -> 01:00 EST)
assert mt.ny_offset_hours(at('2026-11-01T05:59:00Z')) == -4
assert mt.ny_offset_hours(at('2026-11-01T06:00:00Z')) == -5
# Monday 2 Nov is the first winter session; Friday 30 Oct was the last summer one
assert g('open-update', '2026-10-30T13:25:00Z') is True
# spring: DST starts Sun 14 Mar 2027 07:00 UTC
assert mt.ny_offset_hours(at('2027-03-14T06:59:00Z')) == -5
assert mt.ny_offset_hours(at('2027-03-14T07:00:00Z')) == -4
assert g('open-update', '2027-03-15T13:25:00Z') is True and g('open-update', '2027-03-12T14:25:00Z') is True

# 4. weekends and holidays skip both routines
assert g('brief', '2026-09-19T12:00:00Z') is False and g('brief', '2026-09-20T12:00:00Z') is False
assert g('open-update', '2026-09-07T13:25:00Z') is False       # Labor Day
assert g('brief', '2026-11-26T12:00:00Z') is False             # Thanksgiving
assert g('open-update', '2026-11-27T14:25:00Z') is True        # early-close day still opens at 09:30 ET

# 5. brief slot: 20:00 HKT = 12:00 UTC is before the open in both seasons
assert g('brief', '2026-09-22T12:00:00Z') is True and g('brief', '2026-11-03T12:00:00Z') is True
assert g('brief', '2026-09-22T00:00:00Z') is False             # 08:00 HKT is not a brief slot

# 6. last completed session
assert mt.last_completed_session(at('2026-09-21T12:00:00Z')).isoformat() == '2026-09-18'   # Monday pre-open -> Friday
assert mt.last_completed_session(at('2026-09-22T21:00:00Z')).isoformat() == '2026-09-22'   # Tuesday after the bell
assert mt.last_completed_session(at('2026-09-08T12:00:00Z')).isoformat() == '2026-09-04'   # after Labor Day
assert [d.isoformat() for d in mt.trading_days_back(dt.date(2026, 9, 18), 5)] == ['2026-09-18', '2026-09-17', '2026-09-16', '2026-09-15', '2026-09-14']

# 7. my DST rule equals the real tz database for every hour of 2026-2027
ny = zoneinfo.ZoneInfo('America/New_York')
t, end, bad = at('2026-01-01T00:00:00Z'), at('2028-01-01T00:00:00Z'), 0
while t < end:
    if t.astimezone(ny).utcoffset() != dt.timedelta(hours=mt.ny_offset_hours(t)):
        bad += 1
    t += dt.timedelta(hours=1)
assert bad == 0, f'{bad} hours disagree with zoneinfo'

# 8. holiday table equals FMP's fully-closed list (fetched 21 Sep 2026)
FMP_CLOSED = ['2027-12-31', '2027-12-24', '2027-11-25', '2027-09-06', '2027-07-05', '2027-06-18', '2027-05-31', '2027-03-26',
              '2027-02-15', '2027-01-18', '2027-01-01', '2026-12-25', '2026-11-26', '2026-09-07', '2026-07-03', '2026-06-19',
              '2026-05-25', '2026-04-03', '2026-02-16', '2026-01-19']
assert {dt.date.fromisoformat(x) for x in FMP_CLOSED} | {dt.date(2026, 1, 1)} == mt.NYSE_CLOSED

# 9. beyond the holiday table the guard runs anyway but says so
ok, why = mt.guard('open-update', at('2028-03-01T13:25:00Z'))
assert ok and 'holiday table' in why
print('market_time: all checks passed')
