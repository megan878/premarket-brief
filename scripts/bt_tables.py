"""Report tables from a backtest pickle (default.pkl or final.pkl): per setup, per rank bucket, lists vs baselines, universe options, weight sensitivity.

    python scripts/bt_tables.py out/bt/final.pkl
"""
import collections, itertools, pathlib, pickle, sys
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bt_report as rp

SETUPS = ('base', 'breakout', 'pullback', 'flag')
W0 = (30, 25, 15, 15)


def dates_split(items, frac=0.6):
    return rp.split_dates(items, frac)


def comp(sig, w):
    return (sig['rs'] * w[0] + sig['quality'] * w[1] + sig['trend'] * w[2] + sig['accum'] * w[3]) / sum(w)


def lists(sigs, w=W0, mode='scaled', maxn=10):
    """Per scan date: the list the page would show. 'top10' = always the top 10 primary signals; 'scaled' = 10 in a risk-on market,
    otherwise 5 favouring pullbacks and the highest relative strength."""
    by = collections.defaultdict(list)
    for s in sigs:
        if s['primary']:
            by[s['date']].append(s)
    out = []
    for d, items in sorted(by.items()):
        items = sorted(items, key=lambda s: -comp(s, w))
        risk_on = items[0]['riskOn']
        if mode == 'scaled' and not risk_on:
            pool = [s for s in items if s['setup'] == 'pullback' or s['rsPct'] >= 90]
            sel = pool[:5]
        else:
            sel = items[:maxn]
        for r, s in enumerate(sel, 1):
            out.append({**s, 'listRank': r})
    return out


def section(title):
    print('\n' + '=' * 6, title)


def per_setup(sigs, tr_d, te_d):
    section('PER SETUP TYPE (all signals of that type; target models: +10% / 2R)')
    for su in SETUPS:
        it = [s for s in sigs if s['setup'] == su]
        for nm, dset in (('train', tr_d), ('TEST ', te_d)):
            sub = [s for s in it if s['date'] in dset]
            print(f'{su:9} {nm} +10%: {rp.fmt(rp.stats(sub, "o10"))}')
            print(f'{"":9} {nm} 2R  : {rp.fmt(rp.stats(sub, "o2R"))}')


def per_rank(sigs, tr_d, te_d, w=W0):
    section('RANK BUCKETS (primary signal per ticker, ranked by the composite on each scan date; 2R target)')
    by = collections.defaultdict(list)
    for s in sigs:
        if s['primary']:
            by[s['date']].append(s)
    ranked = []
    for d, items in by.items():
        for r, s in enumerate(sorted(items, key=lambda s: -comp(s, w)), 1):
            ranked.append({**s, 'r': r})
    for nm, dset in (('train', tr_d), ('TEST ', te_d)):
        for b in ('top 3', '4-10', '11+'):
            sub = [s for s in ranked if s['date'] in dset and rp.bucket(s['r']) == b]
            print(f'{nm} {b:6} 2R  : {rp.fmt(rp.stats(sub, "o2R"))}')
            print(f'{"":5} {"":6} +10%: {rp.fmt(rp.stats(sub, "o10"))}')


def vs_baselines(sigs, base, tr_d, te_d):
    section('THE LIST vs BASELINES (what a reader would have been shown on each scan date)')
    L = lists(sigs)
    for nm, dset in (('train', tr_d), ('TEST ', te_d)):
        print(f'-- {nm}')
        sub = [s for s in L if s['date'] in dset]
        print(f'  new list (scaled, <=10)  2R  : {rp.fmt(rp.stats(sub, "o2R"))}')
        print(f'  new list (scaled, <=10)  +10%: {rp.fmt(rp.stats(sub, "o10"))}')
        L10 = [s for s in lists(sigs, mode='top10') if s['date'] in dset]
        print(f'  new list (always top 10) 2R  : {rp.fmt(rp.stats(L10, "o2R"))}')
        for b in ('top4-v1', 'quality-max10-v2'):
            x = [p for p in base if p['baseline'] == b and p['date'] in dset]
            print(f'  {b:24} +10%: {rp.fmt(rp.stats(x, "o10"))}')
        x = [p for p in base if p['baseline'] == 'random' and p['date'] in dset]
        print(f'  random 5 (same universe) +10%: {rp.fmt(rp.stats(x, "o10"))}')
        print(f'  random 5 (same universe) 2R  : {rp.fmt(rp.stats(x, "o2R"))}')


