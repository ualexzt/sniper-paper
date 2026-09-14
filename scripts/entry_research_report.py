"""Read-only entry diagnostics. Run against a consistent SQLite backup."""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from sniper_paper.entry_research import VERSION, markout


def report(database, since):
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    legacy = [dict(r) for r in db.execute(
        "SELECT symbol,status,reason,count(*) n FROM signals WHERE occurred_at_ms>=? "
        "GROUP BY symbol,status,reason", (since,))]
    rows = list(db.execute(
        "SELECT * FROM shadow_diagnostics WHERE lane=? AND occurred_at_ms>=? ORDER BY occurred_at_ms",
        (VERSION, since)))
    output, seen = [], set()
    cache = {}
    for row in rows:
        f = json.loads(row['features_json'])
        if not f['gates']['geometry']:
            continue
        # First geometry observation per episode/type: avoid overweighting long episodes.
        key = (row['protocol_hash'], row['setup_id'], row['reason'])
        if key in seen:
            continue
        seen.add(key)
        symbol = row['symbol']
        if symbol not in cache:
            cache[symbol] = [dict(r) for r in db.execute(
                "SELECT * FROM bars WHERE symbol=? AND timeframe='15s' AND opened_at_ms>=? ORDER BY opened_at_ms",
                (symbol, since))]
        output.append({
            'symbol': symbol, 'episode_id': row['setup_id'], 'protocol_hash': row['protocol_hash'],
            'observed_at_ms': row['occurred_at_ms'], 'kind': row['reason'], 'side': row['side'],
            'gates': f['gates'], 'readiness': f['readiness'],
            'markouts': {str(seconds): markout(cache[symbol], row['occurred_at_ms'],
                        f['reference_mid'], row['side'], seconds * 1000) for seconds in (15, 60, 180, 300)},
        })
    db.close()
    comparisons = {}
    for removed in (None, 'delta', 'microprice', 'imbalance', 'depth', 'quote'):
        selected = [r for r in output if r['readiness'].get('ready') and all(
            v is True for k, v in r['gates'].items() if k != removed)]
        comparisons['all_gates' if removed is None else 'without_' + removed] = {
            'episodes': len(selected),
            'horizons': {str(seconds): {
                'available': len(usable),
                'mean_signed_return_bp': sum(x['signed_return_bp'] for x in usable) / len(usable) if usable else None,
                'mean_mfe_bp': sum(x['mfe_bp'] for x in usable) / len(usable) if usable else None,
                'mean_mae_bp': sum(x['mae_bp'] for x in usable) / len(usable) if usable else None,
            } for seconds in (15, 60, 180, 300)
               for usable in [[r['markouts'][str(seconds)] for r in selected if r['markouts'][str(seconds)]['available']]]},
        }
    return {'research_version': VERSION, 'since_ms': since, 'legacy_decision_counts': legacy,
            'frame_count': len(rows), 'first_geometry_count': len(output),
            'failed_gate_counts': dict(Counter(k for r in output for k,v in r['gates'].items() if v is False)),
            'observations': output,
            'filter_comparisons_first_geometry_only': comparisons,
            'limitations': ['Markouts are price diagnostics, not fills or PnL.',
                            'No reconstruction of missing historical book or post-arm delta.',
                            'First-geometry sampling; episodes on overlapping levels are correlated.',
                            'Same-bar path order and initial partial interval are unknown.',
                            'Filter changes require subsequent untouched forward data.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('database')
    parser.add_argument('--since-ms', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(report(args.database, args.since_ms), indent=2))
