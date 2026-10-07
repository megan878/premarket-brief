"""Backtest driver: default scan, baselines, walk-forward tuning on the first 60% of scan dates, untouched evaluation on the last 40%.

    python scripts/bt_analysis.py default --world out/bt/world.pkl            # scan + baselines with the starting values  -> out/bt/default.pkl
    python scripts/bt_analysis.py tune    --world out/bt/world.pkl            # one-at-a-time grid on the TRAIN dates only -> out/bt/tune.json
    python scripts/bt_analysis.py final   --world out/bt/world.pkl            # tuned params on all dates; train vs TEST report
"""
import argparse, copy, json, pathlib, pickle, sys, time
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bt_signals as bs, bt_run as br, bt_base as bb, bt_report as rp

OUT = pathlib.Path('out/bt')
GRID = {   # one-at-a-time alternatives around the starting values (a key not listed keeps its start value)
    'a': [('minReadiness', [45, 55, 65, 75]), ('maxBelowPivotPct', [3.0, 5.0, 7.0])],
    'b': [('volMult', [1.2, 1.5, 2.0, 2.5]), ('maxExtAtr', [1.0, 1.5, 2.5]), ('closeTopFrac', [0.2, 0.3, 0.5])],
    'c': [('minRsPct', [60, 70, 80, 90]), ('rsiHi', [50, 55, 62]), ('depthHi', [6.0, 8.0, 12.0])],
    'd': [('minRun', [15.0, 20.0, 30.0, 40.0]), ('contraction', [0.6, 0.75, 0.9]), ('volFall', [0.7, 0.85, 1.0])],
}
MIN_N = 40


def world_of(path):
    return pickle.loads(pathlib.Path(path).read_bytes())


def idx_split(world, step=5, first=240, frac=0.6):
    n = len(world['cal'])
    idx = list(range(first, n - 31, step))
    k = int(len(idx) * frac)
    return idx, set(idx[:k]), set(idx[k:])


def cmd_default(a):
    world = world_of(a.world)
    P = copy.deepcopy(bs.PARAMS)
    t0 = time.time()
    sigs = br.scan(world, P, a.step)
    print(f'scan: {len(sigs)} signals in {time.time() - t0:.0f}s')
    t0 = time.time()
    base = bb.run_baselines(world, P, a.step)
    print(f'baselines: {len(base)} picks in {time.time() - t0:.0f}s')
    (OUT / 'default.pkl').write_bytes(pickle.dumps({'params': P, 'signals': sigs, 'baselines': base, 'cal': world['cal']}, protocol=4))


def cmd_tune(a):
    world = world_of(a.world)
    idx, train, test = idx_split(world, a.step)
    res = {}
    base_P = copy.deepcopy(bs.PARAMS)
    for det, items in GRID.items():
        for key, vals in items:
            for v in vals:
                P = copy.deepcopy(base_P)
                P[det][key] = v
                name = {'a': 'base', 'b': 'breakout', 'c': 'pullback', 'd': 'flag'}[det]
                sigs = br.scan(world, P, a.step, only=train, setups={name})
                st = rp.stats(sigs, 'o2R')
                res[f'{det}.{key}={v}'] = st
                print(f'{det}.{key}={v}: ' + rp.fmt(st), flush=True)
    (OUT / 'tune.json').write_text(json.dumps(res, default=float), encoding='utf-8')


def best_params(tune):
    """Per detector and key: the value with the best TRAIN expectancy per signal among alternatives with n >= MIN_N (ties keep the start value)."""
    P = copy.deepcopy(bs.PARAMS)
    chosen = {}
    for det, items in GRID.items():
        for key, vals in items:
            cand = [(tune[f'{det}.{key}={v}'], v) for v in vals if tune[f'{det}.{key}={v}'].get('n', 0) >= MIN_N]
            if not cand:
                continue
            start = P[det][key]
            best = max(cand, key=lambda x: (x[0]['exp'] if x[0]['exp'] == x[0]['exp'] else -9, x[1] == start))
            # only move off the start value when it beats it by more than one standard error (guards against picking noise)
            cur = next((c for c in cand if c[1] == start), None)
            if cur and best[1] != start and best[0]['exp'] - cur[0]['exp'] <= max(cur[0]['se'] if cur[0]['se'] == cur[0]['se'] else 0, 0.03):
                best = cur
            P[det][key] = best[1]
            chosen[f'{det}.{key}'] = best[1]
    return P, chosen


def cmd_final(a):
    world = world_of(a.world)
    tune = json.loads((OUT / 'tune.json').read_text(encoding='utf-8'))
    P, chosen = best_params(tune)
    print('tuned values (train only):', chosen)
    sigs = br.scan(world, P, a.step)
    base = bb.run_baselines(world, P, a.step)
    (OUT / 'final.pkl').write_bytes(pickle.dumps({'params': P, 'chosen': chosen, 'signals': sigs, 'baselines': base, 'cal': world['cal']}, protocol=4))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['default', 'tune', 'final']); ap.add_argument('--world', required=True); ap.add_argument('--step', type=int, default=5)
    a = ap.parse_args(argv)
    return {'default': cmd_default, 'tune': cmd_tune, 'final': cmd_final}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
