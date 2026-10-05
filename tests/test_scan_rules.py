"""Unit tests for the 5 Oct 2026 additions: reset keep/clear policy, the <=10 quality selection, strength flags, priced-in score,
last-run / data-age plumbing. stdlib unittest, synthetic fixtures only (never reads data/brief-data.json or data/picks.json content)."""
import copy, datetime as dt, json, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import selection as S, pricedin as P, flags as F, reset_policy as R, health as H, market_time as mt, tracker as T   # noqa: E402

LAST = '2026-10-02'


def cand(tk='AAA', comp=70, tib=5, last=100.0, pivot=101.0, entry=101.0, low=98.0, atr=3.0, adr=4.0, earn='2026-11-05', prox=20):
    """Close 100, pivot 101 (1% below it), entry 101. Default placed stop = 5% below the entry = 95.95 -> risk 5.0%, R:R 2.0."""
    return {'tk': tk, 'readiness': {'composite': comp, 'proximity': prox, 'tightness': 10, 'volumeDryUp': 10, 'timeInBase': 10},
            'timeInBaseDays': tib, 'lastClose': last, 'pivot': pivot, 'entry': entry, 'lastLow': low, 'atr14': atr, 'adr20Pct': adr,
            'nextEarnings': earn, 'lastSession': LAST}


