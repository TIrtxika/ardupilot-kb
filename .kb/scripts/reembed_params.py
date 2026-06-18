#!/usr/bin/env python3
"""
Re-embed the 3406 param chunks (text now carries full_name) and update the
global LanceDB 'chunks' table in-place.

Steps:
1. Read param_chunks.jsonl (regenerated, has full_name in text)
2. Embed in batches via POST http://localhost:11434/api/embed (bge-m3)
3. Delete all existing param rows from the global 'chunks' table
4. Insert the new param rows (same schema, updated text + fresh vectors)
5. Verify row count is still 40,058
"""

import sys, json, time, urllib.request
from pathlib import Path

KB_ROOT = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import lancedb
import pyarrow as pa
import numpy as np

PARAM_JSONL   = KB_ROOT / 'chunks' / 'param_chunks.jsonl'
INDEX_DIR     = KB_ROOT / 'index'
EMBED_URL     = 'http://localhost:11434/api/embed'
EMBED_MODEL   = 'bge-m3'
EMBED_DIM     = 1024
BATCH_SIZE    = 64   # 64 texts per HTTP call — good balance for bge-m3
EXPECTED_ROWS = 40058


def embed_batch(texts: list[str]) -> list[list[float]] | None:
    payload = json.dumps({'model': EMBED_MODEL, 'input': texts}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            d = json.loads(resp.read())
            return d['embeddings']
    except Exception as e:
        print(f"  [embed] batch failed: {e}")
        return None


def load_param_chunks() -> list[dict]:
    chunks = []
    with open(PARAM_JSONL) as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def main():
    t0 = time.time()
    print("=" * 64)
    print("Re-embed param chunks (full_name fix) + update global LanceDB")
    print("=" * 64)

    # ── 1. Load chunks ────────────────────────────────────────────────────────
    chunks = load_param_chunks()
    print(f"Loaded {len(chunks)} param chunks from {PARAM_JSONL}")

    # ── 2. Embed in batches ───────────────────────────────────────────────────
    print(f"\nEmbedding {len(chunks)} texts (batch_size={BATCH_SIZE})…")
    vectors: list[list[float]] = []
    failed_indices: list[int] = []

    n_batches = (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_i in range(n_batches):
        start = batch_i * BATCH_SIZE
        end   = min(start + BATCH_SIZE, len(chunks))
        batch_texts = [chunks[i]['text'] for i in range(start, end)]

        t_batch = time.time()
        embs = embed_batch(batch_texts)
        elapsed_b = time.time() - t_batch

        if embs is None or len(embs) != len(batch_texts):
            print(f"  Batch {batch_i+1}/{n_batches} FAILED — zeroing {len(batch_texts)} vectors")
            for i in range(start, end):
                failed_indices.append(i)
            vectors.extend([[0.0] * EMBED_DIM] * len(batch_texts))
        else:
            vectors.extend(embs)
            if (batch_i + 1) % 10 == 0 or batch_i == n_batches - 1:
                pct = 100 * (batch_i + 1) / n_batches
                print(f"  Batch {batch_i+1}/{n_batches} ({pct:.0f}%) — {elapsed_b:.2f}s — total so far: {len(vectors)}")

    embed_time = time.time() - t0
    print(f"\nEmbedding done: {len(vectors)} vectors in {embed_time:.1f}s")
    if failed_indices:
        print(f"WARNING: {len(failed_indices)} chunks had failed embeddings (zero vectors): {failed_indices[:10]}...")

    # ── 3. Open LanceDB and read global table schema ──────────────────────────
    print("\nOpening LanceDB global table…")
    db = lancedb.connect(str(INDEX_DIR / 'lancedb'))
    global_tbl = db.open_table('chunks')
    before_count = global_tbl.count_rows()
    print(f"Row count before update: {before_count}")

    # ── 4. Delete existing param rows ─────────────────────────────────────────
    print("Deleting existing param rows (chunk_type = 'param')…")
    global_tbl.delete("chunk_type = 'param'")
    after_delete = global_tbl.count_rows()
    deleted = before_count - after_delete
    print(f"Deleted {deleted} param rows. Remaining: {after_delete}")

    # ── 5. Build PyArrow table matching existing schema ───────────────────────
    # Schema (no full_name column — it lives in the text):
    # chunk_id, chunk_type, domain, source_path, symbol_id(int64),
    # start_line(int64), end_line(int64), enclosing_class, section_path,
    # param_name, msg_name, default_val, range_min, range_max, units,
    # vehicle, text, vector(float[1024])

    print(f"\nBuilding PyArrow batch for {len(chunks)} param rows…")

    rows_chunk_id      = [c['chunk_id']    for c in chunks]
    rows_chunk_type    = ['param']          * len(chunks)
    rows_domain        = [c['domain']      for c in chunks]
    rows_source_path   = [c['source_path'] for c in chunks]
    rows_symbol_id     = [None]            * len(chunks)   # int64 nullable
    rows_start_line    = [c['start_line']  for c in chunks]
    rows_end_line      = [c['end_line']    for c in chunks]
    rows_enclosing_cls = [None]            * len(chunks)
    rows_section_path  = [None]            * len(chunks)
    rows_param_name    = [c['param_name']  for c in chunks]
    rows_msg_name      = [None]            * len(chunks)
    rows_default_val   = [c.get('default_val') for c in chunks]
    rows_range_min     = [c.get('range_min')   for c in chunks]
    rows_range_max     = [c.get('range_max')   for c in chunks]
    rows_units         = [c.get('units')        for c in chunks]
    rows_vehicle       = [c.get('vehicle')      for c in chunks]
    rows_text          = [c['text']        for c in chunks]
    rows_vector        = [v for v in vectors]

    # Convert vectors to fixed_size_list[float32, 1024]
    flat_vecs = []
    for v in rows_vector:
        flat_vecs.extend(v)
    vec_array = pa.FixedSizeListArray.from_arrays(
        pa.array(flat_vecs, type=pa.float32()), EMBED_DIM)

    arrow_batch = pa.table({
        'chunk_id':       pa.array(rows_chunk_id,      type=pa.string()),
        'chunk_type':     pa.array(rows_chunk_type,    type=pa.string()),
        'domain':         pa.array(rows_domain,        type=pa.string()),
        'source_path':    pa.array(rows_source_path,   type=pa.string()),
        'symbol_id':      pa.array(rows_symbol_id,     type=pa.int64()),
        'start_line':     pa.array(rows_start_line,    type=pa.int64()),
        'end_line':       pa.array(rows_end_line,      type=pa.int64()),
        'enclosing_class':pa.array(rows_enclosing_cls, type=pa.string()),
        'section_path':   pa.array(rows_section_path,  type=pa.string()),
        'param_name':     pa.array(rows_param_name,    type=pa.string()),
        'msg_name':       pa.array(rows_msg_name,      type=pa.string()),
        'default_val':    pa.array(rows_default_val,   type=pa.string()),
        'range_min':      pa.array(rows_range_min,     type=pa.string()),
        'range_max':      pa.array(rows_range_max,     type=pa.string()),
        'units':          pa.array(rows_units,          type=pa.string()),
        'vehicle':        pa.array(rows_vehicle,        type=pa.string()),
        'text':           pa.array(rows_text,           type=pa.string()),
        'vector':         vec_array,
    })
    print(f"Arrow batch: {len(arrow_batch)} rows, schema: {arrow_batch.schema}")

    # ── 6. Add new param rows ─────────────────────────────────────────────────
    print("\nAdding updated param rows to global table…")
    global_tbl.add(arrow_batch)

    after_insert = global_tbl.count_rows()
    print(f"Row count after insert: {after_insert}")

    # ── 7. Verify ─────────────────────────────────────────────────────────────
    if after_insert == EXPECTED_ROWS:
        print(f"Row count check PASSED: {after_insert} == {EXPECTED_ROWS}")
    else:
        print(f"Row count check FAILED: got {after_insert}, expected {EXPECTED_ROWS}")
        print(f"  Before delete: {before_count}, deleted: {deleted}, inserted: {len(chunks)}")

    # Spot-check: verify param_151 now has full_name in text
    spot = global_tbl.search().where("chunk_id = 'param_151'").limit(1).select(['chunk_id','text']).to_list()
    if spot:
        print(f"\nSpot-check param_151 text: {spot[0]['text'][:150]}")
    else:
        print("\nSpot-check param_151: NOT FOUND (investigate!)")

    total_time = time.time() - t0
    print(f"\nTotal time: {total_time:.1f}s")
    print(f"Re-embedded: {len(chunks)} param chunks")
    print(f"Failed embeddings: {len(failed_indices)}")
    print(f"Global row count: {after_insert}")


if __name__ == '__main__':
    main()
