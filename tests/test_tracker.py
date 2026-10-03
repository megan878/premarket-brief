"""Unit tests for scripts/tracker.py. Plain stdlib: `python tests/test_tracker.py`. Synthetic fixtures only - this file
never reads data/picks.json or data/brief-data.json (the live files are overwritten by routines)."""
import copy, datetime as dt, json, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import tracker as T  # noqa: E402

SESS = T.sessions_between('2026-09-08', '2026-11-30')   # 7 Sep is Labor Day, so the first session is Tue 8 Sep
START = SESS[0]


def rec(entry=100.0, stop=95.0, target=110.0, zone_hi=101.0, start=START, **kw):
    r = {'id': 'x', 'ticker': 'TST', 'publishedAt': '2026-09-07T12:00:00+08:00', 'basisSession': '2026-09-04',
         'startSession': start, 'entry': entry, 'stop': stop, 'target': target, 'entryZoneHigh': zone_hi,
         'entryZoneLow': entry, 'status': 'pending', 'repickDates': [], 'notes': [], 'evidence': {}}
    r.update(kw)
    return r


def rows(*specs, start=0):
    """specs are (open, high, low, close); assigned to consecutive sessions from SESS[start]."""
    return [{'date': SESS[start + i], 'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1000}
            for i, (o, h, l, c) in enumerate(specs)]


FLAT = (98.0, 99.0, 97.0, 98.0)          # never triggers (high<entry), never stops (low>stop)
TRIG = (99.0, 101.0, 98.0, 100.0)        # opens below entry, trades through it, stays above stop
HOLD = (100.0, 102.0, 98.0, 100.0)       # a held-position day: no stop, no target


class Transitions(unittest.TestCase):
    def test_pending_without_trigger(self):
        o = T.replay(rec(), rows(FLAT, FLAT, FLAT))
        self.assertEqual(o['status'], 'pending')
        self.assertEqual(o['evaluatedThrough'], SESS[2])
        self.assertIsNone(o['fillPrice'])

    def test_no_rows_is_pending_with_nothing_evaluated(self):
        o = T.replay(rec(), [])
        self.assertEqual((o['status'], o['evaluatedThrough']), ('pending', None))

    def test_trigger_fills_at_entry(self):
        o = T.replay(rec(), rows(FLAT, TRIG))
        self.assertEqual((o['status'], o['triggerDate'], o['fillPrice'], o['chased']), ('triggered', SESS[1], 100.0, False))
        self.assertEqual(o['lastClose'], 100.0)

    def test_gap_up_fills_at_the_open_and_is_chased(self):
        o = T.replay(rec(), rows((103.0, 104.0, 102.0, 103.5)))
        self.assertEqual((o['status'], o['fillPrice'], o['chased']), ('triggered', 103.0, True))

    def test_open_inside_the_zone_fills_at_open_and_is_not_chased(self):
        o = T.replay(rec(), rows((100.6, 101.5, 100.0, 101.0)))
        self.assertEqual((o['fillPrice'], o['chased']), (100.6, False))

    def test_invalidated_when_open_is_at_or_below_stop(self):
        for op in (94.0, 95.0):
            o = T.replay(rec(), rows((op, 101.0, 93.0, 99.0)))      # even though it later trades through entry
            self.assertEqual((o['status'], o['exitReason']), ('invalidated', 'open-at-or-below-stop'), op)
            self.assertIsNone(o['fillPrice'])

    def test_invalidated_when_low_hits_stop_before_any_trigger(self):
        o = T.replay(rec(), rows((97.0, 99.0, 94.9, 96.0)))
        self.assertEqual((o['status'], o['exitReason']), ('invalidated', 'stop-before-trigger'))

    def test_same_session_entry_and_stop_opened_between_is_triggered_then_stopped(self):
        o = T.replay(rec(), rows((98.0, 101.0, 94.0, 96.0)))        # stop < open < entry
        self.assertEqual((o['status'], o['fillPrice'], o['exitPrice'], o['rMultiple'], o['daysHeld']),
                         ('stopped', 100.0, 95.0, -1.0, 1))

    def test_same_session_entry_and_stop_opened_above_entry_fills_at_open(self):
        o = T.replay(rec(), rows((102.0, 103.0, 94.0, 96.0)))
        self.assertEqual((o['status'], o['fillPrice'], o['exitPrice'], o['rMultiple']), ('stopped', 102.0, 95.0, -1.4))
        self.assertTrue(o['chased'])

    def test_expired_after_ten_sessions_without_trigger(self):
        o = T.replay(rec(), rows(*[FLAT] * 10))
        self.assertEqual((o['status'], o['exitDate']), ('expired', SESS[9]))

    def test_nine_flat_sessions_is_still_pending(self):
        self.assertEqual(T.replay(rec(), rows(*[FLAT] * 9))['status'], 'pending')

    def test_trigger_on_the_tenth_session_counts(self):
        o = T.replay(rec(), rows(*[FLAT] * 9, TRIG))
        self.assertEqual((o['status'], o['triggerDate']), ('triggered', SESS[9]))

    def test_trigger_on_the_eleventh_session_is_too_late(self):
        o = T.replay(rec(), rows(*[FLAT] * 10, TRIG))
        self.assertEqual((o['status'], o['exitDate']), ('expired', SESS[9]))
        self.assertIsNone(o['triggerDate'])

    def test_gapped_past_target_is_skipped_and_has_no_r(self):
        o = T.replay(rec(), rows((111.0, 112.0, 110.5, 111.5)))
        self.assertEqual((o['status'], o['exitReason'], o['rMultiple'], o['fillPrice']), ('skipped', 'gapped-past-target', None, None))

    def test_target_hit_on_a_later_session(self):
        o = T.replay(rec(), rows(TRIG, (101.0, 110.5, 100.0, 109.0)))
        self.assertEqual((o['status'], o['exitPrice'], o['rMultiple'], o['daysHeld'], o['exitDate']),
                         ('target', 110.0, 2.0, 2, SESS[1]))

    def test_target_hit_on_the_trigger_session(self):
        o = T.replay(rec(), rows((99.0, 111.0, 98.0, 110.0)))
        self.assertEqual((o['status'], o['rMultiple'], o['daysHeld']), ('target', 2.0, 1))

    def test_stopped_on_a_later_session_exits_at_the_stop(self):
        o = T.replay(rec(), rows(TRIG, (96.0, 97.0, 94.5, 95.5)))
        self.assertEqual((o['status'], o['exitPrice'], o['rMultiple']), ('stopped', 95.0, -1.0))

    def test_gap_down_stop_exits_at_the_open(self):
        o = T.replay(rec(), rows(TRIG, (92.0, 93.0, 90.0, 91.0)))
        self.assertEqual((o['exitPrice'], o['rMultiple']), (92.0, -1.6))

    def test_stop_and_target_in_the_same_session_assumes_stop_first(self):
        o = T.replay(rec(), rows(TRIG, (100.0, 111.0, 94.0, 105.0)))
        self.assertEqual((o['status'], o['exitPrice']), ('stopped', 95.0))

    def test_stop_and_target_on_the_trigger_session_assumes_stop_first(self):
        o = T.replay(rec(), rows((99.0, 111.0, 94.0, 100.0)))
        self.assertEqual(o['status'], 'stopped')

    def test_time_exit_at_the_close_of_session_20(self):
        o = T.replay(rec(), rows(TRIG, *[HOLD] * 18, (100.0, 102.0, 98.0, 100.5)))
        self.assertEqual((o['status'], o['exitPrice'], o['daysHeld'], o['exitDate']), ('time-exit', 100.5, 20, SESS[19]))
        self.assertEqual(o['rMultiple'], 0.1)

    def test_nineteen_held_sessions_is_still_live(self):
        o = T.replay(rec(), rows(TRIG, *[HOLD] * 18))
        self.assertEqual(o['status'], 'triggered')

    def test_live_trade_keeps_the_last_close(self):
        o = T.replay(rec(), rows(TRIG, (100.0, 103.0, 99.0, 102.0)))
        self.assertEqual((o['status'], o['lastClose'], o['evaluatedThrough']), ('triggered', 102.0, SESS[1]))
        self.assertEqual(T.open_r({**rec(), **o}), 0.4)

    def test_rows_before_the_start_session_are_ignored(self):
        early = rows((99.0, 105.0, 98.0, 104.0))                    # would trigger, but predates the pick
        o = T.replay(rec(start=SESS[1]), early + rows(FLAT, start=1))
        self.assertEqual(o['status'], 'pending')


class Idempotence(unittest.TestCase):
    def test_replay_is_deterministic_and_prefix_stable(self):
        rs = rows(FLAT, TRIG, HOLD, (100.0, 111.0, 99.0, 110.0), HOLD, HOLD)
        a, b = T.replay(rec(), rs), T.replay(rec(), rs)
        self.assertEqual(a, b)
        self.assertEqual(a['status'], 'target')
        self.assertEqual(T.replay(rec(), rs[:4]), a)                # rows after the terminal session change nothing
        self.assertEqual(a['evaluatedThrough'], a['exitDate'])

    def test_slices_converge_to_the_same_answer(self):
        rs = rows(FLAT, TRIG, HOLD, HOLD)
        full = T.replay(rec(), rs)
        for k in range(1, len(rs)):
            part = T.replay(rec(), rs[:k])
            again = T.replay(rec(), rs)
            self.assertEqual(again, full)
            self.assertIn(part['status'], ('pending', 'triggered'))

    def test_evaluate_twice_gives_an_identical_ledger(self):
        led = {'schemaVersion': 1, 'records': [rec(id='a', ticker='AAA')]}
        ohlc = {'AAA': {'rows': rows(FLAT, TRIG, HOLD)}}
        T.evaluate(led, ohlc, SESS[2])
        once = copy.deepcopy(led)
        T.evaluate(led, ohlc, SESS[2])
        self.assertEqual(led, once)


class Validation(unittest.TestCase):
    def ok(self, rs, last=None, fmp=None):
        return T.validate_rows(rs, START, last or rs[-1]['date'], fmp)

    def test_clean_rows_pass(self):
        ok, probs, clean = self.ok(rows(FLAT, TRIG, HOLD))
        self.assertTrue(ok, probs)
        self.assertEqual(len(clean), 3)

    def test_low_above_open_fails(self):
        bad = rows(FLAT, (99.0, 101.0, 99.5, 100.0), HOLD)
        ok, probs, _ = self.ok(bad)
        self.assertFalse(ok)
        self.assertTrue(any('out of order' in p for p in probs))

    def test_close_above_high_fails(self):
        self.assertFalse(self.ok(rows(FLAT, (99.0, 101.0, 98.0, 102.0), HOLD))[0])

    def test_dates_not_strictly_increasing_fail(self):
        rs = rows(FLAT, TRIG, HOLD)
        rs[1], rs[2] = rs[2], rs[1]
        ok, probs, _ = self.ok(rs, last=SESS[2])
        self.assertFalse(ok)
        self.assertTrue(any('strictly increasing' in p for p in probs))

    def test_duplicate_date_fails(self):
        rs = rows(FLAT, TRIG, HOLD)
        rs[2] = dict(rs[2], date=rs[1]['date'])
        self.assertFalse(self.ok(rs, last=SESS[2])[0])

    def test_missing_session_fails(self):
        rs = rows(FLAT, TRIG, HOLD)
        del rs[1]
        ok, probs, _ = self.ok(rs, last=SESS[2])
        self.assertFalse(ok)
        self.assertTrue(any('missing sessions' in p for p in probs))

    def test_weekend_row_fails(self):
        rs = rows(FLAT) + [{'date': '2026-09-12', 'open': 98, 'high': 99, 'low': 97, 'close': 98, 'volume': 1}]
        ok, probs, _ = T.validate_rows(rs, START, SESS[0])
        self.assertFalse(ok)
        self.assertTrue(any('non-session' in p for p in probs))

    def test_stale_last_date_fails(self):
        ok, probs, _ = T.validate_rows(rows(FLAT, TRIG), START, SESS[2])
        self.assertFalse(ok)

    def test_nonnumeric_row_fails(self):
        rs = rows(FLAT, TRIG)
        rs[1]['high'] = 'n/a'
        self.assertFalse(self.ok(rs)[0])

    def test_fmp_cross_check(self):
        rs = rows(FLAT, TRIG)                                       # last close 100.0
        self.assertTrue(self.ok(rs, fmp=100.2)[0])
        ok, probs, _ = self.ok(rs, fmp=100.9)                       # 0.9% apart: usable, with a warning
        self.assertTrue(ok)
        self.assertTrue(any(p.startswith('warn:') for p in probs))
        self.assertFalse(self.ok(rs, fmp=110.0)[0])                 # 9% apart: reject

    def test_failed_ticker_is_excluded_not_replayed(self):
        led = {'schemaVersion': 1, 'records': [rec(id='a', ticker='AAA')]}
        bad = rows(FLAT, (99.0, 101.0, 99.5, 100.0))
        rep = T.evaluate(led, {'AAA': {'rows': bad}}, SESS[1])
        self.assertIn('AAA', rep['excluded'])
        self.assertEqual(led['records'][0]['status'], 'pending')
        self.assertIsNone(led['records'][0].get('evaluatedThrough'))
        self.assertEqual(rep['awaiting'], ['a'])


class Sessions(unittest.TestCase):
    def fs(self, iso):
        return T.first_session_after(iso)

    def test_before_the_open_is_that_day(self):
        self.assertEqual(self.fs('2026-10-01T14:43:00+08:00'), '2026-10-01')        # 02:43 ET
        self.assertEqual(self.fs('2026-10-02T19:46:23+08:00'), '2026-10-02')        # 07:46 ET

    def test_after_the_open_is_the_next_session(self):
        self.assertEqual(self.fs('2026-10-01T22:15:33+08:00'), '2026-10-02')        # 10:15 ET, session in progress

    def test_weekend_and_holiday_roll_forward(self):
        self.assertEqual(self.fs('2026-10-03T10:00:00+08:00'), '2026-10-05')        # Saturday
        self.assertEqual(self.fs('2026-09-05T10:00:00+08:00'), '2026-09-08')        # Saturday before Labor Day
        self.assertEqual(self.fs('2026-09-04T23:00:00+08:00'), '2026-09-08')        # Friday evening, after the open

    def test_hkt_date(self):
        self.assertEqual(T.hkt_date('2026-10-01T22:15:33+08:00'), '2026-10-01')
        self.assertEqual(T.hkt_date('2026-10-01T18:00:00+00:00'), '2026-10-02')


def idx_block(**over):
    b = {'indices': [
        {'id': 'SPX', 'q': {'price': 7651.54, 'ma50': 7645.157, 'ma200': 7213.3394}},
        {'id': 'DJI', 'q': {'price': 50906.05, 'ma50': 52773.465, 'ma200': 50237.484}},
        {'id': 'RUT', 'q': {'price': 2796.864, 'ma50': 2952.2825, 'ma200': 2777.736}},
        {'id': 'VIX', 'q': {'price': 16.39, 'ma50': 15.9016, 'ma200': 18.06815}}],
        'macro': {'y10hist': [{'date': d, 'y10': y} for d, y in
                              [('09-24', 5.18), ('09-25', 5.17), ('09-28', 5.24), ('09-29', 5.26), ('09-30', 5.29)]]}}
    b.update(over)
    return b


class Regime(unittest.TestCase):
    def test_matches_the_published_page_5_of_9_mixed(self):
        self.assertEqual(T.regime_from_block(idx_block()), ('MIXED', 5, 9))

    def test_eight_checks_without_rates_history(self):
        b = idx_block()
        b['macro'] = {}
        self.assertEqual(T.regime_from_block(b), ('MIXED', 5, 8))

    def test_rates_down_passes_the_ninth_check(self):
        b = idx_block()
        b['macro']['y10hist'][-1]['y10'] = 5.10
        self.assertEqual(T.regime_from_block(b)[1:], (6, 9))

    def test_thresholds_scale_with_the_number_of_checks(self):
        b = idx_block()
        for i in b['indices']:
            q = i['q']
            if i['id'] == 'VIX':
                q.update(price=12.0, ma50=15.0)
            else:
                q.update(price=q['ma200'] * 1.2, ma50=q['ma200'] * 1.0)
        b['macro']['y10hist'][-1]['y10'] = 5.0                       # rates down -> 9/9
        self.assertEqual(T.regime_from_block(b), ('RISK-ON', 9, 9))
        b['macro'] = {}
        self.assertEqual(T.regime_from_block(b), ('RISK-ON', 8, 8))

    def test_risk_off_and_missing_inputs(self):
        b = idx_block()
        for i in b['indices']:
            q = i['q']
            q.update(price=100.0, ma50=200.0, ma200=200.0) if i['id'] != 'VIX' else q.update(price=30.0, ma50=15.0)
        self.assertEqual(T.regime_from_block(b)[0], 'RISK-OFF')
        self.assertEqual(T.regime_from_block({'indices': []}), (None, None, None))


def pick(tk='NVDA', entry=234.77, stop=223.03, target=258.25, **kw):
    p = {'tk': tk, 'name': tk, 'sector': 'Technology', 'industry': 'Semiconductors', 'setup': 'flag', 'pivot': entry - 0.01,
         'entryZone': '1,387.47 – 1,401.33' if tk == 'MPWR' else '234.77 – 237.11', 'entry': entry, 'stop': stop,
         'target': target, 'readiness': {'composite': 84, 'tightness': 21, 'proximity': 23, 'volumeDryUp': 22, 'timeInBase': 18}}
    p.update(kw)
    return p


def block(picks, window='25 trading days to 1 Oct 2026, stockanalysis.com daily data', **kw):
    b = idx_block(picks=picks, pickWindow=window, bg={'NVDA': {'next': '18 Nov'}, 'MPWR': {'next': '29 Oct'}})
    b.update(kw)
    return b


PUB = '2026-10-02T19:46:23+08:00'


class Ingest(unittest.TestCase):
    def test_new_record_fields(self):
        led = T.empty_ledger()
        acts = T.ingest(led, block([pick('MPWR', 1387.47, 1331.0, 1526.22)]), PUB)
        r = led['records'][0]
        self.assertEqual(r['id'], '2026-10-02-MPWR')
        self.assertEqual((r['entryZoneLow'], r['entryZoneHigh']), (1387.47, 1401.33))
        self.assertEqual((r['basisSession'], r['startSession']), ('2026-10-01', '2026-10-02'))
        self.assertEqual((r['regimeLabel'], r['regimeScore'], r['regimeChecks']), ('MIXED', 5, 9))
        self.assertEqual(r['nextEarningsDate'], '2026-10-29')
        self.assertEqual((r['status'], r['scoringVersion'], r['readiness']['composite']), ('pending', 'v1', 84))
        self.assertEqual(r['notes'], [])
        self.assertTrue(any('new record' in a for a in acts))

    def test_unscored_pick_has_no_scoring_version(self):
        p = pick()
        del p['readiness']
        led = T.empty_ledger()
        T.ingest(led, block([p]), PUB)
        self.assertEqual((led['records'][0]['readiness'], led['records'][0]['scoringVersion']), (None, None))

    def test_carry_forward_republish_is_a_noop(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB)
        before = copy.deepcopy(led)
        T.ingest(led, block([pick()]), '2026-10-03T20:07:00+08:00')                # same basis, next day's rebuild
        self.assertEqual(led, before)

    def test_terminal_record_is_not_reopened_by_the_carry_forward(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB)
        led['records'][0].update(status='stopped', exitDate='2026-10-05')
        T.ingest(led, block([pick()]), '2026-10-06T20:07:00+08:00')
        self.assertEqual(len(led['records']), 1)

    def test_repick_of_a_pending_pick_replaces_it(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB)
        T.ingest(led, block([pick(entry=240.0, stop=228.0, target=264.0)], window='25 trading days to 8 Oct 2026'),
                 '2026-10-09T19:00:00+08:00')
        old, new = led['records']
        self.assertEqual((old['status'], old['replacedBy'], old['exitReason']), ('replaced', new['id'], 're-picked'))
        self.assertEqual((new['status'], new['entry'], new['basisSession']), ('pending', 240.0, '2026-10-08'))

    def test_repick_of_a_live_trade_only_notes_the_date(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB)
        led['records'][0].update(status='triggered', fillPrice=234.77)
        acts = T.ingest(led, block([pick(entry=240.0, stop=228.0, target=264.0)], window='25 trading days to 8 Oct 2026'),
                        '2026-10-09T19:00:00+08:00')
        self.assertEqual(len(led['records']), 1)
        self.assertEqual(led['records'][0]['repickDates'], ['2026-10-09'])
        self.assertEqual(led['records'][0]['entry'], 234.77)                       # original levels untouched
        self.assertTrue(any('kept the open trade' in a for a in acts))

    def test_same_day_new_record_gets_a_suffix(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB)
        led['records'][0].update(status='stopped', exitDate='2026-10-02')
        T.ingest(led, block([pick(entry=240.0, stop=228.0, target=264.0)], window='25 trading days to 2 Oct 2026'),
                 '2026-10-02T21:00:00+08:00')
        self.assertEqual([r['id'] for r in led['records']], ['2026-10-02-NVDA', '2026-10-02-NVDA-2'])

    def test_pick_without_levels_is_skipped(self):
        led = T.empty_ledger()
        acts = T.ingest(led, block([pick(stop=None)]), PUB)
        self.assertEqual(led['records'], [])
        self.assertTrue(any('missing entry/stop/target' in a for a in acts))

    def test_basis_falls_back_to_last_completed_session_and_notes_a_mismatch(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()], window=''), PUB)
        self.assertEqual(led['records'][0]['basisSession'], '2026-10-01')
        led2 = T.empty_ledger()
        T.ingest(led2, block([pick()], window='25 trading days to 30 Sep 2026'), PUB)
        self.assertTrue(led2['records'][0]['notes'])

    def test_ingest_from_an_older_page_is_labelled(self):
        led = T.empty_ledger()
        T.ingest(led, block([pick()]), PUB, source='previous published page')
        self.assertIn('first-ingest', led['records'][0]['notes'][0])