def universes(sigs, tr_d, te_d):
    section('UNIVERSE OPTIONS (2R target; list = scaled top 10 built from the subset)')
    opts = {'all industries': lambda s: True, 'top-15 industries (1M+3M momentum)': lambda s: s['topIndustry'],
            'top-15 industries + RS>=90 outside': lambda s: s['topIndustry'] or s['rsPct'] >= 90,
            'cap >= $5B': lambda s: s['cap'] >= 5e9, 'cap $3-5B only': lambda s: 3e9 <= s['cap'] < 5e9}
    for nm, f in opts.items():
        sub = [s for s in sigs if f(s)]
        # re-mark primary within the subset
        by = collections.defaultdict(dict)
        for s in sorted(sub, key=lambda s: -s['composite']):
            by[s['date']].setdefault(s['tk'], s)
        re = [dict(v, primary=True) for d in by.values() for v in d.values()]
        L = lists(re)
        for tn, dset in (('train', tr_d), ('TEST ', te_d)):
            x = [s for s in L if s['date'] in dset]
            print(f'{nm:38} {tn} list: {rp.fmt(rp.stats(x, "o2R"))}')


def weights(sigs, tr_d, te_d):
    section('COMPOSITE WEIGHTS (rs, setup quality, trend, accumulation; flags weight is 0 in the backtest) - top-10 list, 2R target')
    sets = {'30/25/15/15 (start)': (30, 25, 15, 15), 'RS-heavy 50/20/15/15': (50, 20, 15, 15), 'quality-heavy 20/45/15/20': (20, 45, 15, 20),
            'equal 25/25/25/25': (25, 25, 25, 25), 'RS only': (100, 0, 0, 0), 'quality only': (0, 100, 0, 0), 'trend+accum 0/0/50/50': (0, 0, 50, 50)}
    ref = {(s['date'], s['tk']) for s in lists(sigs, W0, 'top10')}
    for nm, w in sets.items():
        L = lists(sigs, w, 'top10')
        cur = {(s['date'], s['tk']) for s in L}
        jac = len(ref & cur) / max(1, len(ref | cur))
        tr_ = [s for s in L if s['date'] in tr_d]
        te_ = [s for s in L if s['date'] in te_d]
        print(f'{nm:28} overlap with start top-10 {jac * 100:3.0f}% | train {rp.fmt(rp.stats(tr_, "o2R"))}')
        print(f'{"":28}                            | TEST  {rp.fmt(rp.stats(te_, "o2R"))}')


def regimes(sigs, tr_d, te_d):
    section('MARKET REGIME (proxy: SPY > SMA50 and SPY 20d realised vol < 20%)')
    L = lists(sigs, mode='top10')
    for nm, dset in (('train', tr_d), ('TEST ', te_d)):
        on = [s for s in L if s['date'] in dset and s['riskOn']]
        off = [s for s in L if s['date'] in dset and not s['riskOn']]
        print(f'{nm} risk-on  top10 2R: {rp.fmt(rp.stats(on, "o2R"))}')
        print(f'{nm} risk-off top10 2R: {rp.fmt(rp.stats(off, "o2R"))}')
        offp = [s for s in off if s['setup'] == 'pullback' or s['rsPct'] >= 90]
        print(f'{nm} risk-off pullback/RS>=90 2R: {rp.fmt(rp.stats(offp, "o2R"))}')


def main(path):
    d = pickle.loads(pathlib.Path(path).read_bytes())
    sigs, base = d['signals'], d.get('baselines', [])
    alld = [s['date'] for s in sigs] + [p['date'] for p in base]
    tr_d, te_d, last_train = rp.split_dates([{'date': x} for x in alld])
    print(f'signals {len(sigs)} | baseline picks {len(base)} | scan dates {len(tr_d) + len(te_d)} (train {len(tr_d)} to {last_train}, test {len(te_d)})')
    print('params:', d.get('chosen') or 'starting values')
    per_setup(sigs, tr_d, te_d)
    per_rank(sigs, tr_d, te_d)
    vs_baselines(sigs, base, tr_d, te_d)
    universes(sigs, tr_d, te_d)
    weights(sigs, tr_d, te_d)
    regimes(sigs, tr_d, te_d)


if __name__ == '__main__':
    main(sys.argv[1])