class Selection(unittest.TestCase):
    def test_a_clean_candidate_passes_every_rule(self):
        r = S.check(cand())
        self.assertTrue(r['passed'], r['failed'])
        self.assertEqual((r['levels']['stop'], r['levels']['riskPct'], r['levels']['rr']), (95.95, 5.0, 2.0))

    def test_readiness_threshold_is_a_config_value(self):
        self.assertFalse(S.check(cand(comp=54))['passed'])
        self.assertTrue(S.check(cand(comp=54), {'minReadiness': 50})['passed'])
        self.assertEqual(S.CONFIG['minReadiness'], 55)

    def test_same_day_or_too_fresh_pivot_is_not_a_base(self):
        self.assertIn('base', [f['rule'] for f in S.check(cand(tib=0))['failed']])
        self.assertIn('base', [f['rule'] for f in S.check(cand(tib=1))['failed']])
        self.assertTrue(S.check(cand(tib=2))['passed'])

    def test_proximity_gate_is_within_five_percent_of_the_pivot(self):
        self.assertTrue(S.check(cand(last=95.0, pivot=100.0, entry=100.0, low=None))['rules']['proximity'])      # exactly 5.0% below: allowed
        r = S.check(cand(last=94.9, pivot=100.0, entry=100.0, low=None))
        self.assertIn('proximity', [f['rule'] for f in r['failed']])
        self.assertIn('not set up yet', [f['reason'] for f in r['failed'] if f['rule'] == 'proximity'][0])
        self.assertTrue(S.check(cand(last=94.9, pivot=100.0, entry=100.0, low=None), {'maxBelowPivotPct': 6})['rules']['proximity'])
        self.assertEqual(S.CONFIG['maxBelowPivotPct'], 5.0)

    def test_the_stop_is_placed_at_the_lowest_of_the_candidate_stops(self):
        self.assertEqual(S.place_stop(cand())[0], 95.95)                                                     # 5% below the entry binds
        self.assertEqual(S.place_stop(cand(atr=6.0)), (94.0, '1x ATR14 below the close'))                    # 1 x ATR14 below the CLOSE
        self.assertEqual(S.place_stop(cand(atr=1.0, adr=14.0)), (93.0, '0.5x ADR20 below the close'))        # 0.5 x ADR20$ below the close
        self.assertEqual(S.place_stop(cand(low=95.5)), (95.5, "the last session's low"))                     # a low 5.4% under the entry qualifies and is lowest
        self.assertEqual(S.place_stop(cand(low=90.0))[0], 95.95)                                             # a low 10.9% under the entry is NOT a qualifying low

    def test_the_stop_is_always_below_the_close_and_unknown_inputs_fail_closed(self):
        for atr in (0.5, 3.0, 12.0):
            self.assertLess(S.place_stop(cand(atr=atr))[0], 100.0)
        self.assertIn('stop-valid', [f['rule'] for f in S.check(cand(atr=None))['failed']])
        self.assertIn('stop-valid', [f['rule'] for f in S.check({**cand(), 'adr20Pct': None})['failed']])

    def test_risk_over_eight_percent_is_rejected(self):
        self.assertIn('risk', [f['rule'] for f in S.check(cand(atr=10.5))['failed']])      # stop 89.5 -> risk 11.4%
        self.assertTrue(S.check(cand(atr=5.0))['rules']['risk'])                          # stop 95 -> 5.94%

    def test_rr_must_reach_one_point_five_at_the_fixed_ten_percent_target(self):
        # ATR 5.5 -> stop 94.5 -> risk 6.44% -> R:R 1.55 passes ; ATR 6 -> stop 94.0 -> risk 6.93% -> 1.44 fails (risk still <= 8)
        self.assertTrue(S.check(cand(atr=5.5))['passed'], S.check(cand(atr=5.5))['failed'])
        r = S.check(cand(atr=6.0))
        self.assertEqual([f['rule'] for f in r['failed']], ['rr'])
        self.assertIn('fixed +10% target', r['failed'][0]['reason'])

    def test_the_target_definition_never_changes_with_risk(self):
        self.assertEqual(S.levels(cand(atr=10.5))['target'], round(101.0 * 1.10, 2))     # 111.10 even when the stop is wide
        self.assertEqual(S.CONFIG['targetPct'], 10.0)

    def test_earnings_inside_three_sessions_is_rejected(self):
        # LAST = Fri 2 Oct; next sessions: Mon 5, Tue 6, Wed 7, Thu 8
        self.assertEqual(S.sessions_until(LAST, '2026-10-05'), 1)
        self.assertIn('earnings', [f['rule'] for f in S.check(cand(earn='2026-10-07'))['failed']])      # 3 sessions: blocked
        self.assertTrue(S.check(cand(earn='2026-10-08'))['passed'])                                      # 4 sessions: fine

    def test_unverified_earnings_date_rejects_with_the_stated_reason(self):
        self.assertEqual(S.check(cand(earn=None))['failed'][0]['reason'], 'earnings date unverified')

    def test_a_just_reported_date_is_not_a_blackout_but_an_old_stale_one_is_unverified(self):
        self.assertTrue(S.check(cand(earn='2026-09-30'))['passed'])            # reported 2 days ago, next one is a quarter away
        self.assertIn('unverified', S.check(cand(earn='2026-06-01'))['failed'][0]['reason'])

    def test_zero_picks_is_a_valid_result(self):
        r = S.select([cand('A', comp=40), cand('B', comp=30)])
        self.assertEqual((r['picks'], r['qualified'], r['scored']), ([], 0, 2))
        self.assertEqual(S.header_line(r), '0 of 2 scored names qualified (readiness ≥ 55)')

    def test_picks_carry_the_placed_levels(self):
        p = S.select([cand('A')])['picks'][0]
        self.assertEqual((p['stop'], p['target'], p['riskPct'], p['rr']), (95.95, 111.1, 5.0, 2.0))

    def test_at_most_ten_ranked_by_readiness_and_the_rest_are_cut(self):
        r = S.select([cand(f'T{i:02d}', comp=60 + i) for i in range(14)])
        self.assertEqual((len(r['picks']), r['qualified']), (10, 14))
        self.assertEqual([c['tk'] for c in r['picks']][:3], ['T13', 'T12', 'T11'])
        self.assertEqual({c['tk'] for c in r['cut']}, {'T00', 'T01', 'T02', 'T03'})

    def test_ranking_ties_break_on_proximity_then_ticker(self):
        r = S.select([cand('ZZZ', comp=70, prox=10), cand('AAA', comp=70, prox=20), cand('MMM', comp=70, prox=20)])
        self.assertEqual([c['tk'] for c in r['picks']], ['AAA', 'MMM', 'ZZZ'])

    def test_threshold_counts_for_tuning(self):
        t = S.threshold_counts([cand('A', comp=46), cand('B', comp=56), cand('C', comp=66)])
        self.assertEqual(t['passing'], {'45': 3, '55': 2, '65': 1})
        self.assertEqual(t['scores'], [66, 56, 46])

    def test_failure_summary_explains_a_short_list(self):
        cs = [cand('LOW', comp=40), cand('LVL', comp=80, atr=10.5), cand('BOTH', comp=70, tib=0, atr=10.5), cand('OK', comp=70)]
        w = S.failure_summary(cs)
        self.assertEqual((w['scored'], w['readinessOk']), (4, 3))
        self.assertEqual([b['tk'] for b in w['blocked']], ['LVL', 'BOTH'])
        self.assertEqual([b['tk'] for b in w['blockedOnlyLevels']], ['LVL'])          # BOTH also fails the base rule
        self.assertEqual(w['byRule']['readiness'], 1)

    def test_the_funnel_counts_names_gate_by_gate(self):
        cs = [cand('A'), cand('B', comp=40), cand('C', tib=0), cand('D', last=90.0), cand('E', atr=6.0), cand('F', earn='2026-10-06')]
        rows, alive = S.funnel(cs)
        self.assertEqual([(g, n) for g, n, _ in rows], [('scored', 6), ('readiness', 5), ('base', 4), ('proximity', 3), ('stop-valid', 3), ('risk', 3), ('rr', 2), ('earnings', 1)])
        self.assertEqual([c['tk'] for c in alive], ['A'])


