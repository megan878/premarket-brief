"""Backtest harness + setup detectors: point-in-time (no look-ahead) tests, one known example and one known non-example per detector, and the
validity checks that must still exclude a name. Synthetic bars only. Run:  python tests/test_bt_signals.py"""
import copy, math, pathlib, sys, unittest

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bt_signals as bs, bt_run as br, bt_report as rp, tracker as tr   # noqa: E402


def dates(n, start='2023-01-02'):
    return [d.strftime('%Y-%m-%d') for d in pd.bdate_range(start, periods=n)]


def frame(closes, rng, vols):
    """OHLCV from closes: open = previous close, high/low = close +/- half the per-bar range fraction."""
    c = np.asarray(closes, float)
    r = np.asarray(rng, float) if np.ndim(rng) else np.full(len(c), rng)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + r / 2)
    l = np.minimum(o, c) * (1 - r / 2)
    v = np.asarray(vols, float) if np.ndim(vols) else np.full(len(c), vols)
    return pd.DataFrame({'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}, index=dates(len(c)))


def series(df, cap=2e10, tk='TST'):
    return bs.Series(tk, df, bs.indicators(df), {'cap': cap, 'name': tk, 'industry': 'Test'})


def walk(n, seed=1, drift=0.0004, vol=0.012):
    rs = np.random.RandomState(seed)
    return 100 * np.exp(np.cumsum(drift + vol * rs.randn(n)))


# ───────────── fixtures: known examples ─────────────
def breakout_df(vol_day=2_500_000, close_day=102.6):
    n = 300
    c = np.full(n, 100.0) + np.sin(np.arange(n) / 3) * 0.4
    rng = np.full(n, 0.015)
    v = np.full(n, 1_000_000.0)
    c[-1] = close_day
    v[-1] = vol_day
    df = frame(c, rng, v)
    df.iloc[-1, df.columns.get_loc('high')] = close_day * 1.002
    df.iloc[-1, df.columns.get_loc('low')] = close_day - 1.6
    return df


def flag_df(run=True):
    c = list(np.full(240, 50.0) + np.sin(np.arange(240) / 5) * 0.3)
    vol = [1_000_000.0] * 240
    rng = [0.02] * 240
    if run:
        c += list(np.linspace(50, 72, 20))
    else:
        c += list(np.linspace(50, 51, 20))
    vol += [1_500_000.0] * 20
    rng += [0.04] * 20
    top = c[-1]
    c += [top - 0.2, top - 0.1, top - 0.25, top - 0.15, top - 0.1, top - 0.05]
    vol += [750_000.0] * 6
    rng += [0.008] * 6
    return frame(c, rng, vol)


def pullback_df():
    pull = (-0.008, -0.007, -0.006, -0.005, -0.004)
    n = 320
    m = n - len(pull)
    c = list(np.linspace(55, 100, m) * (1 + 0.012 * np.where(np.arange(m) % 2 == 0, 1, -1)))     # a steady uptrend with day-to-day noise
    for d in pull:
        c.append(c[-1] * (1 + d))
    vol = [1_000_000.0] * m + [650_000.0] * len(pull)
    return frame(c, 0.012, vol)


def base_df():
    c = list(100 + np.arange(260) * 0.05)                                                        # quiet drift to ~113
    c += [114, 118, 122, 126, 129, 132, 134, 136, 138, 139]                                      # run-up: wide, heavy bars into the pivot
    vol = [1_000_000.0] * 260 + [2_200_000.0] * 10
    rng = [0.015] * 260 + [0.05] * 10
    pivot_close = c[-1]
    c += [pivot_close * 1.01] + [pivot_close * 1.01 * (1 - 0.003 * (i % 3)) for i in range(9)]  # the pivot day, then 9 tight sessions under it
    vol += [2_400_000.0] + [650_000.0] * 9
    rng += [0.06] + [0.010] * 9
    return frame(c, rng, vol)


class PointInTime(unittest.TestCase):
    def test_every_indicator_is_causal(self):
        df = frame(walk(420, 3), 0.015, np.random.RandomState(4).randint(5e5, 3e6, 420))
        full = bs.indicators(df)
        for t in (260, 300, 399):
            cut = bs.indicators(df.iloc[:t + 1])
            for col in full.columns:
                a, b = full[col].iloc[t], cut[col].iloc[t]
                self.assertTrue((math.isnan(a) and math.isnan(b)) or abs(a - b) <= 1e-9 * max(1, abs(a)), f'{col} at {t}: {a} vs {b}')

    def test_detectors_ignore_everything_after_t(self):
        base = flag_df()
        t = len(base) - 1
        future = pd.concat([base, frame(walk(40, 9) * 0.5, 0.05, 9_000_000)]).copy()
        future.index = dates(len(future))
        future.iloc[:t + 1] = base.values                                 # same past, wild future
        a, b = series(base), series(future)
        for fn in (bs.detect_flag, bs.detect_breakout, bs.detect_base):
            ra, rb = fn(a, t), fn(b, t)
            self.assertEqual(bool(ra), bool(rb))
            if ra:
                self.assertEqual(ra['lv'], rb['lv'])

    def test_replay_rows_start_after_the_signal_date(self):
        s = series(frame(walk(100, 2), 0.015, 1e6))
        rows = br.make_rows(s, 41, 71)
        self.assertEqual(rows[0]['date'], s.dates[41])
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(r['date'] > s.dates[40] for r in rows))

    def test_the_harness_uses_the_ledger_replay(self):
        s = series(frame(walk(120, 5, drift=0.003), 0.02, 1e6))
        t, entry = 60, float(s.c[60]) * 1.01
        stop, target = entry * 0.93, entry * 1.1
        rec = {'entry': entry, 'stop': stop, 'target': target, 'entryZoneHigh': entry * 1.01, 'startSession': s.dates[t + 1]}
        self.assertEqual(br.replay_levels(s, t, entry, stop, target), tr.replay(rec, br.make_rows(s, t + 1, t + 31)))

    def test_market_cap_is_estimated_from_the_price_path(self):
        s = series(frame(walk(300, 6), 0.015, 1e6), cap=1e10)
        self.assertAlmostEqual(s.cap_at(len(s.c) - 1), 1e10)
        self.assertAlmostEqual(s.cap_at(200), 1e10 * s.c[200] / s.c[-1])


class Detectors(unittest.TestCase):
    def test_breakout_example_and_non_examples(self):
        s = series(breakout_df())
        t = len(s.c) - 1
        d = bs.detect_breakout(s, t)
        self.assertIsNotNone(d)
        self.assertEqual(d['setup'], 'breakout')
        self.assertIn('2.5x volume', d['why'])
        self.assertGreater(d['lv']['entry'], s.h[t])                        # a buy-stop above the breakout-day high
        self.assertLess(d['lv']['stop'], s.c[t])
        self.assertIsNone(bs.detect_breakout(series(breakout_df(vol_day=1_200_000)), t))        # no volume
        self.assertIsNone(bs.detect_breakout(series(breakout_df(close_day=99.5)), t))           # never closed above the pivot
        self.assertIsNone(bs.detect_breakout(series(breakout_df(close_day=110.0)), t))          # too extended (> 1.5 ATR)

    def test_flag_example_and_non_example(self):
        s = series(flag_df())
        t = len(s.c) - 1
        d = bs.detect_flag(s, t)
        self.assertIsNotNone(d)
        self.assertEqual(d['setup'], 'flag')
        self.assertGreaterEqual(d['flagLen'], 3)
        self.assertLess(d['lv']['stop'], s.c[t])
        self.assertIsNone(bs.detect_flag(series(flag_df(run=False)), t))                          # no prior strong run

    def test_pullback_example_and_non_examples(self):
        s = series(pullback_df())
        t = len(s.c) - 1
        d = bs.detect_pullback(s, t, None, rs_pct=85)
        self.assertIsNotNone(d, (s.ind['rsi14'][t], s.c[t], s.ind['ema21'][t], s.ind['atr14'][t]))
        self.assertEqual(d['setup'], 'pullback')
        self.assertAlmostEqual(d['lv']['entry'], s.h[t] + 0.01, places=2)
        self.assertIsNone(bs.detect_pullback(s, t, None, rs_pct=50))                              # weak relative strength
        down = series(frame(np.linspace(100, 60, 320), 0.012, 1e6))
        self.assertIsNone(bs.detect_pullback(down, 319, None, rs_pct=95))                         # not an uptrend

    def test_base_example_and_non_example(self):
        s = series(base_df())
        t = len(s.c) - 1
        d = bs.detect_base(s, t)
        self.assertIsNotNone(d, bs.argmax_age(s.h, t))
        self.assertEqual(d['setup'], 'base')
        self.assertGreaterEqual(d['tib'], 5)
        self.assertIsNone(bs.detect_base(series(frame(walk(300, 8), 0.02, 1e6)), 299))             # a random walk is not a tight base


class Validity(unittest.TestCase):
    def test_the_stop_is_always_below_the_close_and_the_entry(self):
        for struct in (50.0, 99.0, 120.0, 10.0):                                                    # even a nonsense structure stop above the close
            lv = bs.finalize_levels(101.0, struct, 100.0, 2.0, 3.0)
            self.assertIsNotNone(lv)
            self.assertLess(lv['stop'], 100.0)
            self.assertLessEqual(lv['stop'], 100.0 - 2.0 + 1e-9)                                    # the 1 x ATR14 floor
            self.assertLessEqual(lv['stop'], 100.0 * (1 - 0.5 * 0.03) + 1e-9)                       # the 0.5 x ADR20 floor

    def test_unknown_volatility_excludes_the_name(self):
        self.assertIsNone(bs.finalize_levels(101.0, 95.0, 100.0, None, 3.0))
        self.assertIsNone(bs.finalize_levels(101.0, 95.0, 100.0, 2.0, 0.0))
        self.assertIsNone(bs.finalize_levels(None, 95.0, 100.0, 2.0, 3.0))

    def test_the_universe_filter_excludes_thin_cheap_and_small_names(self):
        ok = series(frame(walk(300, 11) * 1.0, 0.03, 2_000_000))
        self.assertTrue(bs.universe_ok(ok, 299))
        self.assertFalse(bs.universe_ok(series(frame(walk(300, 11), 0.03, 200_000)), 299))        # volume under 500K
        self.assertFalse(bs.universe_ok(series(frame(walk(300, 11) * 0.03, 0.03, 2_000_000)), 299))  # price under $5
        self.assertFalse(bs.universe_ok(series(frame(walk(300, 11), 0.03, 2_000_000), cap=1e9), 299))  # cap under the floor
        self.assertFalse(bs.universe_ok(series(frame(walk(300, 11), 0.004, 2_000_000)), 299))       # ADR under 2%

    def test_composite_renormalises_when_flags_are_unavailable(self):
        self.assertAlmostEqual(bs.composite(80, 60, 50, 40, None), (80 * 30 + 60 * 25 + 50 * 15 + 40 * 15) / 85)
        self.assertAlmostEqual(bs.composite(80, 60, 50, 40, 100), (80 * 30 + 60 * 25 + 50 * 15 + 40 * 15 + 100 * 15) / 100)


class Stats(unittest.TestCase):
    def _it(self, status, R, d='2024-01-02', ex='2024-01-10'):
        return {'date': d, 'o10': {'status': status, 'R': R, 'exitDate': ex}}

    def test_stats_use_the_ledger_definitions(self):
        items = [self._it('target', 1.0), self._it('stopped', -1.0, ex='2024-01-12'), self._it('time-exit', 0.5, ex='2024-01-20'),
                 self._it('expired', None), self._it('invalidated', None)]
        s = rp.stats(items)
        self.assertEqual((s['n'], s['trades']), (5, 3))
        self.assertAlmostEqual(s['trig'], 3 / 5)
        self.assertAlmostEqual(s['win'], 2 / 3)
        self.assertAlmostEqual(s['avgR'], 0.5 / 3)
        self.assertAlmostEqual(s['exp'], 0.5 / 5)                                                # untriggered signals count as 0 R
        self.assertAlmostEqual(s['worstDD'], -1.0)

    def test_split_and_buckets(self):
        tr_, te, last = rp.split_dates([{'date': f'2024-01-{d:02d}'} for d in range(1, 11)])
        self.assertEqual((len(tr_), len(te), last), (6, 4, '2024-01-06'))
        self.assertEqual([rp.bucket(r) for r in (1, 3, 4, 10, 11, None)], ['top 3', 'top 3', '4-10', '4-10', '11+', 'unranked'])


if __name__ == '__main__':
    unittest.main()