class Merge(unittest.TestCase):
    def led(self, *recs):
        return {'schemaVersion': 1, 'records': list(recs)}

    def test_later_evaluated_through_wins_for_non_terminal(self):
        a = rec(id='a', status='triggered', evaluatedThrough='2026-09-10')
        b = rec(id='a', status='triggered', evaluatedThrough='2026-09-14', lastClose=101.0)
        m, c = T.merge_ledgers(self.led(a), self.led(b))
        self.assertEqual((m['records'][0]['evaluatedThrough'], c), ('2026-09-14', []))
        m2, _ = T.merge_ledgers(self.led(b), self.led(a))
        self.assertEqual(m2['records'][0]['evaluatedThrough'], '2026-09-14')

    def test_terminal_beats_non_terminal_in_both_directions(self):
        t = rec(id='a', status='stopped', exitDate='2026-09-12', evaluatedThrough='2026-09-12')
        n = rec(id='a', status='triggered', evaluatedThrough='2026-09-30')
        for x, y in ((t, n), (n, t)):
            m, c = T.merge_ledgers(self.led(x), self.led(y))
            self.assertEqual((m['records'][0]['status'], c), ('stopped', []))

    def test_conflicting_terminal_copies_keep_the_first_and_log(self):
        a = rec(id='a', status='stopped', exitDate='2026-09-12', exitPrice=95.0)
        b = rec(id='a', status='target', exitDate='2026-09-13', exitPrice=110.0)
        m, c = T.merge_ledgers(self.led(a), self.led(b))
        self.assertEqual(m['records'][0]['status'], 'stopped')
        self.assertEqual(len(c), 1)

    def test_disjoint_ids_are_unioned_and_lists_merged(self):
        a = rec(id='a', repickDates=['2026-09-10'], notes=['n1'])
        b = rec(id='a', repickDates=['2026-09-11'], notes=['n1', 'n2'])
        c = rec(id='c')
        m, _ = T.merge_ledgers(self.led(a), self.led(b, c))
        self.assertEqual([r['id'] for r in m['records']], ['a', 'c'])
        self.assertEqual((m['records'][0]['repickDates'], m['records'][0]['notes']), (['2026-09-10', '2026-09-11'], ['n1', 'n2']))

    def test_evaluate_never_overwrites_a_terminal_outcome_but_reports_the_disagreement(self):
        led = self.led(rec(id='a', ticker='AAA', status='stopped', exitDate=SESS[1], exitPrice=95.0, fillPrice=100.0))
        rep = T.evaluate(led, {'AAA': {'rows': rows(TRIG, (101.0, 111.0, 100.0, 110.0))}}, SESS[1], verify_terminal=True)
        self.assertEqual(led['records'][0]['status'], 'stopped')
        self.assertEqual(len(rep['conflicts']), 1)