class PricedIn(unittest.TestCase):
    def test_component_a_anchors(self):
        self.assertEqual(P.comp_a(4.0, 4.0), 0)             # 1x ADR -> 0
        self.assertEqual(P.comp_a(12.0, 4.0), 25)           # 3x -> 25
        self.assertAlmostEqual(P.comp_a(8.0, 4.0), 12.5)    # 2x -> half
        self.assertEqual(P.comp_a(-12.0, 4.0), 25)          # direction does not matter

    def test_component_c_anchors(self):
        self.assertEqual(P.comp_c(95, 100), 25)             # 5% below target -> 25
        self.assertEqual(P.comp_c(110, 100), 25)            # above target -> 25
        self.assertEqual(P.comp_c(75, 100), 0)              # 25% below -> 0
        self.assertAlmostEqual(P.comp_c(85, 100), 12.5)

    def test_component_d_anchors(self):
        self.assertEqual(P.comp_d(-3), 0)
        self.assertEqual(P.comp_d(0), 0)
        self.assertEqual(P.comp_d(20), 25)
        self.assertAlmostEqual(P.comp_d(10), 12.5)

    def test_component_b_strong_early_hold_is_low_and_a_faded_stale_gap_is_high(self):
        early_strong = P.comp_b(last=110, event_high=108, event_low=100, sessions_since=2)     # above the event-day high, day 2
        self.assertEqual(early_strong, 0)
        faded_stale = P.comp_b(last=99, event_high=108, event_low=100, sessions_since=6)       # below the low, 6 sessions on
        self.assertEqual(faded_stale, 25)
        flat_stale = P.comp_b(last=108, event_high=108, event_low=100, sessions_since=6)       # held the high but flat for 5+ sessions
        self.assertAlmostEqual(flat_stale, 25 * 0.4)

    def test_score_is_rescaled_to_100_and_labelled(self):
        full = P.score({'movePct': 12, 'adr20Pct': 4, 'last': 99, 'eventHigh': 108, 'eventLow': 100, 'sessionsSince': 6, 'price': 100, 'target': 100, 'runUpPct': 25})
        self.assertEqual((full['score'], full['n'], full['partial']), (100, 4, False))
        self.assertEqual(full['band'], 'mostly or fully priced in')
        self.assertEqual(full['label'], 'Priced-in 100/100 — mostly or fully priced in')

    def test_partial_scores_rescale_and_show_the_component_count(self):
        p = P.score({'price': 95, 'target': 100})                                              # only component c = 25/25
        self.assertEqual((p['score'], p['n'], p['partial']), (100, 1, True))
        self.assertIn('1/4 components, partial', p['label'])
        p = P.score({'price': 75, 'target': 100, 'runUpPct': 20})                              # 0 and 25 -> 50
        self.assertEqual((p['score'], p['n']), (50, 2))

    def test_nothing_available_is_not_scored(self):
        p = P.score({})
        self.assertIsNone(p['score'])
        self.assertEqual(p['n'], 0)

    def test_bands(self):
        self.assertEqual([P.band(x) for x in (0, 30, 31, 60, 61, 100)],
                         ['room left', 'room left', 'partly priced in', 'partly priced in', 'mostly or fully priced in', 'mostly or fully priced in'])

    def test_alerts_rank_lowest_first_with_unscored_last(self):
        items = [{'tk': 'B', 'pricedIn': {'score': 70}}, {'tk': 'C'}, {'tk': 'A', 'pricedIn': {'score': 20}}, {'tk': 'D', 'pricedIn': {'score': None}}]
        self.assertEqual([c['tk'] for c in P.rank_alerts(items)], ['A', 'B', 'C', 'D'])

    def test_inputs_from_ohlc_exclude_the_event_day_from_adr_and_runup(self):
        days = [d.isoformat() for d in reversed(mt.trading_days_back(dt.date(2026, 10, 2), 40))]
        rows = [{'date': d, 'open': 100, 'high': 101, 'low': 99, 'close': 100 + (0.1 * i), 'volume': 1} for i, d in enumerate(days)]
        ev = 30
        rows[ev].update(open=110, high=120, low=108, close=118)                                  # a wide event day
        inp = P.inputs_from_ohlc(rows, days[ev], days[-1], price=119, target=130)
        self.assertAlmostEqual(inp['adr20Pct'], 100 * (101 / 99 - 1), places=2)                 # the 20 sessions BEFORE the event: the wide day is not in it
        self.assertEqual(inp['eventHigh'], 120)
        self.assertEqual(inp['sessionsSince'], len(days) - ev)
        self.assertAlmostEqual(inp['runUpPct'], round((rows[ev - 1]['close'] / rows[ev - 21]['close'] - 1) * 100, 2))
        self.assertGreater(inp['movePct'], 10)


