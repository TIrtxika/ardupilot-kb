#!/usr/bin/env python3
"""Hypothesis test: does global-BM25 (filtered to routed domains) + RRF fusion
   with dense promote the low-ranked param/concept gold chunks into top-5?
   Reports dense_rank, bm25_rank, rrf_rank for each param_fact/concept item."""
import sys, json, pickle
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'scripts'))
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import lancedb, duckdb
from phase3_router import classify_query
import phase3_eval as E

con = duckdb.connect(str(KB / 'structured' / 'kb.duckdb'), read_only=True)
db = lancedb.connect(str(KB / 'index' / 'lancedb'))

# global BM25
g = pickle.load(open(KB / 'index' / 'bm25' / 'bm25.pkl', 'rb'))
bm25, bm25_cids = g['bm25'], g['chunk_ids']
cid2dom = json.loads((KB / 'index' / 'chunk_domain_map.json').read_text())

def dense_ranked(routed, qv, k=200):
    res = E.routed_vector_retrieval(db, routed, qv, [], k=k)
    return res.get('top_chunk_ids', [])

def bm25_ranked(routed, q, k=200):
    rd = set(routed)
    scores = bm25.get_scores(q.lower().split())
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    out = []
    for i in order:
        cid = bm25_cids[i]
        if cid2dom.get(cid) in rd:
            out.append(cid)
            if len(out) >= k:
                break
    return out

def rrf(lists, k=60):
    score = {}
    for lst in lists:
        for rank, cid in enumerate(lst):
            score[cid] = score.get(cid, 0) + 1.0 / (k + rank + 1)
    return [c for c, _ in sorted(score.items(), key=lambda x: -x[1])]

def rank_of(lst, gids):
    for g_ in gids:
        if g_ in lst:
            return lst.index(g_) + 1
    return None

gold = [json.loads(l) for l in open(KB / 'eval' / 'gold.jsonl')]
hits = {'dense': 0, 'bm25': 0, 'rrf': 0, 'n': 0}
for item in gold:
    if item.get('type') not in ('param_fact', 'concept'):
        continue
    gids = E.gold_chunk_ids_for_item(item, con)
    q = item['question']
    routed = classify_query(q)
    qv = E.embed_query(q)
    dl = dense_ranked(routed, qv)
    bl = bm25_ranked(routed, q)
    rl = rrf([dl, bl])
    dr, br, rr = rank_of(dl, gids), rank_of(bl, gids), rank_of(rl, gids)
    hits['n'] += 1
    hits['dense'] += 1 if dr and dr <= 5 else 0
    hits['bm25'] += 1 if br and br <= 5 else 0
    hits['rrf'] += 1 if rr and rr <= 5 else 0
    flag = '' if (rr and rr <= 5) else '  <-- still miss@5'
    print(f"[{item['type']:10}] {item['id']:30} dense={dr} bm25={br} rrf={rr}{flag}")

n = hits['n']
print(f"\n@5 hit rate over {n} param/concept items:")
print(f"  dense: {hits['dense']}/{n} ({100*hits['dense']/n:.0f}%)")
print(f"  bm25 : {hits['bm25']}/{n} ({100*hits['bm25']/n:.0f}%)")
print(f"  rrf  : {hits['rrf']}/{n} ({100*hits['rrf']/n:.0f}%)")