def closed(status, r, pub='2026-10-02T19:46:23+08:00', band=84, **kw):
    fill = 100.0
    exit_px = {'target': 110.0, 'stopped': 95.0}.get(status, 100.0 + (r or 0) * 5)
    d = rec(id=f'{pub}-{status}-{r}-{kw.get("i", 0)}', status=status, fillPrice=fill, exitPrice=exit_px, exitReason=status,
            rMultiple=r, daysHeld=5, exitDate='2026-10-09', publishedAt=pub, chased=False, regimeLabel='MIXED',
            sector='Technology', scoringVersion='v1',
            readiness=({'composite': band} if band is not None else None))
    d.update(kw)
    return d


class Stats(unittest.TestCase):
    def twenty(self):
        return [closed('target', 2.0, i=i) for i in range(12)] + [closed('stopped', -1.0, i=i + 12) for i in range(8)]

    def test_below_twenty_closed_withholds_every_percentage(self):
        s = T.summarize(self.twenty()[:19])['overall']
        self.assertTrue(s['tooSmall'])
        self.assertEqual(s['label'], 'n too small (<20)')
        for k in ('winRate', 'avgR', 'expectancy', 'triggerRate', 'avgDaysHeld'):
            self.assertIsNone(s[k], k)
        self.assertEqual(s['closed'], 19)

    def test_twenty_closed_reports_the_numbers(self):
        s = T.summarize(self.twenty())['overall']
        self.assertFalse(s['tooSmall'])
        self.assertAlmostEqual(s['winRate'], 0.6)
        self.assertAlmostEqual(s['avgR'], 0.8)
        self.assertAlmostEqual(s['avgWinR'], 2.0)
        self.assertAlmostEqual(s['avgLossR'], -1.0)
        self.assertAlmostEqual(s['expectancy'], 0.6 * 2.0 + 0.4 * -1.0)
        self.assertEqual(s['avgDaysHeld'], 5)

    def test_a_positive_time_exit_is_a_win_and_a_negative_one_is_not(self):
        rs = self.twenty()[:18] + [closed('time-exit', 0.2, i=90), closed('time-exit', -0.2, i=91)]
        s = T.summarize(rs)['overall']
        targets = sum(1 for r in rs if r['exitReason'] == 'target')
        self.assertEqual(s['raw']['wins'], targets + 1)              # only the +0.2R time-exit joins the target hits

    def test_skipped_is_counted_but_stays_out_of_win_rate_and_r(self):
        rs = self.twenty() + [closed('skipped', None, i=70, fillPrice=None, exitPrice=None, rMultiple=None, daysHeld=None)]
        s = T.summarize(rs)['overall']
        self.assertEqual((s['closed'], s['skipped'], s['picks']), (20, 1, 21))
        self.assertAlmostEqual(s['avgR'], 0.8)

    def test_replaced_records_are_not_picks(self):
        rs = self.twenty() + [rec(id='r', status='replaced')]
        self.assertEqual(T.summarize(rs)['overall']['picks'], 20)

    def test_effective_sets_counts_distinct_publishes(self):
        rs = [closed('target', 2.0, pub='2026-10-02T19:46:23+08:00', i=1), closed('stopped', -1.0, pub='2026-10-02T19:46:23+08:00', i=2),
              closed('target', 2.0, pub='2026-10-09T19:46:23+08:00', i=3)]
        s = T.summarize(rs)['overall']
        self.assertEqual((s['picks'], s['sets'], s['closedSets']), (3, 2, 2))

    def test_band_boundaries(self):
        mk = lambda c: closed('target', 1.0, band=c)  # noqa: E731
        self.assertEqual([T.band(mk(c)) for c in (0, 49, 50, 69, 70, 100)], ['<50', '<50', '50-69', '50-69', '>=70', '>=70'])
        self.assertEqual(T.band(closed('target', 1.0, band=None)), 'unscored')

    def test_splits_by_band_regime_sector_version_and_fill(self):
        rs = [closed('target', 2.0, band=80, i=1), closed('stopped', -1.0, band=40, i=2, regimeLabel='RISK-ON', sector='Industrials',
                                                           chased=True, scoringVersion='v2')]
        s = T.summarize(rs)
        self.assertEqual(s['byReadinessBand']['>=70']['closed'], 1)
        self.assertEqual(s['byReadinessBand']['<50']['closed'], 1)
        self.assertEqual(set(s['byRegime']), {'MIXED', 'RISK-ON'})
        self.assertEqual(set(s['bySector']), {'Technology', 'Industrials'})
        self.assertEqual(set(s['byScoringVersion']), {'v1', 'v2'})
        self.assertEqual({k: v['closed'] for k, v in s['byFill'].items()}, {'clean fill': 1, 'chased fill': 1})

    def test_trigger_rate_uses_filled_over_resolved_pending(self):
        rs = self.twenty() + [rec(id='i', status='invalidated'), rec(id='e', status='expired')]
        s = T.summarize(rs)['overall']
        self.assertAlmostEqual(s['triggerRate'], 20 / 22)