class Flags(unittest.TestCase):
    SECTORS = [{'name': 'Technology', 'pct': 6.7}, {'name': 'Communication Services', 'pct': 0.4}, {'name': 'Industrials', 'pct': -0.6}, {'name': 'Energy', 'pct': -2.6}]
    INDUSTRIES = [{'sector': 'Technology', 'items': [{'name': 'Semiconductors', 'pct': 10.8}, {'name': 'Electronic Components', 'pct': 10.9}]}]

    def test_sector_flag_needs_all_three_conditions(self):
        f, u = F.sector_flag('Technology', 'Semiconductors', 14.0, self.SECTORS, self.INDUSTRIES)
        self.assertEqual(f['key'], 'sector')
        self.assertIn('+14.0% 1M', f['tip'])
        self.assertEqual(f['inputs']['sectorRank'], 1)
        self.assertIsNone(F.sector_flag('Technology', 'Semiconductors', 10.0, self.SECTORS, self.INDUSTRIES)[0])      # does not beat its industry
        self.assertIsNone(F.sector_flag('Technology', 'Software - Infrastructure', 14.0, self.SECTORS, self.INDUSTRIES)[0])   # industry not in the top 3
        self.assertIsNone(F.sector_flag('Energy', 'Oil & Gas E&P', 14.0, self.SECTORS, self.INDUSTRIES)[0])           # sector not in the top 3

    def test_sector_flag_unavailable_input_is_reported_not_guessed(self):
        f, u = F.sector_flag('Technology', 'Semiconductors', None, self.SECTORS, self.INDUSTRIES)
        self.assertIsNone(f)
        self.assertIn('unavailable', u)

    def test_financials_needs_three_of_four_and_shows_which(self):
        f, u = F.financials_flag({'revGrowthPct': 20, 'epsGrowthPct': 25, 'opMarginPct': 30, 'opMarginPriorPct': 28, 'fcf': -5})
        self.assertEqual(f['inputs']['passed'], ['revenue', 'eps', 'margin'])
        self.assertIn('3 of 4 passed', f['tip'])
        self.assertIn('Not passed', f['tip'])
        f, u = F.financials_flag({'revGrowthPct': 20, 'epsGrowthPct': 5, 'opMarginPct': 30, 'opMarginPriorPct': 28, 'fcf': -5})
        self.assertIsNone(f)
        self.assertIsNone(u)                                                                           # decided: simply not notable

    def test_financials_margin_tolerance_and_eps_note(self):
        f, _ = F.financials_flag({'revGrowthPct': 20, 'epsGrowthPct': -50, 'epsNote': 'one-off in the base year', 'opMarginPct': 28.0, 'opMarginPriorPct': 28.4, 'fcf': 1})
        self.assertIn('margin', f['inputs']['passed'])                                                 # 0.4pp lower is "stable"
        self.assertNotIn('eps', f['inputs']['passed'])
        self.assertIn('one-off in the base year', f['tip'])

    def test_financials_undetermined_when_missing_inputs_could_change_the_outcome(self):
        f, u = F.financials_flag({'revGrowthPct': 20, 'epsGrowthPct': 25, 'fcf': None, 'opMarginPct': None})
        self.assertIsNone(f)
        self.assertIn('undetermined', u)

    def _cat(self, **k):
        return {**{'ctype': 'Earnings beat + raised guidance', 'cdateISO': '2026-10-01', 'sources': [{'u': 'https://a.com/x'}, {'u': 'https://b.com/y'}]}, **k}

    def test_catalyst_flag_rules(self):
        ok = {'score': 40, 'n': 4, 'label': 'Priced-in 40/100 — partly priced in'}
        f, u = F.catalyst_flag(self._cat(), ok, LAST)
        self.assertEqual(f['key'], 'catalyst')
        self.assertIsNone(F.catalyst_flag(self._cat(), {**ok, 'score': 61}, LAST)[0])                  # too priced in
        self.assertIsNone(F.catalyst_flag(self._cat(sources=[{'u': 'https://a.com/x'}, {'u': 'https://a.com/y'}]), ok, LAST)[0])   # one outlet
        self.assertIsNone(F.catalyst_flag(self._cat(ctype='CEO interview'), ok, LAST)[0])             # not a playbook type
        self.assertIsNone(F.catalyst_flag(self._cat(cdateISO='2026-09-10'), ok, LAST)[0])             # older than 10 sessions
        f, u = F.catalyst_flag(self._cat(), {**ok, 'n': 2}, LAST)
        self.assertIsNone(f)
        self.assertIn('≥3 components', u)

    def test_catalyst_types_map_onto_the_playbook(self):
        self.assertEqual([F.ctype_class(x) for x in ('Earnings beat + raised guidance', 'Major contract + raised guidance', 'Analyst upgrade',
                                                      'FDA approval', 'S&P 500 index addition')],
                         ['earnings beat + raise', 'earnings beat + raise', 'analyst upgrade', 'approval / launch', 'index add'])


