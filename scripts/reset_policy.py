"""What a "Reset scan" clears and what it keeps (added 2026-10-05). ONE list: the code, CLAUDE.md and the page all read it.

CLEAR = discard as authoritative and rebuild from fresh sources. If a source fails, the last good copy is shown marked STALE, never blank.
KEEP  = never wiped by a reset. A reset must never delete or rewrite a ledger record.
`tests/test_reset.py` checks this list against CLAUDE.md and `verify()` against before/after snapshots.
"""
import hashlib, json, pathlib

CLEAR = [
    {'id': 'sectors', 'keys': ['sectors', 'sectorSource'], 'what': 'sector ranking'},
    {'id': 'industries', 'keys': ['industries'], 'what': 'industry drill-down'},
    {'id': 'picks', 'keys': ['picks', 'pickWindow', 'selection'], 'what': 'technical picks (and the selection summary)'},
    {'id': 'nearmiss', 'keys': ['nearmiss'], 'what': 'near-misses and their watchlist rows'},
    {'id': 'catalysts-expired', 'keys': [], 'what': 'catalyst alerts older than 5 trading days'},
    {'id': 'stale-flags', 'keys': [], 'what': 'stale health flags (recomputed by the new build)'},
]
KEEP = [
    {'id': 'ledger', 'path': 'data/picks.json', 'what': 'the pick ledger and the Track record section'},
    {'id': 'provenance', 'path': 'data/provenance/', 'what': 'provenance files'},
    {'id': 'catalysts-live', 'keys': ['catalysts'], 'what': 'catalyst alerts still inside their 5-day window (re-priced, not dropped)'},
    {'id': 'playbook', 'what': 'the Catalyst Playbook / Reference section'},
    {'id': 'regime-rules', 'what': 'regime-score rules, config values and the design'},
]
CLEAR_KEYS = sorted({k for c in CLEAR for k in c['keys']})
KEEP_KEYS = sorted({k for c in KEEP for k in c.get('keys', [])})


def render_markdown():
    """The block that CLAUDE.md must contain verbatim between the RESET markers."""
    lines = ['CLEAR and rebuild:'] + [f"- {c['what']}" for c in CLEAR] + ['KEEP (never wiped by a reset):'] + [f"- {k['what']}" for k in KEEP]
    return '\n'.join(lines)


def file_digest(path):
    p = pathlib.Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def prepare(prev, ledger_path=None, catalyst_window=None):
    """Plan a reset from the previous published block. Returns {'drop': keys removed from the working data, 'keep': ...,
    'repriceCatalysts': [tk still in window], 'expireCatalysts': [tk older than the window], 'ledgerDigest': sha256}."""
    prev = prev or {}
    window = set(catalyst_window or [])
    cats = prev.get('catalysts') or []
    live = [c['tk'] for c in cats if not window or c.get('cdateISO') in window]
    dead = [c['tk'] for c in cats if window and c.get('cdateISO') not in window]
    return {'drop': [k for k in CLEAR_KEYS if k in prev], 'keep': [k for k in KEEP_KEYS if k in prev],
            'repriceCatalysts': live, 'expireCatalysts': dead,
            'ledgerDigest': file_digest(ledger_path) if ledger_path else None}


def verify(plan, new_block, ledger_path=None, ledger_before=None):
    """Violations of the policy after a reset build: [] means the reset respected the keep list."""
    bad = []
    if ledger_path is not None and ledger_before is not None:
        # append-only is allowed (new records for newly published picks); rewriting or removing existing records is not
        try:
            before = json.loads(ledger_before)['records'] if isinstance(ledger_before, str) else ledger_before['records']
            after = json.loads(pathlib.Path(ledger_path).read_text(encoding='utf-8'))['records']
        except Exception as e:                                   # noqa: BLE001
            return [f'ledger unreadable after reset: {e}']
        by_id = {r['id']: r for r in after}
        for r in before:
            a = by_id.get(r['id'])
            if a is None:
                bad.append(f"ledger record {r['id']} was removed")
            elif r.get('status') in ('stopped', 'target', 'time-exit', 'invalidated', 'expired', 'skipped', 'replaced') and \
                    any(a.get(k) != r.get(k) for k in ('status', 'exitPrice', 'exitDate', 'rMultiple', 'fillPrice')):
                bad.append(f"terminal ledger record {r['id']} was changed")
    live = set(plan.get('repriceCatalysts') or [])
    got = {c['tk'] for c in (new_block.get('catalysts') or [])}
    for tk in sorted(live - got):
        bad.append(f'catalyst {tk} was inside its 5-day window but was dropped')
    if plan.get('expireCatalysts') and (got & set(plan['expireCatalysts'])):
        bad.append('catalysts older than the window were kept: ' + ', '.join(sorted(got & set(plan['expireCatalysts']))))
    if not new_block.get('tracker'):
        bad.append('the Track record section is missing from the rebuilt page')
    return bad