class Chips(unittest.TestCase):
    def test_chip_states(self):
        last = '2026-10-02'
        self.assertEqual(T.chip(rec(status='pending', startSession='2026-10-02', evaluatedThrough='2026-10-02'), last), 'waiting')
        self.assertEqual(T.chip(rec(status='triggered', startSession='2026-10-01', evaluatedThrough='2026-10-02'), last), 'live')
        self.assertEqual(T.chip(rec(status='pending', startSession='2026-10-01', evaluatedThrough=None), last), 'awaiting')
        self.assertEqual(T.chip(rec(status='triggered', startSession='2026-10-01', evaluatedThrough='2026-10-01'), last), 'awaiting')
        self.assertEqual(T.chip(rec(status='pending', startSession='2026-10-05', evaluatedThrough=None), last), 'waiting')
        for s in ('stopped', 'target', 'expired', 'invalidated', 'time-exit', 'skipped'):
            self.assertEqual(T.chip(rec(status=s), last), s)

    def test_view_open_and_closed_lists(self):
        recs = [closed('target', 2.0, i=1, exitDate='2026-10-05'), closed('stopped', -1.0, i=2, exitDate='2026-10-07'),
                rec(id='o', status='triggered', ticker='LIVE', fillPrice=100.0, lastClose=103.0, startSession='2026-10-01',
                    evaluatedThrough='2026-10-08')]
        v = T.build_view({'records': recs}, '2026-10-08')
        self.assertEqual([c['exitDate'] for c in v['closed20']], ['2026-10-07', '2026-10-05'])
        self.assertEqual((v['open'][0]['ticker'], v['open'][0]['openR'], v['chips']['LIVE']['chip']), ('LIVE', 0.6, 'live'))


