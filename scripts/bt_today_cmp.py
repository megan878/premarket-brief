"""Side by side: today's list under the new rules vs the current rules (quality-max10-v2 + Tier B logic), with real earnings dates from stockanalysis for the names shown."""
import copy, json, math, pathlib, pickle, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import selection as sel, tracker as tr, scan_fetch as sf, scan_build as sb
import bt_signals as bs, bt_run as br, bt_base as bb


def earn(tk):
    try:
        return sb.parse_date((sf.fetch_stats(tk) or {}).get('Earnings Date', ''))
    except Exception:                                              # noqa: BLE001
        return None


def main():
    world = pickle.loads(pathlib.Path('out/bt/world.pkl').read_bytes())
    fin = pickle.loads(pathlib.Path('out/bt/final.pkl').read_bytes())
    P = fin['params']
    t = len(world['cal']) - 1
    last = world['cal'][t]
    new = json.loads(pathlib.Path('out/bt/today.json').read_text(encoding='utf-8'))
    print('NEW RULES, top 10, earnings checked (blackout = report within 3 sessions after', last + ')')
    for r in new:
        ne = earn(r['tk'])
        n = sel.sessions_until(last, ne) if ne and ne > last else None
        flag = f'  <-- EARNINGS IN {n} SESSION(S)' if n is not None and n <= 3 else ('  (earnings date not found)' if not ne else '')
        print(f"  {r['tk']:5} {r['setup']:9} earnings {ne}{flag}")
    # current rules on the same data: candidates = names in the 9 industries that pass the universe/trend filters, scored v2
    nine = {tk for tk, s in world['series'].items() if s.meta['industry'] in __import__('bt_diagnose').NINE}
    cands = []
    for tk in nine:
        s = world['series'][tk]
        if not bs.universe_ok(s, t, P):
            continue
        I = s.ind
        c = s.c[t]
        if not (c > I['sma20'][t] and c > I['sma50'][t] and c > I['sma200'][t]) or c > 1.2 * I['sma50'][t]:
            continue
        win = bs.rows_window(s, t)
        if any(math.isnan(r['close']) for r in win):
            continue
        rd = tr.readiness_v2(win)
        pivot = rd['pivot']
        cands.append({'tk': tk, 'readiness': {'composite': rd['composite'], 'proximity': rd['proximity']}, 'timeInBaseDays': rd['timeInBaseDays'], 'lastClose': c,
                      'pivot': pivot, 'entry': round(pivot + 0.01, 2), 'lastLow': s.l[t], 'atr14': I['atr14'][t], 'adr20Pct': I['adr20'][t], 'lastSession': last,
                      'nextEarnings': None, 'industry': s.meta['industry']})
    # earnings only for names that reach readiness 45 (saves fetches)
    for c in cands:
        if c['readiness']['composite'] >= 45:
            c['nextEarnings'] = earn(c['tk'])
        else:
            c['nextEarnings'] = '2099-01-01'
    r = sel.select_tiered(cands)
    print(f"\nCURRENT RULES (tiered-v3 logic; 9 industries; {len(cands)} names pass the universe filters) -> Tier A {r['tierA']}, Tier B {r['tierB']}")
    for p in r['picks']:
        print(f"  {p['tier']} {p['tk']:5} readiness {p['readiness']['composite']} entry {p['entry']} stop {p['stop']} risk {p['riskPct']}% R:R {p['rr']}  chips {[c['label'] for c in p['chips']]}")
    print('  hard-rejected for Tier B (validity):', [(x['tk'], x['hardFailed']) for x in r['bRejected'] if x['tk'] in {c['tk'] for c in cands if c['readiness']['composite'] >= 45}][:20])


main()
