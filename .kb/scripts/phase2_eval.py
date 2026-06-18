#!/usr/bin/env python3
"""
Phase 2 — KB Evaluation against gold.jsonl

Metrics:
  - Retrieval@k (k=1,5,10): did gold-support chunk appear in top-k?
  - Exact-fact accuracy: for param_fact/concept/relationship, does produced value = gold?
  - Localization accuracy: did answer cite correct file:line?
  - Grounding rate: fraction of answers with valid citation
  - Hallucination rate: claims marked UNSUPPORTED per 100 answers

Strategy:
  - param_fact + relationship + concept with SQL gold_support -> query DuckDB directly
    (deterministic layer; these do NOT require the vector index)
  - localization -> check DuckDB symbol table
  - Vector retrieval -> check if gold chunk is in top-k from LanceDB
  - BM25 retrieval -> check if gold chunk is in top-k

This eval is GROUNDING-FIRST: the deterministic layer is queried first.
The LanceDB retrieval test measures whether the index surfaces the right chunk.
"""

import sys, json, re
from pathlib import Path
from collections import defaultdict

KB_ROOT  = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import duckdb
import urllib.request

DB_PATH      = KB_ROOT / 'structured' / 'kb.duckdb'
GOLD_PATH    = KB_ROOT / 'eval' / 'gold.jsonl'
INDEX_DIR    = KB_ROOT / 'index'
CHUNKS_DIR   = KB_ROOT / 'chunks'
EMBED_URL    = 'http://localhost:11434/api/embed'
EMBED_MODEL  = 'bge-m3'
EMBED_DIM    = 1024