QRVO = [  # 26 sessions to 30 Sep 2026 (date, open, high, low, close, volume): golden fixture for the v1 recompute
    ('2026-08-25', 95.5, 95.53, 91.86, 94.34, 772008), ('2026-08-26', 94.54, 95.26, 93.445, 95.135, 636711),
    ('2026-08-27', 95.14, 96.5, 94.88, 96.18, 631572), ('2026-08-28', 96.33, 96.49, 93.38, 94.73, 841566),
    ('2026-08-31', 94.76, 96.83, 93.94, 96.08, 1928899), ('2026-09-01', 95.03, 96.865, 93.67, 96.4, 857457),
    ('2026-09-02', 96.4, 102.04, 96.1, 100.63, 1262514), ('2026-09-03', 99.8, 101.528, 99.255, 100.38, 1076638),
    ('2026-09-04', 101.0, 103.55, 100.485, 102.84, 718294), ('2026-09-08', 103.18, 104.545, 102.35, 104.07, 1440359),
    ('2026-09-09', 103.86, 105.39, 103.035, 105.24, 981346), ('2026-09-10', 103.88, 114.28, 103.61, 112.36, 2255394),
    ('2026-09-11', 112.835, 120.52, 112.595, 116.65, 2192998), ('2026-09-14', 114.11, 115.08, 107.84, 107.98, 1783068),
    ('2026-09-15', 108.4, 118.46, 108.11, 118.06, 2292951), ('2026-09-16', 118.18, 118.18, 113.59, 113.97, 1777914),
    ('2026-09-17', 114.9, 119.735, 113.63, 119.51, 1569867), ('2026-09-18', 120.76, 120.76, 115.88, 117.18, 3104205),
    ('2026-09-21', 118.44, 119.89, 116.658, 117.21, 1017987), ('2026-09-22', 116.61, 119.18, 116.61, 118.44, 1036266),
    ('2026-09-23', 118.44, 121.09, 117.841, 119.59, 1129395), ('2026-09-24', 118.92, 119.64, 115.91, 116.34, 950457),
    ('2026-09-25', 116.9, 119.3, 115.54, 117.97, 1386571), ('2026-09-28', 117.81, 118.405, 114.44, 116.3, 1529699),
    ('2026-09-29', 116.94, 118.17, 114.89, 116.75, 751327), ('2026-09-30', 116.56, 118.63, 113.475, 114.48, 4473757)]
