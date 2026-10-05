"""Strength flags for technical picks and catalyst alerts (added 2026-10-05).

A flag is shown ONLY when the evidence is strong; no flag means "not notable", never "bad". Each rule is objective, reads the CONFIG
block below and returns a tooltip with the numbers behind it. If an input is unavailable the flag is NOT shown and the gap is
returned in `unavailable` so the builder can log it in health. Nothing is guessed.

  SECTOR     stock's sector is in today's top 3 sectors AND its industry is in that sector's top 3 by 1M performance AND the stock's
             1M return beats its industry's 1M return.
  FINANCIALS >= 3 of 4: TTM revenue growth >= 15% | TTM EPS growth >= 20% (with a one-off note) | operating margin stable or expanding
             y/y | positive free cash flow. Shown with which passed. If fewer than 3 passed but unknown inputs could still change the
             outcome, nothing is shown and it is logged as undetermined.
  CATALYST   a qualifying catalyst within the last 10 trading days: a playbook type, >= 2 sources, priced-in score <= 60 computed from
             >= 3 components.
"""
import datetime as dt
import market_time as mt

CONFIG = {
    'sector': {'topSectors': 3, 'topIndustries': 3},
    'financials': {'minPass': 3, 'revGrowthPct': 15.0, 'epsGrowthPct': 20.0, 'marginToleranceTotalPct': 0.5},
    'catalyst': {'windowSessions': 10, 'minSources': 2, 'maxPricedIn': 60, 'minComponents': 3,
                 'types': ['earnings beat + raise', 'major contract', 'analyst upgrade', 'approval / launch', 'index add']},
}
LABEL = {'sector': 'Sector', 'financials': 'Financials', 'catalyst': 'Catalyst'}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def ctype_class(ctype):
    """Map a free-text catalyst type onto the playbook classes (None if it is not one of them)."""
    t = (ctype or '').lower()
    if 'index' in t and ('add' in t or 'inclusion' in t or 'join' in t):
        return 'index add'
    if 'earnings' in t or 'guidance' in t or 'beat' in t:
        return 'earnings beat + raise'
    if 'contract' in t or 'deal' in t or 'partnership' in t or 'agreement' in t:
        return 'major contract'
    if 'upgrade' in t:
        return 'analyst upgrade'
    if 'approval' in t or 'launch' in t or 'fda' in t:
        return 'approval / launch'
    return None


def sector_flag(sector, industry, stock_1m, sectors, industries, cfg=None):
    """sectors: [{name,pct}] ; industries: [{sector, items:[{name,pct}]}] best-first (top 3 per sector).
    Returns (flag|None, unavailable|None)."""
    c = (cfg or CONFIG)['sector']
    if not _num(stock_1m):
        return None, 'stock 1M return unavailable'
    ranked = sorted([s for s in sectors or [] if _num(s.get('pct'))], key=lambda s: -s['pct'])
    if not ranked:
        return None, 'sector ranking unavailable'
    top_s = [s['name'] for s in ranked[:c['topSectors']]]
    grp = next((g for g in industries or [] if g.get('sector') == sector), None)
    if sector not in top_s:
        return None, None                                            # not notable (not an unavailable input)
    if not grp:
        return None, 'industry ranking for this sector unavailable'
    its = grp.get('items', [])[:c['topIndustries']]
    hit = next((i for i in its if i.get('name') == industry), None)
    if not hit:
        return None, None
    if not _num(hit.get('pct')):
        return None, 'industry 1M return unavailable'
    if stock_1m <= hit['pct']:
        return None, None
    rank_s = top_s.index(sector) + 1
    rank_i = [i['name'] for i in its].index(industry) + 1
    inputs = {'sector': sector, 'sectorRank': rank_s, 'industry': industry, 'industryRank': rank_i, 'industry1M': hit['pct'], 'stock1M': round(stock_1m, 2)}
    return {'key': 'sector', 'label': LABEL['sector'],
            'tip': f"{sector} is #{rank_s} of the top 3 sectors; {industry} is #{rank_i} in it ({hit['pct']:+.1f}% 1M); the stock is {stock_1m:+.1f}% 1M, ahead of its industry.",
            'inputs': inputs}, None