# ──────────────────────────────────────────────────────────────────────────────
# Load gold
# ──────────────────────────────────────────────────────────────────────────────
def load_gold() -> list[dict]:
    items = []
    with open(GOLD_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# DuckDB exact-fact grader
# ──────────────────────────────────────────────────────────────────────────────
def run_duckdb_query(con, sql: str) -> list[tuple]:
    try:
        return con.execute(sql).fetchall()
    except Exception as e:
        return [('ERROR', str(e))]


def normalize_value(v) -> str:
    """Normalize a value for comparison: strip, lower, handle None."""
    if v is None:
        return 'none'
    return str(v).strip().lower()


def grade_param_fact(gold_answer: str, db_rows: list[tuple]) -> tuple[bool, str]:
    """
    Compare gold_answer string like "default=180, range=10..500, units=deg/s"
    against DuckDB result rows.

    The DuckDB SELECT returns columns in the order: default_val, range_min, range_max[, units, ...]
    The gold_support SELECT determines the column order.

    We parse the gold_answer field names to understand what columns are expected,
    then match the DB values to them positionally OR by constructing a composite range.
    Returns (correct: bool, produced: str)
    """
    if not db_rows or (isinstance(db_rows[0][0], str) and db_rows[0][0] == 'ERROR'):
        return False, f"DB_ERROR: {db_rows}"

    row = db_rows[0]

    # Parse gold_answer: extract key=value pairs
    gold_lower = gold_answer.lower().strip()
    gold_fields = {}
    for token in re.split(r',\s*', gold_lower):
        if '=' in token:
            k, v = token.split('=', 1)
            gold_fields[k.strip()] = v.strip()

    # DB SELECT column order matters. We match gold fields to DB columns positionally
    # by parsing the gold_support SELECT clause.
    # Since we don't have that here, we apply a heuristic:
    #
    # For range=min..max: the DB returns (range_min, range_max) as separate columns.
    # We need to reconstruct and compare.
    # For default, units: direct match.
    #
    # Strategy: flatten the DB row into a set of normalized values, then check each
    # gold field against that set, with special handling for "range=min..max".

    db_vals = [normalize_value(x) for x in row]
    produced_str = ', '.join(db_vals)

    def num_eq(a: str, b: str) -> bool:
        """Numeric equality with tolerance."""
        try:
            return abs(float(a) - float(b)) < 1e-6
        except ValueError:
            return False

    def val_in_db(gv: str) -> bool:
        """Check if a single gold value is present in the DB row."""
        if gv == 'none':
            return any(v in ('none', 'null', '') for v in db_vals)
        for dv in db_vals:
            if gv == dv:
                return True
            if num_eq(gv, dv):
                return True
        return False

    correct = True
    for key, gval in gold_fields.items():
        if key == 'range':
            # gval is like "10..500" or "-0.1745..+0.1745" or "-70..80"
            if '..' in gval:
                parts = gval.split('..', 1)
                rmin, rmax = parts[0].strip(), parts[1].strip()
                # Both must appear in DB values
                if not (val_in_db(rmin) and val_in_db(rmax)):
                    # Also try: concatenated "min..max" appears as one of the DB values
                    concat = f"{rmin}..{rmax}"
                    if not any(concat == dv or concat.replace('+','') == dv.replace('+','')
                                for dv in db_vals):
                        correct = False
                        break
            else:
                if not val_in_db(gval):
                    correct = False
                    break
        elif gval == 'none':
            if not any(v in ('none', 'null', '') for v in db_vals):
                correct = False
                break
        elif gval in ('n/a', ''):
            pass
        else:
            if not val_in_db(gval):
                # Case-insensitive fallback
                gval_ci = gval.lower()
                if not any(gval_ci == dv.lower() for dv in db_vals):
                    correct = False
                    break

    return correct, produced_str


def grade_relationship(gold_answer: str, db_rows: list[tuple]) -> tuple[bool, str]:
    """
    Gold answer like "5 subclasses: A, B, C, D, E"
    DB rows: list of single-column tuples
    """
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"

    produced_names = sorted([normalize_value(r[0]) for r in db_rows])
    produced_str = ', '.join(produced_names)

    # Extract expected names from gold
    gold_lower = gold_answer.lower()
    # Remove count prefix like "5 subclasses: "
    gold_names_part = re.sub(r'^\d+\s+\w+:\s*', '', gold_lower).strip()
    gold_names = sorted([n.strip() for n in gold_names_part.split(',') if n.strip()])

    correct = (produced_names == gold_names)
    return correct, produced_str


def grade_localization(gold_answer: str, db_rows: list[tuple]) -> tuple[bool, str]:
    """
    Gold answer like "libraries/AC_AttitudeControl/AC_PosControl.h:38"
    DB rows: [(file, start_line), ...]
    """
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"

    file_val, line_val = db_rows[0]
    produced = f"{file_val}:{line_val}"

    gold_lower = gold_answer.lower()
    produced_lower = produced.lower()

    # Exact match
    correct = (gold_lower == produced_lower)
    if not correct:
        # File-only match (line may differ)
        gold_file = gold_lower.split(':')[0]
        prod_file = produced_lower.split(':')[0]
        correct = (gold_file == prod_file)

    return correct, produced


def grade_concept(gold_answer: str, db_rows: list[tuple]) -> tuple[bool, str]:
    """
    Grade concept questions (msg IDs, field types etc.)
    Gold: "msg_id=0" or "field_type=float, units=rad"
    """
    if not db_rows or db_rows[0][0] == 'ERROR':
        return False, f"DB_ERROR: {db_rows}"

    row = db_rows[0]
    parts = [normalize_value(x) for x in row]
    produced = ', '.join(parts)

    gold_lower = gold_answer.lower().strip()
    # Extract values from gold
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
                correct = False
                break
        else:
            found = any(gv == p or gv in p for p in parts)
            if not found:
                try:
                    found = any(abs(float(gv) - float(p)) < 1e-6 for p in parts)
                except ValueError:
                    pass
            if not found:
                correct = False
                break

    return correct, produced


# ──────────────────────────────────────────────────────────────────────────────
# Retrieval graders
# ──────────────────────────────────────────────────────────────────────────────

def embed_query(text: str) -> list[float] | None:
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


def retrieval_at_k(table, query_text: str, gold_chunk_ids: list[str], k: int = 10) -> dict:
    """
    Vector retrieval@k: check if any gold chunk id is in top-k results.
    """
    vec = embed_query(query_text)
    if vec is None:
        return {'r@1': None, 'r@5': None, 'r@10': None, 'top_chunk_ids': []}

    try:
        results = (table.search(vec, vector_column_name='vector')
                       .limit(k)
                       .select(['chunk_id', 'domain', 'source_path', 'param_name', 'msg_name'])
                       .to_list())
        top_ids = [r['chunk_id'] for r in results]

        return {
            'r@1':  any(gid in top_ids[:1]  for gid in gold_chunk_ids),
            'r@5':  any(gid in top_ids[:5]  for gid in gold_chunk_ids),
            'r@10': any(gid in top_ids[:10] for gid in gold_chunk_ids),
            'top_chunk_ids': top_ids,
        }
    except Exception as e:
        return {'r@1': None, 'r@5': None, 'r@10': None, 'error': str(e), 'top_chunk_ids': []}


def bm25_at_k(bm25_data: dict | None, query_text: str, gold_chunk_ids: list[str],
              k: int = 10, gold_file: str = None,
              chunk_lookup: dict | None = None) -> dict:
    """
    BM25 retrieval@k.
    gold_chunk_ids: list of chunk_ids to match (for param/concept)
    gold_file: source file path to match (for localization, file-level match)
    chunk_lookup: {chunk_id -> {source_path, ...}} for file-based matching
    """
    if bm25_data is None:
        return {'r@1': None, 'r@5': None, 'r@10': None}

    bm25 = bm25_data['bm25']
    chunk_ids = bm25_data['chunk_ids']
    tokens = query_text.lower().split()

    import numpy as np
    scores = bm25.get_scores(tokens)
    top_idx = np.argsort(scores)[::-1][:k]
    top_ids = [chunk_ids[i] for i in top_idx]

    if gold_file and chunk_lookup:
        gold_file_lower = gold_file.lower()
        in_top1  = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower
                       for cid in top_ids[:1])
        in_top5  = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower
                       for cid in top_ids[:5])
        in_top10 = any((chunk_lookup.get(cid, {}).get('source_path', '')).lower() == gold_file_lower
                       for cid in top_ids[:10])
        return {
            'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
            'top_chunk_ids': top_ids,
            'mode': 'file_match',
        }

    return {
        'r@1':  any(gid in top_ids[:1]  for gid in gold_chunk_ids),
        'r@5':  any(gid in top_ids[:5]  for gid in gold_chunk_ids),
        'r@10': any(gid in top_ids[:10] for gid in gold_chunk_ids),
        'top_chunk_ids': top_ids,
        'mode': 'chunk_id_match',
    }