QRVO_ROWS = [dict(zip(('date', 'open', 'high', 'low', 'close', 'volume'), t)) for t in QRVO]


class Readiness(unittest.TestCase):
    def test_golden_recompute_matches_the_independently_computed_scratch_result(self):
        r = T.readiness_v1(QRVO_ROWS)
        self.assertEqual((r['pivot'], r['timeInBaseDays']), (121.09, 5))
        self.assertEqual((r['tightness'], r['proximity'], r['volumeDryUp'], r['timeInBase'], r['composite']), (5, 9, 1, 25, 41))

    def test_round_half_up(self):
        self.assertEqual([T.round_half_up(v) for v in (5.5, 4.5, 5.4999, 5.4999999999999, 0.0, 24.6)], [6, 5, 5, 6, 0, 25])   # last tie-ish value is inside the 1e-9 float epsilon

    def test_new_high_today_has_no_base(self):
        rs = [dict(r) for r in QRVO_ROWS]
        rs[-1].update(high=125.0, close=124.0, low=116.0, open=117.0)
        r = T.readiness_v1(rs)
        self.assertEqual((r['timeInBaseDays'], r['volumeDryUp'], r['timeInBase']), (0, 0, 0))

    def test_closes_match(self):
        pub = [r['close'] for r in QRVO_ROWS][-20:]
        self.assertEqual(T.closes_match(QRVO_ROWS, pub), [])
        pub[3] += 0.5
        self.assertEqual(len(T.closes_match(QRVO_ROWS, pub)), 1)