def financials_flag(f, cfg=None):
    """f: revGrowthPct, epsGrowthPct, epsNote, opMarginPct, opMarginPriorPct, fcf (TTM, any currency units), source.
    Returns (flag|None, unavailable|None)."""
    c = (cfg or CONFIG)['financials']
    tests = {}
    tests['revenue'] = None if not _num(f.get('revGrowthPct')) else f['revGrowthPct'] >= c['revGrowthPct']
    tests['eps'] = None if not _num(f.get('epsGrowthPct')) else f['epsGrowthPct'] >= c['epsGrowthPct']
    tests['margin'] = (None if not (_num(f.get('opMarginPct')) and _num(f.get('opMarginPriorPct')))
                       else f['opMarginPct'] >= f['opMarginPriorPct'] - c['marginToleranceTotalPct'])
    tests['fcf'] = None if not _num(f.get('fcf')) else f['fcf'] > 0
    passed = [k for k, v in tests.items() if v is True]
    unknown = [k for k, v in tests.items() if v is None]
    if len(passed) >= c['minPass']:
        pass
    elif len(passed) + len(unknown) >= c['minPass'] and unknown:
        return None, 'financials undetermined: no data for ' + ', '.join(unknown)
    else:
        return None, None
    names = {'revenue': f"revenue {f.get('revGrowthPct')}% (≥{c['revGrowthPct']:g})", 'eps': f"EPS {f.get('epsGrowthPct')}% (≥{c['epsGrowthPct']:g})",
             'margin': f"op. margin {f.get('opMarginPct')}% vs {f.get('opMarginPriorPct')}% a year ago", 'fcf': 'free cash flow positive'}
    tip = f"{len(passed)} of 4 passed: " + '; '.join(names[k] for k in passed)
    failed = [k for k, v in tests.items() if v is False]
    if failed:
        tip += '. Not passed: ' + ', '.join(names[k] for k in failed)
    if unknown:
        tip += '. No data: ' + ', '.join(unknown)
    if f.get('epsNote'):
        tip += f". EPS note: {f['epsNote']}"
    return {'key': 'financials', 'label': LABEL['financials'], 'tip': tip,
            'inputs': {**{k: f.get(k) for k in ('revGrowthPct', 'epsGrowthPct', 'epsNote', 'opMarginPct', 'opMarginPriorPct', 'fcf', 'source')},
                       'passed': passed, 'unknown': unknown}}, None


def catalyst_flag(cat, priced, last_session, cfg=None):
    """cat: {ctype, cdateISO, sources:[{u}]}; priced: pricedin.score() result. Returns (flag|None, unavailable|None)."""
    c = (cfg or CONFIG)['catalyst']
    cls = ctype_class(cat.get('ctype'))
    if cls not in c['types']:
        return None, None
    try:
        d = dt.date.fromisoformat(cat['cdateISO'])
    except Exception:
        return None, 'catalyst date unavailable'
    window = {x.isoformat() for x in mt.trading_days_back(dt.date.fromisoformat(last_session), c['windowSessions'])}
    if cat['cdateISO'] not in window:
        return None, None
    hosts = {s.get('u', '').split('/')[2] for s in cat.get('sources') or [] if str(s.get('u', '')).startswith('https://')}
    if len(hosts) < c['minSources']:
        return None, None
    if not priced or priced.get('score') is None or priced.get('n', 0) < c['minComponents']:
        return None, f"priced-in score needs ≥{c['minComponents']} components" + ('' if not priced else f" (has {priced.get('n', 0)})")
    if priced['score'] > c['maxPricedIn']:
        return None, None
    return {'key': 'catalyst', 'label': LABEL['catalyst'],
            'tip': f"{cls} on {cat['cdateISO']} ({len(hosts)} sources); {priced['label']}.",
            'inputs': {'class': cls, 'date': cat['cdateISO'], 'sources': len(hosts), 'pricedIn': priced['score'], 'components': priced['n']}}, None