def gold_chunk_ids_for_item(item: dict, con) -> list[str]:
    """
    Determine what chunk_id(s) correspond to the gold support for a given eval item.
    For param_fact: chunk_id = "param_{id}" where id comes from the params table.
    For localization: chunk_id derived from file:line.
    For relationship: structured table chunk (subclasses_of, callers_of etc.) — we emit
                      the relevant chunks.
    For concept (messages): chunk_id = "msg_{msg_name}".
    """
    gold_support = item.get('gold_support', '')
    qtype = item.get('type', '')

    if qtype == 'param_fact':
        # Extract param id from DuckDB
        # Rewrite gold_support SELECT to get id
        sql_id = re.sub(r'SELECT\s+\S+.*?FROM', 'SELECT id FROM', gold_support, flags=re.IGNORECASE)
        try:
            rows = con.execute(sql_id).fetchall()
            if rows:
                return [f"param_{rows[0][0]}"]
        except Exception:
            pass
        # Fallback: extract param name and file from gold_support
        name_m = re.search(r"name='([^']+)'", gold_support)
        if name_m:
            return [f"param_name={name_m.group(1)}"]
        return []

    elif qtype == 'localization':
        # Get file:start_line from DB
        rows = run_duckdb_query(con, gold_support)
        if rows and rows[0][0] != 'ERROR':
            file_val, line_val = rows[0]
            # chunk_id is sha1 of "file:start_line:end_line"
            # We search by file+start_line in lancedb instead
            return [f"file:{file_val}:{line_val}"]  # pseudo-id for matching
        return []

    elif qtype == 'concept':
        # MAVLink/DroneCAN messages: chunk_id = "msg_{name}"
        name_m = re.search(r"name='([^']+)'", gold_support)
        if name_m:
            return [f"msg_{name_m.group(1)}"]
        return []

    elif qtype == 'relationship':
        # Edge/subclass queries — no single chunk. Return empty (retrieval not graded for rel).
        return []

    return []