class Parsing(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(T.parse_zone('1,387.47 – 1,401.33'), (1387.47, 1401.33))
        self.assertEqual(T.parse_zone('364.30 – 366.00'), (364.3, 366.0))
        self.assertEqual(T.parse_next_earnings('28 Oct', '2026-10-02'), '2026-10-28')
        self.assertEqual(T.parse_next_earnings('3 Jan', '2026-12-20'), '2027-01-03')
        self.assertEqual(T.parse_next_earnings('3 Nov, after close', '2026-09-22'), '2026-11-03')
        self.assertEqual(T.parse_next_earnings('24 Sep, before open', '2026-09-22'), '2026-09-24')
        self.assertIsNone(T.parse_next_earnings('reports 24 Sep', '2026-09-20'))
        self.assertEqual(T.parse_pick_window_end('25 trading days to 1 Oct 2026, stockanalysis.com'), '2026-10-01')
        self.assertIsNone(T.parse_pick_window_end('garbage'))


class EndToEnd(unittest.TestCase):
    def test_update_cli_is_idempotent_and_writes_ledger_and_view(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data = block([pick('NVDA', 100.0, 95.0, 110.0, entryZone='100.00 – 101.00')])
            (td / 'data.json').write_text(json.dumps(data), encoding='utf-8')
            rs = [{'date': d, 'open': 99.0, 'high': 101.0, 'low': 98.0, 'close': 100.0, 'volume': 5} for d in ('2026-10-02', '2026-10-05')]
            (td / 'ohlc.json').write_text(json.dumps({'tickers': {'NVDA': {'rows': rs}}}), encoding='utf-8')
            args = ['update', '--ledger', str(td / 'picks.json'), '--data', str(td / 'data.json'), '--ohlc', str(td / 'ohlc.json'),
                    '--published-at', PUB, '--now', '2026-10-05T23:00:00+00:00', '--view', str(td / 'view.json')]
            self.assertEqual(T.main(args), 0)
            first = (td / 'picks.json').read_text(encoding='utf-8')
            self.assertEqual(T.main(args), 0)
            self.assertEqual((td / 'picks.json').read_text(encoding='utf-8'), first)
            led = json.loads(first)
            self.assertEqual((len(led['records']), led['records'][0]['status']), (1, 'triggered'))
            view = json.loads((td / 'view.json').read_text(encoding='utf-8'))
            self.assertEqual(view['chips']['NVDA']['chip'], 'live')

    def test_ledger_file_is_one_record_per_line(self):
        led = {'schemaVersion': 1, 'records': [rec(id='a'), rec(id='b')]}
        txt = T.dump_ledger(led)
        self.assertEqual(json.loads(txt), led)
        self.assertEqual(len(txt.strip().splitlines()), 4)


if __name__ == '__main__':
    unittest.main(verbosity=1)
