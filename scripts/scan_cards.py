"""Pick cards for the names that qualified: every field the page needs, computed from the scan data (no hand-typed numbers).

    python scripts/scan_cards.py --selection out/scan/selection.json --ohlc out/scan/ohlc.json --market out/scan/market.json \\
        --descriptions out/scan/descriptions.json --out out/scan/cards.json [--fetch-financials]

`descriptions.json` = {TK: "one-line company description"} (FMP company profile text, shortened by hand once per new name).
The card's `closes` is the validated 25-session window the readiness was computed on (tracker.validate_rows), never hand-assembled.
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import tracker as tr, flags as fl, scan_build as sb, scan_fetch as sf, selection as sel


def money(v):
    return f'${v / 1e12:.2f}T' if v >= 1e12 else f'${v / 1e9:.1f}B'


def vol_txt(v):
    return f'{v / 1e6:.2f}M' if v >= 1e6 else f'{v / 1e3:.0f}K'


def short_date(iso):
    import datetime as dt
    d = dt.date.fromisoformat(iso)
    return f'{d.day} {d.strftime("%b")}'


def card(c, rows, market, desc, fin=None):
    rows = [r for r in tr.sort_rows(rows) if r['date'] <= c['lastSession']]
    last = rows[-1]
    entry, stop, target = c['entry'], c['stop'], c['target']
    below = (c['pivot'] - c['lastClose']) / c['pivot'] * 100
    sma = lambda n: sum(r['close'] for r in rows[-n:]) / n          # noqa: E731
    vw = [100 * (r['high'] / r['low'] - 1) for r in rows[-5:]]
    vm = [100 * (r['high'] / r['low'] - 1) for r in rows[-20:]]
    lvl = sb.round_level(entry, target)
    rd = c['readiness']
    st = c.get('stats') or {}
    ne = c.get('nextEarnings')
    n_to_e = sel.sessions_until(c['lastSession'], ne) if ne and ne > c['lastSession'] else None
    why = (f"Readiness {rd['composite']} (tightness {rd['tightness']}, proximity {rd['proximity']}, volume dry-up {rd['volumeDryUp']}, time in base {rd['timeInBase']}). "
           f"Closed {below:.1f}% under its {short_date(c['pivotDate'])} high after {c['timeInBaseDays']} sessions of base. "
           f"Levels: stop {c['stopNote']} ({stop:.2f}, risk {c['riskPct']:.1f}% of the entry), fixed +10% target, R:R {c['rr']:.2f}. "
           f"Honest caveats: ADX(14) is {c['adx14']:.1f}" + (' (below the 20 the full scan asks for)' if c['adx14'] < 20 else '') +
           (", and price is above EMA9 (not in the EMA9 > price > EMA21 zone)" if not c['emaCompression'] else '') +
           f"; 20-day average volume {vol_txt(c['avgVol20'])} against the 500K floor."
           + (f" Earnings {short_date(ne)} is {n_to_e} sessions away (after the 20-session maximum hold)." if n_to_e and n_to_e > 20 else
              (f" Earnings {short_date(ne)} is {n_to_e} sessions away." if n_to_e else '')))
    pk = {'tk': c['tk'], 'name': c['name'], 'industry': c['industry'], 'sector': c['sector'],
          'setup': f"{c['timeInBaseDays']}-session base under the {short_date(c['pivotDate'])} high, {below:.1f}% below it",
          'tags': 'above SMA20/50/200 (stockanalysis)', 'pivot': c['pivot'], 'entryZone': f'{entry:.2f} – {entry * 1.01:.2f}',
          'entry': entry, 'stop': stop, 'stopNote': f"{c['stopNote']} (last low {c['lastLow']:.2f})", 'target': target,
          'why': why, 'trim': (f'Round number ${lvl:g} sits between entry and target — trim 25% there.' if lvl else 'Trim 25% at the first resistance on the way to the target.'),
          'cap': money(c['cap']), 'avgVol': vol_txt(c['avgVol20']),
          'sma': [round((c['lastClose'] / sma(20) - 1) * 100, 2), round((c['lastClose'] / sma(50) - 1) * 100, 2), round((c['lastClose'] / c['sma200'] - 1) * 100, 2)],
          'rsi': sf.num(st.get('Relative Strength Index (RSI)')), 'volW': round(sum(vw) / 5, 2), 'volM': round(sum(vm) / 20, 2),
          'px': c['lastClose'], 'pct': c['lastDayPct'], 'closes': c['closes'], 'readiness': rd, 'atr14': c['atr14'], 'adr20Pct': c['adr20Pct'],
          'emaCompression': c['emaCompression'], 'adx14': round(c['adx14'], 1), 'scoringVersion': 'v2'}
    flags, log = [], []
    f, u = fl.sector_flag(c['sector'], c['industry'], c.get('ret1m'), market['sectors'], market['industries'])
    (flags.append(f) if f else None); (log.append(f"sector: {u}") if u else None)
    if fin is not None:
        fin = {**fin, 'fcf': sf.num(str(st.get('Free Cash Flow', '')).replace('$', ''))}
        f, u = fl.financials_flag(fin)
        (flags.append(f) if f else None); (log.append(f"financials: {u}") if u else None)
    else:
        log.append('financials: statements not fetched')
    log.append('catalyst: no catalyst search was run for technical picks')
    pk['flags'] = flags
    bg = {'cap': money(c['cap']), 'what': desc, 'deal': []}
    if fin is not None and fin.get('revGrowthPct') is not None:
        bg.update(rev=st.get('Revenue'), revG=fin['revGrowthPct'], eps=st.get('Earnings Per Share (EPS)'), epsG=fin.get('epsGrowthPct'))
    if ne:
        bg.update(next=short_date(ne), nextWarn=bool(n_to_e is not None and n_to_e <= 20))
    return pk, bg, log


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--selection', required=True); ap.add_argument('--ohlc', required=True); ap.add_argument('--market', required=True)
    ap.add_argument('--descriptions', required=True); ap.add_argument('--out', required=True); ap.add_argument('--fetch-financials', action='store_true')
    a = ap.parse_args(argv)
    sl = json.loads(pathlib.Path(a.selection).read_text(encoding='utf-8'))
    oh = json.loads(pathlib.Path(a.ohlc).read_text(encoding='utf-8'))['tickers']
    market = json.loads(pathlib.Path(a.market).read_text(encoding='utf-8'))
    desc = json.loads(pathlib.Path(a.descriptions).read_text(encoding='utf-8'))
    out = {'picks': [], 'bg': {}, 'flagLog': []}
    for rank, c in enumerate(sl['picks'], 1):
        fin = sf.fetch_financials(c['tk']) if a.fetch_financials else None
        pk, bg, log = card(c, oh[c['tk']], market, desc.get(c['tk'], ''), fin)
        pk.update(selectionRank=rank, selectionVersion=sel.SELECTION_VERSION, tier=c.get('tier', 'A'), chips=c.get('chips', []))
        out['picks'].append(pk); out['bg'][c['tk']] = bg
        out['flagLog'] += [f"{c['tk']}: {m}" for m in log]
    pathlib.Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding='utf-8')
    print(f"{len(out['picks'])} card(s) -> {a.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
