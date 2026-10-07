"""What the new scan lists on the latest stored session (dry run; nothing is written to the page or the ledger).

    python scripts/bt_today.py --world out/bt/world.pkl [--params out/bt/final.pkl] [--risk-pct 1.0]
"""
import argparse, copy, json, pathlib, pickle, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bt_signals as bs, bt_run as br, bt_tables as bt


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--world', required=True); ap.add_argument('--params'); ap.add_argument('--risk-pct', type=float, default=1.0)
    ap.add_argument('--top', type=int, default=25); ap.add_argument('--out')
    a = ap.parse_args(argv)
    world = pickle.loads(pathlib.Path(a.world).read_bytes())
    P = copy.deepcopy(bs.PARAMS)
    if a.params:
        P = pickle.loads(pathlib.Path(a.params).read_bytes())['params']
    t = len(world['cal']) - 1
    day = br.signals_at(world, P, t)
    risk_on, rv = br.regime(world, t)
    print(f"as of {world['cal'][t]} | regime proxy: {'RISK-ON' if risk_on else 'RISK-OFF'} (SPY realised vol {rv:.1f}%) | {len(day)} signals, {len({d['tk'] for d in day})} names")
    for d in day:
        d['_sort'] = bt.comp(d, bt.W0)
    prim = sorted([d for d in day if d['primary']], key=lambda d: -d['_sort'])
    shown = prim[:10] if risk_on else [d for d in prim if d['setup'] == 'pullback' or d['rsPct'] >= 90][:5]
    print(f'list size: {len(shown)} ({"up to 10" if risk_on else "up to 5, pullbacks / RS>=90 first"})')
    print(f"{'#':>2} {'tk':5} {'setup':9} {'comp':>5} {'RS%':>4} {'entry':>8} {'stop':>8} {'risk%':>6} {'2R tgt':>8} {'3R':>8} {'size%acct':>9}  why")
    rows = []
    for i, d in enumerate(shown, 1):
        lv = d['lv']
        size = a.risk_pct / lv['riskPct'] * 100
        print(f"{i:>2} {d['tk']:5} {d['setup']:9} {d['_sort']:5.1f} {d['rsPct']:4.0f} {lv['entry']:8.2f} {lv['stop']:8.2f} {lv['riskPct']:6.2f} {lv['target2R']:8.2f} {lv['target3R']:8.2f} {size:8.1f}%  {d['why']}")
        rows.append({**{k: d[k] for k in ('tk', 'setup', 'rsPct', 'why', 'trigger', 'invalidation', 'industry', 'cap')}, 'composite': d['_sort'], 'lv': lv, 'sizePctOfAccount': size})
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(rows, default=float, indent=1), encoding='utf-8')
    print('\nnext in line (primary signals ranked beyond the list):')
    for d in prim[len(shown):len(shown) + 8]:
        print(f"   {d['tk']:5} {d['setup']:9} comp {d['_sort']:5.1f} RS {d['rsPct']:3.0f}  {d['why']}")
    print('\nper setup type today:', {su: sum(1 for d in day if d['setup'] == su) for su in bt.SETUPS})
    return 0


if __name__ == '__main__':
    sys.exit(main())