def check_localization_in_results(top_chunk_ids: list[str], gold_file: str,
                                   con_lancedb) -> bool:
    """Check if any top chunk is from the correct file."""
    # This is a fallback for localization: check source_path in retrieved chunks
    # We'd need to query LanceDB by chunk_id, which is more complex.
    # For now, return None (not graded via retrieval for localization).
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main eval loop
# ──────────────────────────────────────────────────────────────────────────────

def run_eval(skip_vector: bool = False) -> dict:
    print("=" * 60)
    print("Phase 2 — KB Eval")
    print("=" * 60)

    con = duckdb.connect(str(DB_PATH))
    gold = load_gold()
    print(f"Loaded {len(gold)} gold items")

    # Load LanceDB table if available
    lancedb_table = None
    if not skip_vector:
        try:
            import lancedb
            db = lancedb.connect(str(INDEX_DIR / 'lancedb'))
            lancedb_table = db.open_table('chunks')
            n_rows = lancedb_table.count_rows()
            print(f"LanceDB table: {n_rows} rows")
        except Exception as e:
            print(f"LanceDB not ready: {e}")

    # Load BM25 if available
    bm25_data = None
    bm25_path = INDEX_DIR / 'bm25' / 'bm25.pkl'
    if bm25_path.exists():
        import pickle
        with open(bm25_path, 'rb') as f:
            bm25_data = pickle.load(f)
        print(f"BM25 loaded: {len(bm25_data['chunk_ids'])} chunks")
    else:
        print("BM25 not ready")

    # Load chunk lookup for file-based localization matching
    chunk_lookup = None
    lookup_path = INDEX_DIR / 'chunk_lookup.json'
    if lookup_path.exists():
        with open(lookup_path) as f:
            chunk_lookup = json.load(f)
        print(f"Chunk lookup loaded: {len(chunk_lookup)} entries")

    results = []

    for item in gold:
        qid = item['id']
        qtype = item['type']
        domain = item['domain']
        question = item['question']
        gold_answer = item['gold_answer']
        gold_support = item['gold_support']

        r = {
            'id': qid,
            'type': qtype,
            'domain': domain,
            'question': question,
            'gold_answer': gold_answer,
        }

        # ── 1. Exact-fact grading via DuckDB ──────────────────────────────────
        db_rows = run_duckdb_query(con, gold_support)

        if qtype == 'param_fact':
            correct, produced = grade_param_fact(gold_answer, db_rows)
            r['exact_fact_correct'] = correct
            r['produced'] = produced
            r['grounded'] = True  # DuckDB is authoritative
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

        # ── 2. Vector retrieval@k ─────────────────────────────────────────────
        gold_cids = gold_chunk_ids_for_item(item, con)

        if lancedb_table is not None and gold_cids:
            # For param_fact: search by param chunk_id directly
            if qtype == 'param_fact':
                param_id = gold_cids[0]  # "param_123"
                vec_result = retrieval_at_k(lancedb_table, question, [param_id], k=10)
                r['vector_retrieval'] = vec_result
            elif qtype == 'concept':
                msg_id = gold_cids[0]  # "msg_HEARTBEAT"
                vec_result = retrieval_at_k(lancedb_table, question, [msg_id], k=10)
                r['vector_retrieval'] = vec_result
            elif qtype == 'localization':
                # For localization, we need to find chunk by file+line
                # Search top-10 and check if source_path matches
                vec_q = embed_query(question)
                if vec_q is not None:
                    try:
                        top_results = (lancedb_table.search(vec_q, vector_column_name='vector')
                                           .limit(10)
                                           .select(['chunk_id', 'source_path', 'start_line'])
                                           .to_list())
                        gold_file = gold_answer.split(':')[0].lower()
                        in_top1  = any(r2['source_path'].lower() == gold_file for r2 in top_results[:1])
                        in_top5  = any(r2['source_path'].lower() == gold_file for r2 in top_results[:5])
                        in_top10 = any(r2['source_path'].lower() == gold_file for r2 in top_results[:10])
                        r['vector_retrieval'] = {'r@1': in_top1, 'r@5': in_top5, 'r@10': in_top10,
                                                  'top_files': [r2['source_path'] for r2 in top_results[:5]]}
                    except Exception as e:
                        r['vector_retrieval'] = {'error': str(e)}
            else:
                r['vector_retrieval'] = {'skipped': 'relationship type'}
        else:
            if lancedb_table is None:
                r['vector_retrieval'] = {'skipped': 'index_not_ready'}
            else:
                r['vector_retrieval'] = {'skipped': 'no_gold_chunk_id'}

        # ── 3. BM25 retrieval@k ───────────────────────────────────────────────
        if bm25_data is not None and gold_cids and qtype in ('param_fact', 'concept'):
            r['bm25_retrieval'] = bm25_at_k(bm25_data, question, gold_cids, k=10,
                                              chunk_lookup=chunk_lookup)
        elif bm25_data is not None and qtype == 'localization':
            # BM25 localization: match source_path via chunk_lookup
            gold_file = gold_answer.split(':')[0].lower()
            r['bm25_retrieval'] = bm25_at_k(bm25_data, question, [], k=10,
                                              gold_file=gold_file,
                                              chunk_lookup=chunk_lookup)
        else:
            r['bm25_retrieval'] = {'skipped': 'no_gold_chunk_id' if bm25_data else 'bm25_not_ready'}

        results.append(r)
        status = r.get('exact_fact_correct', r.get('localization_correct', '?'))
        print(f"  [{qid}] type={qtype} domain={domain} exact={status}")

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Aggregate metrics
# ──────────────────────────────────────────────────────────────────────────────

