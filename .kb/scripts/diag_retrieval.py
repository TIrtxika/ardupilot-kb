#!/usr/bin/env python3
"""Diagnostic: for each tunable param_fact/concept gold item, report
   (a) the resolved gold chunk_id(s),
   (b) whether each exists ANYWHERE in the global index,
   (c) its rank in routed vector retrieval (or 'NOT IN TOPK')."""
import sys, json
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'scripts'))
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import lancedb, duckdb
from phase3_router import classify_query
import phase3_eval as E

con = duckdb.connect(str(KB / 'structured' / 'kb.duckdb'), read_only=True)
db = lancedb.connect(str(KB / 'index' / 'lancedb'))

# Set of all chunk_ids in the global index
allcids = set(db.open_table('chunks').to_arrow().column('chunk_id').to_pylist())

gold = [json.loads(l) for l in open(KB / 'eval' / 'gold.jsonl')]
for item in gold:
    if item.get('type') not in ('param_fact', 'concept'):
        continue
    gids = E.gold_chunk_ids_for_item(item, con)
    q = item['question']
    routed = classify_query(q)
    qv = E.embed_query(q)
    res = E.routed_vector_retrieval(db, routed, qv, gids, k=50)
    top = res.get('top_chunk_ids', [])
    exists = {g: (g in allcids) for g in gids}
    rank = None
    for g in gids:
        if g in top:
            rank = top.index(g) + 1
            break
    status = f"rank={rank}" if rank else ("NOT IN TOP50" if all(exists.values()) else "GOLD_ID_NOT_IN_INDEX")
    print(f"[{item['type']:10}] {item['id']:28} gold={gids} exists={exists} routed={routed} -> {status}")
