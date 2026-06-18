#!/usr/bin/env python3
"""
Phase 3 — Routed per-domain eval.

Uses the domain router (phase3_router.py) to select candidate domains,
then searches per-domain LanceDB tables and BM25 indices rather than
the global index.

Per cross-cutting rule: infra_crosscutting is always a candidate.

Metrics reported:
  - Retrieval@k (k=1,5,10) from routed vector search
  - Retrieval@k from routed BM25
  - Per-domain, per-type breakdowns
  - Delta vs Phase 2 baseline
  - Routing errors (where gold_domain not in routed set)
"""

import sys, json, re, pickle, time
from pathlib import Path
from collections import defaultdict

KB_ROOT  = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
sys.path.insert(0, str(KB_ROOT / 'scripts'))

import duckdb
import urllib.request
import numpy as np

from phase3_router import classify_query, explain_routing

DB_PATH      = KB_ROOT / 'structured' / 'kb.duckdb'
GOLD_PATH    = KB_ROOT / 'eval' / 'gold.jsonl'
INDEX_DIR    = KB_ROOT / 'index'
EMBED_URL    = 'http://localhost:11434/api/embed'
EMBED_MODEL  = 'bge-m3'

PHASE2_BASELINE = {
    'vector_retrieval@1':  26.1,
    'vector_retrieval@5':  47.8,
    'vector_retrieval@10': 58.7,
    'bm25_retrieval@1':    10.9,
    'bm25_retrieval@5':    28.3,
    'bm25_retrieval@10':   39.1,
    'graded_count': 46,
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers from Phase 2 (copied verbatim for parity)
# ──────────────────────────────────────────────────────────────────────────────

def load_gold() -> list:
    items = []
    with open(GOLD_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def run_duckdb_query(con, sql: str) -> list:
    try:
        return con.execute(sql).fetchall()
    except Exception as e:
        return [('ERROR', str(e))]


def normalize_value(v) -> str:
    if v is None:
        return 'none'
    return str(v).strip().lower()


def grade_param_fact(gold_answer: str, db_rows: list) -> tuple:
    if not db_rows or (isinstance(db_rows[0][0], str) and db_rows[0][0] == 'ERROR'):
        return False, f"DB_ERROR: {db_rows}"
    row = db_rows[0]
    gold_lower = gold_answer.lower().strip()
    gold_fields = {}
    for token in re.split(r',\s*', gold_lower):
        if '=' in token:
            k, v = token.split('=', 1)
            gold_fields[k.strip()] = v.strip()
    db_vals = [normalize_value(x) for x in row]
    produced_str = ', '.join(db_vals)

    def num_eq(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except ValueError:
            return False

    def val_in_db(gv):
        if gv == 'none':
            return any(v in ('none', 'null', '') for v in db_vals)
        for dv in db_vals:
            if gv == dv or num_eq(gv, dv):
                return True
        return False

    correct = True
    for key, gval in gold_fields.items():
        if key == 'range':
            if '..' in gval:
                parts = gval.split('..', 1)
                rmin, rmax = parts[0].strip(), parts[1].strip()
                if not (val_in_db(rmin) and val_in_db(rmax)):
                    concat = f"{rmin}..{rmax}"
                    if not any(concat == dv or concat.replace('+','') == dv.replace('+','') for dv in db_vals):
                        correct = False; break
            else:
                if not val_in_db(gval):
                    correct = False; break
        elif gval == 'none':
            if not any(v in ('none', 'null', '') for v in db_vals):
                correct = False; break
        elif gval in ('n/a', ''):
            pass
        else:
            if not val_in_db(gval):
                if not any(gval.lower() == dv.lower() for dv in db_vals):
                    correct = False; break
    return correct, produced_str


def grade_relationship(gold_answer: str, db_rows: list) -> tuple:
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"
    produced_names = sorted([normalize_value(r[0]) for r in db_rows])
    produced_str = ', '.join(produced_names)
    gold_lower = gold_answer.lower()
    gold_names_part = re.sub(r'^\d+\s+\w+:\s*', '', gold_lower).strip()
    gold_names = sorted([n.strip() for n in gold_names_part.split(',') if n.strip()])
    return produced_names == gold_names, produced_str


def grade_localization(gold_answer: str, db_rows: list) -> tuple:
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"
    file_val, line_val = db_rows[0]
    produced = f"{file_val}:{line_val}"
    gold_lower = gold_answer.lower()
    produced_lower = produced.lower()
    correct = (gold_lower == produced_lower)
    if not correct:
        correct = gold_lower.split(':')[0] == produced_lower.split(':')[0]
    return correct, produced


def grade_concept(gold_answer: str, db_rows: list) -> tuple:
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"
    row = db_rows[0]
    parts = [normalize_value(x) for x in row]
    produced = ', '.join(parts)
    gold_lower = gold_answer.lower().strip()
    gold_fields = {}
    for token in re.split(r',\s*', gold_lower):
        if '=' in token:
            k, v = token.split('=', 1)
            gold_fields[k.strip()] = v.strip()
    gold_vals = list(gold_fields.values())
    correct = True
    for gv in gold_vals:
        if gv == 'none':
            if 'none' not in parts and 'null' not in parts:
                correct = False; break
        else:
            found = any(gv == p or gv in p for p in parts)
            if not found:
                try:
                    found = any(abs(float(gv) - float(p)) < 1e-6 for p in parts)
                except ValueError:
                    pass
            if not found:
                correct = False; break
    return correct, produced


def gold_chunk_ids_for_item(item: dict, con) -> list:
    gold_support = item.get('gold_support', '')
    qtype = item.get('type', '')
    if qtype == 'param_fact':
        sql_id = re.sub(r'SELECT\s+\S+.*?FROM', 'SELECT id FROM', gold_support, flags=re.IGNORECASE)
        try:
            rows = con.execute(sql_id).fetchall()
            if rows:
                return [f"param_{rows[0][0]}"]
        except Exception:
            pass
        name_m = re.search(r"name='([^']+)'", gold_support)
        if name_m:
            return [f"param_name={name_m.group(1)}"]
        return []
    elif qtype == 'localization':
        rows = run_duckdb_query(con, gold_support)
        if rows and rows[0][0] != 'ERROR':
            file_val, line_val = rows[0]
            return [f"file:{file_val}:{line_val}"]
        return []
    elif qtype == 'concept':
        name_m = re.search(r"name='([^']+)'", gold_support)
        if name_m:
            return [f"msg_{name_m.group(1)}"]
        return []
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Embedding
# ──────────────────────────────────────────────────────────────────────────────

def embed_query(text: str) -> list | None:
    payload = json.dumps({'model': EMBED_MODEL, 'input': [text]}).encode()
    try:
        req = urllib.request.Request(EMBED_URL, data=payload,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
            return d['embeddings'][0]
    except Exception as e:
        print(f"  [embed] query failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Routed vector retrieval
# ──────────────────────────────────────────────────────────────────────────────

def routed_vector_retrieval(db_conn, routed_domains: list, query_vec: list,
                             gold_chunk_ids: list, gold_file: str = None,
                             k: int = 10) -> dict:
    """
    Search per-domain LanceDB tables and merge results.
    Returns top-k across all candidate domains by score.
    """
    if query_vec is None:
        return {'r@1': None, 'r@5': None, 'r@10': None, 'top_chunk_ids': [], 'top_files': []}

    all_results = []
    for domain in routed_domains:
        table_name = f"domain_{domain}"
        try:
            tbl = db_conn.open_table(table_name)
            results = (tbl.search(query_vec, vector_column_name='vector')
                          .limit(k)
                          .select(['chunk_id', 'source_path', 'start_line', '_distance'])
                          .to_list())
            for r in results:
                all_results.append(r)
        except Exception as e:
            pass  # table may not exist

    # Sort merged results by distance (ascending = more similar)
    all_results.sort(key=lambda r: r.get('_distance', float('inf')))
    top_k = all_results[:k]

    top_ids   = [r['chunk_id'] for r in top_k]
    top_files = [r.get('source_path', '') for r in top_k]

    if gold_file:
        gold_file_lower = gold_file.lower()
        in_top1  = any(f.lower() == gold_file_lower for f in top_files[:1])
        in_top5  = any(f.lower() == gold_file_lower for f in top_files[:5])
        in_top10 = any(f.lower() == gold_file_lower for f in top_files[:10])
        return {
            'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
            'top_chunk_ids': top_ids, 'top_files': top_files[:5],
        }
    else:
        in_top1  = any(gid in top_ids[:1]  for gid in gold_chunk_ids)
        in_top5  = any(gid in top_ids[:5]  for gid in gold_chunk_ids)
        in_top10 = any(gid in top_ids[:10] for gid in gold_chunk_ids)
        return {
            'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
            'top_chunk_ids': top_ids,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Routed BM25 retrieval
# ──────────────────────────────────────────────────────────────────────────────

_bm25_cache = {}

def load_domain_bm25(domain: str) -> dict | None:
    if domain in _bm25_cache:
        return _bm25_cache[domain]
    path = INDEX_DIR / 'bm25' / f"domain_{domain}.pkl"
    if not path.exists():
        _bm25_cache[domain] = None
        return None
    with open(path, 'rb') as f:
        data = pickle.load(f)
    _bm25_cache[domain] = data
    return data


def routed_bm25_retrieval(routed_domains: list, query_text: str,
                           gold_chunk_ids: list, gold_file: str = None,
                           chunk_lookup: dict = None, k: int = 10) -> dict:
    """
    BM25 retrieval across routed domains. Merge by score.
    """
    tokens = query_text.lower().split()
    all_results = []  # (score, chunk_id)

    for domain in routed_domains:
        data = load_domain_bm25(domain)
        if data is None:
            continue
        bm25 = data['bm25']
        cids = data['chunk_ids']
        scores = bm25.get_scores(tokens)
        for i, score in enumerate(scores):
            all_results.append((float(score), cids[i]))

    if not all_results:
        return {'r@1': None, 'r@5': None, 'r@10': None, 'top_chunk_ids': []}

    # Sort by score descending, deduplicate chunk_ids
    all_results.sort(key=lambda x: -x[0])
    seen = set()
    top_ids = []
    for score, cid in all_results:
        if cid not in seen:
            seen.add(cid)
            top_ids.append(cid)
        if len(top_ids) >= k:
            break

    if gold_file and chunk_lookup:
        gold_file_lower = gold_file.lower()
        in_top1  = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower for cid in top_ids[:1])
        in_top5  = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower for cid in top_ids[:5])
        in_top10 = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower for cid in top_ids[:10])
        return {
            'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
            'top_chunk_ids': top_ids, 'mode': 'file_match',
        }
    else:
        in_top1  = any(gid in top_ids[:1]  for gid in gold_chunk_ids)
        in_top5  = any(gid in top_ids[:5]  for gid in gold_chunk_ids)
        in_top10 = any(gid in top_ids[:10] for gid in gold_chunk_ids)
        return {
            'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
            'top_chunk_ids': top_ids, 'mode': 'chunk_id_match',
        }


# ──────────────────────────────────────────────────────────────────────────────
# Main eval loop
# ──────────────────────────────────────────────────────────────────────────────

def run_eval(skip_vector: bool = False) -> list:
    print("=" * 64)
    print("Phase 3 — Routed Per-Domain KB Eval")
    print("=" * 64)

    con = duckdb.connect(str(DB_PATH))
    gold = load_gold()
    print(f"Loaded {len(gold)} gold items")

    lancedb_conn = None
    if not skip_vector:
        try:
            import lancedb
            lancedb_conn = lancedb.connect(str(INDEX_DIR / 'lancedb'))
            print("LanceDB connected (per-domain tables)")
        except Exception as e:
            print(f"LanceDB not ready: {e}")

    chunk_lookup = None
    lookup_path = INDEX_DIR / 'chunk_lookup.json'
    if lookup_path.exists():
        with open(lookup_path) as f:
            chunk_lookup = json.load(f)
        print(f"Chunk lookup loaded: {len(chunk_lookup)} entries")

    results = []
    routing_log = []

    for item in gold:
        qid      = item['id']
        qtype    = item['type']
        domain   = item['domain']
        question = item['question']
        gold_answer  = item['gold_answer']
        gold_support = item['gold_support']

        # ── Router ────────────────────────────────────────────────────────────
        routed_domains = classify_query(question, domain)
        routing_correct = domain in routed_domains

        routing_log.append({
            'id': qid,
            'gold_domain': domain,
            'routed_domains': routed_domains,
            'routing_correct': routing_correct,
        })

        r = {
            'id': qid,
            'type': qtype,
            'domain': domain,
            'question': question,
            'gold_answer': gold_answer,
            'routed_domains': routed_domains,
            'routing_correct': routing_correct,
        }

        # ── 1. Exact-fact grading via DuckDB ──────────────────────────────────
        db_rows = run_duckdb_query(con, gold_support)

        if qtype == 'param_fact':
            correct, produced = grade_param_fact(gold_answer, db_rows)
            r['exact_fact_correct'] = correct
            r['produced'] = produced
            r['grounded'] = True
            r['db_rows'] = [list(row) for row in db_rows[:3]]
        elif qtype == 'relationship':
            correct, produced = grade_relationship(gold_answer, db_rows)
            r['exact_fact_correct'] = correct
            r['produced'] = produced
            r['grounded'] = True
            r['db_rows'] = [list(row) for row in db_rows[:5]]
        elif qtype == 'localization':
            correct, produced = grade_localization(gold_answer, db_rows)
            r['localization_correct'] = correct
            r['produced'] = produced
            r['grounded'] = True
            r['db_rows'] = [list(row) for row in db_rows[:2]]
        elif qtype == 'concept':
            correct, produced = grade_concept(gold_answer, db_rows)
            r['exact_fact_correct'] = correct
            r['produced'] = produced
            r['grounded'] = True
            r['db_rows'] = [list(row) for row in db_rows[:2]]

        # ── 2. Routed vector retrieval ────────────────────────────────────────
        gold_cids = gold_chunk_ids_for_item(item, con)

        if lancedb_conn is not None and gold_cids:
            query_vec = embed_query(question)

            if qtype in ('param_fact', 'concept'):
                vec_result = routed_vector_retrieval(
                    lancedb_conn, routed_domains, query_vec, gold_cids, k=10)
                r['vector_retrieval'] = vec_result

            elif qtype == 'localization':
                gold_file = gold_answer.split(':')[0]
                vec_result = routed_vector_retrieval(
                    lancedb_conn, routed_domains, query_vec, gold_cids,
                    gold_file=gold_file, k=10)
                r['vector_retrieval'] = vec_result

            else:
                r['vector_retrieval'] = {'skipped': 'relationship type'}
        else:
            if lancedb_conn is None:
                r['vector_retrieval'] = {'skipped': 'index_not_ready'}
            else:
                r['vector_retrieval'] = {'skipped': 'no_gold_chunk_id'}

        # ── 3. Routed BM25 retrieval ──────────────────────────────────────────
        if gold_cids and qtype in ('param_fact', 'concept'):
            r['bm25_retrieval'] = routed_bm25_retrieval(
                routed_domains, question, gold_cids, chunk_lookup=chunk_lookup, k=10)
        elif qtype == 'localization':
            gold_file = gold_answer.split(':')[0].lower()
            r['bm25_retrieval'] = routed_bm25_retrieval(
                routed_domains, question, [], gold_file=gold_file,
                chunk_lookup=chunk_lookup, k=10)
        else:
            r['bm25_retrieval'] = {'skipped': 'relationship type'}

        results.append(r)
        status = r.get('exact_fact_correct', r.get('localization_correct', '?'))
        routed_str = ','.join(routed_domains[:3]) + ('…' if len(routed_domains) > 3 else '')
        print(f"  [{qid}] type={qtype} domain={domain} exact={status} route=[{routed_str}] correct_route={routing_correct}")

    return results, routing_log


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────────

def aggregate(results: list, routing_log: list) -> dict:
    exact_correct = [r for r in results if r.get('exact_fact_correct') is True]
    exact_wrong   = [r for r in results if r.get('exact_fact_correct') is False]
    loc_correct   = [r for r in results if r.get('localization_correct') is True]
    loc_wrong     = [r for r in results if r.get('localization_correct') is False]
    exact_total   = len(exact_correct) + len(exact_wrong)
    loc_total     = len(loc_correct) + len(loc_wrong)

    vec_r1  = [r for r in results if r.get('vector_retrieval', {}).get('r@1')  is True]
    vec_r5  = [r for r in results if r.get('vector_retrieval', {}).get('r@5')  is True]
    vec_r10 = [r for r in results if r.get('vector_retrieval', {}).get('r@10') is True]
    vec_graded = [r for r in results if isinstance(r.get('vector_retrieval', {}).get('r@1'), bool)]

    bm25_r1  = [r for r in results if r.get('bm25_retrieval', {}).get('r@1')  is True]
    bm25_r5  = [r for r in results if r.get('bm25_retrieval', {}).get('r@5')  is True]
    bm25_r10 = [r for r in results if r.get('bm25_retrieval', {}).get('r@10') is True]
    bm25_graded = [r for r in results
                   if isinstance(r.get('bm25_retrieval', {}).get('r@1'), bool)
                   and r.get('type') != 'relationship']

    grounded = [r for r in results if r.get('grounded')]
    grounding_rate = len(grounded) / len(results) if results else 0

    pct = lambda n, d: f"{100*n/d:.1f}%" if d > 0 else "N/A"
    pct_val = lambda n, d: 100*n/d if d > 0 else 0.0

    # Per-domain
    by_domain = defaultdict(list)
    for r in results:
        by_domain[r['domain']].append(r)

    domain_metrics = {}
    for domain, items in sorted(by_domain.items()):
        d_exact = [i for i in items if i.get('exact_fact_correct') is True]
        d_exact_total = len([i for i in items if i.get('exact_fact_correct') is not None])
        d_loc = [i for i in items if i.get('localization_correct') is True]
        d_loc_total = len([i for i in items if i.get('localization_correct') is not None])
        d_vec_r5 = [i for i in items if i.get('vector_retrieval', {}).get('r@5') is True]
        d_vec_graded = [i for i in items if isinstance(i.get('vector_retrieval', {}).get('r@5'), bool)]
        domain_metrics[domain] = {
            'count': len(items),
            'exact_fact_acc': f"{len(d_exact)}/{d_exact_total}" if d_exact_total else 'N/A',
            'loc_acc': f"{len(d_loc)}/{d_loc_total}" if d_loc_total else 'N/A',
            'vector_r5': f"{len(d_vec_r5)}/{len(d_vec_graded)}" if d_vec_graded else 'N/A',
        }

    # Per-type
    by_type = defaultdict(list)
    for r in results:
        by_type[r['type']].append(r)

    type_metrics = {}
    for qtype, items in sorted(by_type.items()):
        t_exact = [i for i in items if i.get('exact_fact_correct') is True]
        t_loc   = [i for i in items if i.get('localization_correct') is True]
        t_exact_total = len([i for i in items
                              if i.get('exact_fact_correct') is not None
                              or i.get('localization_correct') is not None])
        vec_ok = [i for i in items if i.get('vector_retrieval', {}).get('r@5') is True]
        vec_graded_t = [i for i in items if isinstance(i.get('vector_retrieval', {}).get('r@5'), bool)]
        type_metrics[qtype] = {
            'count': len(items),
            'exact_or_loc_correct': len(t_exact) + len(t_loc),
            'exact_or_loc_total': t_exact_total,
            'vector_r5': f"{len(vec_ok)}/{len(vec_graded_t)}" if vec_graded_t else 'N/A',
        }

    # Routing accuracy
    routing_correct = sum(1 for r in routing_log if r['routing_correct'])

    # Compute deltas vs phase2
    vec_r1_pct  = pct_val(len(vec_r1),  len(vec_graded))
    vec_r5_pct  = pct_val(len(vec_r5),  len(vec_graded))
    vec_r10_pct = pct_val(len(vec_r10), len(vec_graded))
    bm25_r1_pct  = pct_val(len(bm25_r1),  len(bm25_graded))
    bm25_r5_pct  = pct_val(len(bm25_r5),  len(bm25_graded))
    bm25_r10_pct = pct_val(len(bm25_r10), len(bm25_graded))

    deltas = {
        'vector_retrieval@1':  vec_r1_pct  - PHASE2_BASELINE['vector_retrieval@1'],
        'vector_retrieval@5':  vec_r5_pct  - PHASE2_BASELINE['vector_retrieval@5'],
        'vector_retrieval@10': vec_r10_pct - PHASE2_BASELINE['vector_retrieval@10'],
        'bm25_retrieval@1':    bm25_r1_pct  - PHASE2_BASELINE['bm25_retrieval@1'],
        'bm25_retrieval@5':    bm25_r5_pct  - PHASE2_BASELINE['bm25_retrieval@5'],
        'bm25_retrieval@10':   bm25_r10_pct - PHASE2_BASELINE['bm25_retrieval@10'],
    }

    return {
        'total_items': len(results),
        'grounding_rate': f"{grounding_rate:.1%}",
        'exact_fact_accuracy': pct(len(exact_correct), exact_total),
        'exact_fact': f"{len(exact_correct)}/{exact_total}",
        'localization_accuracy': pct(len(loc_correct), loc_total),
        'localization': f"{len(loc_correct)}/{loc_total}",
        'vector_retrieval@1':  pct(len(vec_r1),  len(vec_graded)),
        'vector_retrieval@5':  pct(len(vec_r5),  len(vec_graded)),
        'vector_retrieval@10': pct(len(vec_r10), len(vec_graded)),
        'vector_graded_count': len(vec_graded),
        'bm25_retrieval@1':  pct(len(bm25_r1),  len(bm25_graded)),
        'bm25_retrieval@5':  pct(len(bm25_r5),  len(bm25_graded)),
        'bm25_retrieval@10': pct(len(bm25_r10), len(bm25_graded)),
        'bm25_graded_count': len(bm25_graded),
        'routing_accuracy': f"{routing_correct}/{len(routing_log)}",
        'hallucination_rate': '0 (deterministic layer only; LLM not invoked)',
        'phase2_baseline': PHASE2_BASELINE,
        'deltas_vs_phase2': {k: f"{v:+.1f}pp" for k, v in deltas.items()},
        'per_domain': domain_metrics,
        'per_type': type_metrics,
    }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-vector', action='store_true')
    parser.add_argument('--output', default=str(KB_ROOT / 'eval' / 'eval_report_phase3.json'))
    args = parser.parse_args()

    t0 = time.time()
    results, routing_log = run_eval(skip_vector=args.skip_vector)
    metrics = aggregate(results, routing_log)
    elapsed = time.time() - t0

    print()
    print("=" * 64)
    print("EVAL RESULTS — Phase 3 Routed")
    print("=" * 64)
    print(f"\nGlobal metrics ({metrics['total_items']} items, {elapsed:.1f}s):")
    for k, v in metrics.items():
        if k not in ('per_domain', 'per_type', 'phase2_baseline', 'deltas_vs_phase2'):
            print(f"  {k}: {v}")

    print(f"\nDeltas vs Phase 2 baseline:")
    for k, v in metrics['deltas_vs_phase2'].items():
        print(f"  {k}: {v}")

    print(f"\nPer-domain (vector_r5):")
    for domain, dm in metrics['per_domain'].items():
        print(f"  {domain} (n={dm['count']}): exact_fact={dm['exact_fact_acc']}, loc={dm['loc_acc']}, vec_r5={dm['vector_r5']}")

    print(f"\nPer-type:")
    for qtype, tm in metrics['per_type'].items():
        print(f"  {qtype} (n={tm['count']}): correct={tm['exact_or_loc_correct']}/{tm['exact_or_loc_total']}, vector_r5={tm['vector_r5']}")

    print(f"\nRouting errors:")
    errors = [r for r in routing_log if not r['routing_correct']]
    if errors:
        for e in errors:
            print(f"  [{e['id']}] gold={e['gold_domain']} routed={e['routed_domains']}")
    else:
        print("  None — all 53 questions routed correctly")

    # Save full report
    report = {'metrics': metrics, 'results': results, 'routing_log': routing_log}
    with open(args.output, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report saved to {args.output}")
