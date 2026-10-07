"""Extra dry-run tests: market-cap floor $3B vs $5B (re-scans with the floor changed) and a block bootstrap of list-vs-random on the untouched test dates.

    python scripts/bt_extra.py --world out/bt/world.pkl --final out/bt/final.pkl
"""
import argparse, collections, copy, pathlib, pickle, random, sys
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bt_signals as bs, bt_run as br, bt_report as rp, bt_tables as bt


def relist(sigs):
    by = collections.defaultdict(dict)
    for s in sorted(sigs, key=lambda s: -s['composite']):
        by[s['date']].setdefault(s['tk'], s)
    return bt.lists([dict(v, primary=True) for d in by.values() for v in d.values()])


def boot(a_by_date, b_by_date, dates, key, n=2000, seed=5):
    """Mean difference in R per trade (a - b) with a date-block bootstrap (dates resampled, so overlapping windows stay together)."""
    rng = random.Random(seed)
    dates = list(dates)

    def mean_r(by, ds):
        rs = [o[key]['R'] for d in ds for o in by.get(d, []) if o[key]['R'] is not None and o[key]['status'] in ('stopped', 'target', 'time-exit')]
        return float(np.mean(rs)) if rs else float('nan')
    diffs = []
    for _ in range(n):
        ds = [rng.choice(dates) for _ in dates]
        diffs.append(mean_r(a_by_date, ds) - mean_r(b_by_date, ds))
    diffs = np.array([x for x in diffs if x == x])
    obs = mean_r(a_by_date, dates) - mean_r(b_by_date, dates)
    return obs, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--world', required=True); ap.add_argument('--final', required=True)
    a = ap.parse_args(argv)
    fin = pickle.loads(pathlib.Path(a.final).read_bytes())
    world = pickle.loads(pathlib.Path(a.world).read_bytes())
    alld = [x['date'] for x in fin['signals']] + [x['date'] for x in fin['baselines']]
    tr_d, te_d, _ = rp.split_dates(alld)
    print('====== MARKET-CAP FLOOR (rescan with the floor changed; list = scaled top 10; 2R target)')
    P3 = copy.deepcopy(fin['params'])
    P3['universe']['minCap'] = 3e9
    sig3 = br.scan(world, P3, 5)
    for nm, f in (('cap >= $3B (all)', lambda s: True), ('cap >= $5B', lambda s: s['cap'] >= 5e9), ('cap $3-5B only', lambda s: s['cap'] < 5e9)):
        L = relist([s for s in sig3 if f(s)])
        for tn, dset in (('train', tr_d), ('TEST ', te_d)):
            x = [s for s in L if s['date'] in dset]
            print(f'{nm:20} {tn} list: {rp.fmt(rp.stats(x, "o2R"))}')
    print('\n====== BLOCK BOOTSTRAP on the TEST dates (mean R per trade, difference, 95% interval over scan dates)')
    L = [s for s in bt.lists(fin['signals']) if s['date'] in te_d]
    Lt = collections.defaultdict(list)
    for s in L:
        Lt[s['date']].append(s)
    base = fin['baselines']
    rnd = collections.defaultdict(list)
    q2 = collections.defaultdict(list)
    t4 = collections.defaultdict(list)
    for p in base:
        if p['date'] not in te_d:
            continue
        {'random': rnd, 'quality-max10-v2': q2, 'top4-v1': t4}[p['baseline']][p['date']].append(p)
    for nm, other, key in (('new list vs random-5 (2R)', rnd, 'o2R'),):
        print(f'{nm:34}: diff {boot(Lt, other, te_d, key)}')
    q10 = collections.defaultdict(list)
    for d, v in Lt.items():
        q10[d] = [{'o10': s['o10']} for s in v]
    print(f'{"new list vs quality-v2 (+10%)":34}: diff {boot(q10, q2, te_d, "o10")}')
    print(f'{"new list vs top4-v1 (+10%)":34}: diff {boot(q10, t4, te_d, "o10")}')
    for su in bt.SETUPS:
        by = collections.defaultdict(list)
        for s in fin['signals']:
            if s['setup'] == su and s['date'] in te_d:
                by[s['date']].append(s)
        print(f'{su:10} vs random-5 (2R): diff {boot(by, rnd, te_d, "o2R")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