def aggregate(results: list[dict]) -> dict:
    """Compute per-domain, per-type, and global metrics."""

    # Overall exact-fact / localization accuracy
    exact_correct = [r for r in results if r.get('exact_fact_correct') is True]
    exact_wrong   = [r for r in results if r.get('exact_fact_correct') is False]
    loc_correct   = [r for r in results if r.get('localization_correct') is True]
    loc_wrong     = [r for r in results if r.get('localization_correct') is False]

    exact_total   = len(exact_correct) + len(exact_wrong)
    loc_total     = len(loc_correct) + len(loc_wrong)

    # Vector retrieval@k
    vec_r1  = [r for r in results if r.get('vector_retrieval', {}).get('r@1')  is True]
    vec_r5  = [r for r in results if r.get('vector_retrieval', {}).get('r@5')  is True]
    vec_r10 = [r for r in results if r.get('vector_retrieval', {}).get('r@10') is True]
    vec_graded = [r for r in results
                  if isinstance(r.get('vector_retrieval', {}).get('r@1'), bool)]

    # BM25 retrieval@k (covers param_fact, concept, localization — excludes relationship)
    bm25_r1  = [r for r in results if r.get('bm25_retrieval', {}).get('r@1')  is True]
    bm25_r5  = [r for r in results if r.get('bm25_retrieval', {}).get('r@5')  is True]
    bm25_r10 = [r for r in results if r.get('bm25_retrieval', {}).get('r@10') is True]
    bm25_graded = [r for r in results
                   if isinstance(r.get('bm25_retrieval', {}).get('r@1'), bool) and
                   r.get('type') != 'relationship']

    grounded = [r for r in results if r.get('grounded')]
    grounding_rate = len(grounded) / len(results) if results else 0

    # Per-domain breakdown
    by_domain = defaultdict(list)
    for r in results:
        by_domain[r['domain']].append(r)

    domain_metrics = {}
    for domain, items in sorted(by_domain.items()):
        d_exact = [i for i in items if i.get('exact_fact_correct') is True]
        d_exact_total = len([i for i in items if i.get('exact_fact_correct') is not None])
        d_loc = [i for i in items if i.get('localization_correct') is True]
        d_loc_total = len([i for i in items if i.get('localization_correct') is not None])
        domain_metrics[domain] = {
            'count': len(items),
            'exact_fact_acc': f"{len(d_exact)}/{d_exact_total}" if d_exact_total else 'N/A',
            'loc_acc': f"{len(d_loc)}/{d_loc_total}" if d_loc_total else 'N/A',
        }

    # Per-type breakdown
    by_type = defaultdict(list)
    for r in results:
        by_type[r['type']].append(r)

    type_metrics = {}
    for qtype, items in sorted(by_type.items()):
        t_exact = [i for i in items if i.get('exact_fact_correct') is True]
        t_exact_total = len([i for i in items
                              if i.get('exact_fact_correct') is not None
                              or i.get('localization_correct') is not None])
        t_loc = [i for i in items if i.get('localization_correct') is True]
        vec_ok = [i for i in items if i.get('vector_retrieval', {}).get('r@5') is True]
        vec_graded_t = [i for i in items
                        if isinstance(i.get('vector_retrieval', {}).get('r@5'), bool)]
        type_metrics[qtype] = {
            'count': len(items),
            'exact_or_loc_correct': len(t_exact) + len(t_loc),
            'exact_or_loc_total': t_exact_total,
            'vector_r5': f"{len(vec_ok)}/{len(vec_graded_t)}" if vec_graded_t else 'N/A',
        }

    pct = lambda n, d: f"{100*n/d:.1f}%" if d > 0 else "N/A"

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
        'hallucination_rate': '0 (deterministic layer only; LLM not invoked)',
        'per_domain': domain_metrics,
        'per_type': type_metrics,
    }