class ResetPolicy(unittest.TestCase):
    def test_the_list_has_every_item_the_brief_asked_for(self):
        clear = ' | '.join(c['what'] for c in R.CLEAR)
        for w in ('sector ranking', 'industry drill-down', 'technical picks', 'near-misses', 'older than 5 trading days', 'stale health flags'):
            self.assertIn(w, clear)
        keep = ' | '.join(k['what'] for k in R.KEEP)
        for w in ('pick ledger', 'provenance', 'still inside their 5-day window', 'Catalyst Playbook', 'regime-score rules'):
            self.assertIn(w, keep)

    def test_clear_and_keep_never_overlap(self):
        self.assertFalse(set(R.CLEAR_KEYS) & set(R.KEEP_KEYS))
        self.assertIn('picks', R.CLEAR_KEYS)
        self.assertIn('catalysts', R.KEEP_KEYS)

    def test_claude_md_carries_exactly_this_list(self):
        md = (ROOT / 'CLAUDE.md').read_text(encoding='utf-8')
        a, b = md.index('<!-- RESET:BEGIN -->'), md.index('<!-- RESET:END -->')
        self.assertEqual(md[a + len('<!-- RESET:BEGIN -->'):b].strip(), R.render_markdown())

    def test_prepare_splits_live_and_expired_catalysts(self):
        prev = {'picks': [1], 'catalysts': [{'tk': 'A', 'cdateISO': '2026-10-01'}, {'tk': 'B', 'cdateISO': '2026-09-01'}], 'sectors': []}
        window = {x.isoformat() for x in mt.trading_days_back(dt.date(2026, 10, 2), 5)}
        plan = R.prepare(prev, catalyst_window=window)
        self.assertEqual((plan['repriceCatalysts'], plan['expireCatalysts']), (['A'], ['B']))
        self.assertIn('picks', plan['drop'])
        self.assertIn('catalysts', plan['keep'])

    def _ledger(self, d, recs):
        p = pathlib.Path(d) / 'picks.json'
        p.write_text(json.dumps({'schemaVersion': 1, 'records': recs}), encoding='utf-8')
        return p

    def test_verify_allows_appending_but_catches_a_rewritten_or_removed_record(self):
        done = {'id': 'x', 'status': 'stopped', 'exitPrice': 9.0, 'exitDate': '2026-10-01', 'rMultiple': -1.0, 'fillPrice': 10.0}
        with tempfile.TemporaryDirectory() as d:
            before = json.dumps({'records': [done]})
            plan = {'repriceCatalysts': ['A'], 'expireCatalysts': []}
            nb = {'catalysts': [{'tk': 'A'}], 'tracker': {'x': 1}}
            self.assertEqual(R.verify(plan, nb, self._ledger(d, [done, {'id': 'y', 'status': 'pending'}]), before), [])
            self.assertTrue(R.verify(plan, nb, self._ledger(d, [{**done, 'exitPrice': 12.0}]), before))              # terminal record changed
            self.assertTrue(R.verify(plan, nb, self._ledger(d, []), before))                                         # record removed

    def test_verify_catches_a_dropped_live_catalyst_a_kept_expired_one_and_a_missing_track_record(self):
        plan = {'repriceCatalysts': ['A'], 'expireCatalysts': ['B']}
        bad = R.verify(plan, {'catalysts': [{'tk': 'B'}], 'tracker': None})
        self.assertEqual(len(bad), 3)

    def test_ledger_digest_changes_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._ledger(d, [])
            a = R.file_digest(p)
            p.write_text('x', encoding='utf-8')
            self.assertNotEqual(a, R.file_digest(p))