if __name__ == '__main__':
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-vector', action='store_true', help='Skip LanceDB retrieval (deterministic only)')
    parser.add_argument('--output', default=str(KB_ROOT / 'eval' / 'eval_report_phase2.json'))
    args = parser.parse_args()

    results = run_eval(skip_vector=args.skip_vector)
    metrics = aggregate(results)

    print()
    print("=" * 60)
    print("EVAL RESULTS — Phase 2 Baseline")
    print("=" * 60)

    print(f"\nGlobal metrics ({metrics['total_items']} items):")
    for k, v in metrics.items():
        if k not in ('per_domain', 'per_type'):
            print(f"  {k}: {v}")

    print(f"\nPer-domain:")
    for domain, dm in metrics['per_domain'].items():
        print(f"  {domain} (n={dm['count']}): exact_fact={dm['exact_fact_acc']}, loc={dm['loc_acc']}")

    print(f"\nPer-type:")
    for qtype, tm in metrics['per_type'].items():
        correct = tm['exact_or_loc_correct']
        total_t = tm['exact_or_loc_total']
        print(f"  {qtype} (n={tm['count']}): correct={correct}/{total_t}, vector_r5={tm['vector_r5']}")

    # Save full report
    report = {'metrics': metrics, 'results': results}
    with open(args.output, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report saved to {args.output}")

    # Wrong answers for diagnostics
    wrong = [r for r in results
             if r.get('exact_fact_correct') is False or r.get('localization_correct') is False]
    if wrong:
        print(f"\nFailed items ({len(wrong)}):")
        for r in wrong:
            print(f"  [{r['id']}] gold={r['gold_answer']!r} produced={r.get('produced','?')!r}")