class TrackerFields(unittest.TestCase):
    def _block(self, **pk):
        pick = {'tk': 'AAA', 'name': 'A', 'industry': 'I', 'sector': 'S', 'setup': 'x', 'pivot': 100.0, 'entryZone': '100.01 – 101.00', 'entry': 100.01,
                'stop': 95.0, 'target': 110.0, 'closes': [100.0] * 25, 'readiness': {'composite': 70, 'tightness': 1, 'proximity': 1, 'volumeDryUp': 1, 'timeInBase': 1}, **pk}
        return {'picks': [pick], 'pickWindow': '25 trading days to 2 Oct 2026, x', 'selection': {'version': S.SELECTION_VERSION, 'qualified': 3}}

    def test_new_records_carry_selection_version_rank_and_flags(self):
        led = T.empty_ledger()
        T.ingest(led, self._block(selectionRank=2, flags=[{'key': 'sector', 'inputs': {'a': 1}}]), '2026-10-05T19:00:00+08:00')
        r = led['records'][0]
        self.assertEqual((r['selectionVersion'], r['selectionRank'], r['selectionQualified']), (S.SELECTION_VERSION, 2, 3))
        self.assertEqual((r['flags'], r['flagInputs']), (['sector'], {'sector': {'a': 1}}))

    def test_a_pick_without_flags_data_is_not_evaluated_not_unflagged(self):
        led = T.empty_ledger()
        T.ingest(led, self._block(), '2026-10-05T19:00:00+08:00')
        self.assertIsNone(led['records'][0]['flags'])

    def test_stats_split_by_selection_version_and_by_flags(self):
        def rec(i, sv, fl):
            return {'id': i, 'status': 'pending', 'publishedAt': i, 'selectionVersion': sv, 'flags': fl, 'readiness': None}
        recs = [rec('1', 'top4-v1', None), rec('2', S.SELECTION_VERSION, []), rec('3', S.SELECTION_VERSION, ['sector']), rec('4', S.SELECTION_VERSION, ['sector', 'financials'])]
        s = T.summarize(recs)
        self.assertEqual({k: v['picks'] for k, v in s['bySelectionVersion'].items()}, {'top4-v1': 1, S.SELECTION_VERSION: 3})
        self.assertEqual({k: v['picks'] for k, v in s['byFlags'].items()},
                         {'any flag': 2, 'flag: financials': 1, 'flag: sector': 2, 'no flags': 1, 'not evaluated': 1})


class PageLogic(unittest.TestCase):
    def test_a_scan_with_zero_picks_is_a_healthy_technical_section(self):
        d = {'picks': [], 'selection': {'qualified': 0, 'scored': 50}}
        self.assertEqual(H.check_technical(d, {'lastSession': dt.date(2026, 10, 2)}), [])
        self.assertTrue(H.check_technical({'picks': []}, {'lastSession': dt.date(2026, 10, 2)}))        # empty WITHOUT a selection summary is still a failure

    def test_age_in_sessions_matches_the_calendar(self):
        # the page counts trading days between a section's asOf and the last completed session; Fri 2 Oct -> Mon 5 Oct is 1 session
        def age(asof, last):
            a, n, d = dt.date.fromisoformat(asof), 0, dt.date.fromisoformat(last)
            while a < d:
                a += dt.timedelta(days=1)
                n += mt.is_trading_day(a)
            return n
        self.assertEqual(age('2026-10-01', '2026-10-02'), 1)
        self.assertEqual(age('2026-10-02', '2026-10-06'), 2)
        self.assertEqual(age('2026-09-30', '2026-10-06'), 4)
        self.assertEqual(age('2026-09-04', '2026-09-08'), 1)           # Labor Day 7 Sep is not a session


if __name__ == '__main__':
    unittest.main()
