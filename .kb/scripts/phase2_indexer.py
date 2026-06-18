#!/usr/bin/env python3
"""
Phase 2 — Embedding + LanceDB + BM25 indexer for ArduPilot KB.

Reads all chunk jsonl files from .kb/chunks/, embeds via bge-m3 (Ollama HTTP API),
stores vectors in LanceDB incrementally (checkpoint per write-batch), builds BM25.

Usage:
  python3 phase2_indexer.py [--resume] [--max-chunks N]
"""

import os, sys, json, time, datetime, argparse, pickle
from pathlib import Path

KB_ROOT    = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CHUNKS_DIR = KB_ROOT / 'chunks'
INDEX_DIR  = KB_ROOT / 'index'

sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import numpy as np
import urllib.request
import pyarrow as pa
import lancedb
from rank_bm25 import BM25Okapi

INDEX_DIR.mkdir(parents=True, exist_ok=True)

EMBED_MODEL   = 'bge-m3'
EMBED_DIM     = 1024
EMBED_URL     = 'http://localhost:11434/api/embed'
EMBED_BATCH   = 32      # chunks per embed API call
WRITE_BATCH   = 256     # chunks per LanceDB write (after embedding WRITE_BATCH/EMBED_BATCH embed calls)
GIT_SHA       = '20622a390035d439268cb40583b8fb62c033ed50'
WIKI_SHA      = 'f8c078cb7cc3df01a0987dfba9d3b2b475055f7e'

PROGRESS_FILE = INDEX_DIR / 'indexer_progress.json'

# PyArrow schema for LanceDB
PA_SCHEMA = pa.schema([
    pa.field('chunk_id',       pa.string()),
    pa.field('chunk_type',     pa.string()),
    pa.field('domain',         pa.string()),
    pa.field('source_path',    pa.string()),
    pa.field('symbol_id',      pa.int64(),    nullable=True),
    pa.field('start_line',     pa.int64()),
    pa.field('end_line',       pa.int64()),
    pa.field('enclosing_class',pa.string(),   nullable=True),
    pa.field('section_path',   pa.string(),   nullable=True),
    pa.field('param_name',     pa.string(),   nullable=True),
    pa.field('msg_name',       pa.string(),   nullable=True),
    pa.field('default_val',    pa.string(),   nullable=True),
    pa.field('range_min',      pa.string(),   nullable=True),
    pa.field('range_max',      pa.string(),   nullable=True),
    pa.field('units',          pa.string(),   nullable=True),
    pa.field('vehicle',        pa.string(),   nullable=True),
    pa.field('text',           pa.string()),
    pa.field('vector',         pa.list_(pa.float32(), EMBED_DIM)),
])


def embed_batch(texts: list[str], retries: int = 3) -> list[list[float]] | None:
    payload = json.dumps({'model': EMBED_MODEL, 'input': texts}).encode('utf-8')
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                EMBED_URL, data=payload, headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                d = json.loads(resp.read())
                embeddings = d['embeddings']
                if len(embeddings) != len(texts):
                    raise ValueError(f"Expected {len(texts)}, got {len(embeddings)}")
                return embeddings
        except Exception as e:
            if attempt < retries - 1:
                print(f"  [embed] Retry {attempt+1}: {e}", flush=True)
                time.sleep(2)
            else:
                print(f"  [embed] FAILED: {e}", flush=True)
                return None
    return None


def load_all_chunks(max_chunks: int | None = None) -> list[dict]:
    chunk_files = [
        CHUNKS_DIR / 'cpp_chunks.jsonl',
        CHUNKS_DIR / 'rst_chunks.jsonl',
        CHUNKS_DIR / 'param_chunks.jsonl',
        CHUNKS_DIR / 'message_chunks.jsonl',
    ]
    all_chunks = []
    for cf in chunk_files:
        if not cf.exists():
            print(f"  [load] WARNING: {cf} not found", flush=True)
            continue
        n = 0
        with open(cf) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_chunks.append(json.loads(line))
                        n += 1
                    except Exception:
                        pass
        print(f"  [load] {cf.name}: {n} chunks", flush=True)
        if max_chunks and len(all_chunks) >= max_chunks:
            all_chunks = all_chunks[:max_chunks]
            break
    return all_chunks


def chunk_to_record(c: dict, vec: list[float]) -> dict:
    return {
        'chunk_id':        str(c.get('chunk_id', '')),
        'chunk_type':      str(c.get('chunk_type', '')),
        'domain':          str(c.get('domain', 'other')),
        'source_path':     str(c.get('source_path', '')),
        'symbol_id':       c.get('symbol_id'),
        'start_line':      int(c.get('start_line', 0) or 0),
        'end_line':        int(c.get('end_line', 0) or 0),
        'enclosing_class': c.get('enclosing_class'),
        'section_path':    c.get('section_path'),
        'param_name':      c.get('param_name'),
        'msg_name':        c.get('msg_name'),
        'default_val':     c.get('default_val'),
        'range_min':       c.get('range_min'),
        'range_max':       c.get('range_max'),
        'units':           c.get('units'),
        'vehicle':         c.get('vehicle'),
        'text':            str(c.get('text', ''))[:8000],
        'vector':          [float(x) for x in vec],
    }


def build_index(chunks: list[dict], resume_from: int = 0):
    total = len(chunks)
    db = lancedb.connect(str(INDEX_DIR / 'lancedb'))

    if resume_from == 0:
        try:
            db.drop_table('chunks')
            print("[indexer] Dropped existing 'chunks' table", flush=True)
        except Exception:
            pass
        table = None
    else:
        try:
            table = db.open_table('chunks')
            print(f"[indexer] Resuming from chunk {resume_from}, table has {table.count_rows()} rows", flush=True)
        except Exception:
            table = None
            resume_from = 0

    failed_ids = []
    t0 = time.time()
    write_buffer = []
    indexed = resume_from

    print(f"[indexer] Starting embedding from chunk {resume_from}/{total}...", flush=True)

    for i in range(resume_from, total, EMBED_BATCH):
        batch_chunks = chunks[i:i + EMBED_BATCH]
        texts = [c.get('text', '')[:6000] for c in batch_chunks]

        vecs = embed_batch(texts)
        if vecs is None:
            # Use zero vectors for failed batch, log chunk ids
            vecs = [[0.0] * EMBED_DIM for _ in batch_chunks]
            for c in batch_chunks:
                failed_ids.append(c.get('chunk_id', ''))

        for c, vec in zip(batch_chunks, vecs):
            write_buffer.append(chunk_to_record(c, vec))

        indexed = i + len(batch_chunks)

        # Flush write buffer to LanceDB every WRITE_BATCH chunks
        if len(write_buffer) >= WRITE_BATCH or indexed >= total:
            if write_buffer:
                if table is None:
                    table = db.create_table('chunks', data=write_buffer, schema=PA_SCHEMA, mode='create')
                else:
                    table.add(write_buffer)
                write_buffer = []

            # Save progress checkpoint
            elapsed = time.time() - t0
            rate = (indexed - resume_from) / elapsed if elapsed > 0 else 1
            eta = (total - indexed) / rate if rate > 0 else 0
            progress = {
                'indexed': indexed,
                'total': total,
                'elapsed_s': round(elapsed, 1),
                'rate_chunks_per_s': round(rate, 2),
                'eta_s': round(eta, 0),
                'failed_count': len(failed_ids),
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            }
            with open(PROGRESS_FILE, 'w') as f:
                json.dump(progress, f)
            print(f"  [{indexed}/{total}] {elapsed:.0f}s, {rate:.1f} ch/s, ETA {eta/60:.1f}min, "
                  f"fails={len(failed_ids)}", flush=True)

    return table, indexed, failed_ids


def build_bm25(chunks: list[dict]) -> None:
    print(f"[indexer] Building BM25 over {len(chunks)} chunks...", flush=True)
    tokenized = [c.get('text', '').lower().split() for c in chunks]
    chunk_ids = [c.get('chunk_id', '') for c in chunks]

    bm25 = BM25Okapi(tokenized)

    bm25_dir = INDEX_DIR / 'bm25'
    bm25_dir.mkdir(parents=True, exist_ok=True)
    with open(bm25_dir / 'bm25.pkl', 'wb') as f:
        pickle.dump({'bm25': bm25, 'chunk_ids': chunk_ids}, f)
    with open(bm25_dir / 'bm25_meta.json', 'w') as f:
        json.dump({'chunk_count': len(chunks), 'avgdl': bm25.avgdl,
                   'corpus_size': bm25.corpus_size}, f)
    print(f"  BM25 saved to {bm25_dir}", flush=True)


def write_manifest(total_chunks: int, indexed: int, failed_count: int,
                   elapsed: float, vector_index_created: bool):
    manifest = {
        'version': '2.0',
        'phase': 2,
        'build_date': datetime.datetime.utcnow().isoformat() + 'Z',
        'ardupilot_sha': GIT_SHA,
        'wiki_sha': WIKI_SHA,
        'embed_model': EMBED_MODEL,
        'embed_dim': EMBED_DIM,
        'vector_store': 'lancedb',
        'lexical_index': 'bm25_okapi (rank_bm25)',
        'reranker': 'not_deployed_phase2',
        'total_chunks': total_chunks,
        'indexed_chunks': indexed,
        'failed_embed_chunks': failed_count,
        'vector_index_created': vector_index_created,
        'index_dir': str(INDEX_DIR),
        'elapsed_seconds': round(elapsed, 1),
        'notes': 'Phase 2: single global index. Per-domain is Phase 3.',
    }
    manifest_path = INDEX_DIR / 'manifest.json'
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"[indexer] Manifest written to {manifest_path}", flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-chunks', type=int, default=None)
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("Phase 2 — Embedding + LanceDB + BM25 Indexer", flush=True)
    print("=" * 60, flush=True)

    resume_from = 0
    if args.resume and PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            prog = json.load(f)
        resume_from = prog.get('indexed', 0)
        print(f"[indexer] Resuming from chunk {resume_from}", flush=True)

    t_start = time.time()

    print("[indexer] Loading chunks...", flush=True)
    chunks = load_all_chunks(args.max_chunks)
    print(f"[indexer] Total chunks: {len(chunks)}", flush=True)

    table, indexed, failed_ids = build_index(chunks, resume_from)

    # BM25 over all chunks (fast, in-memory)
    build_bm25(chunks)

    # Try to create ANN index
    vector_index_ok = False
    if table is not None:
        print("[indexer] Creating ANN vector index...", flush=True)
        try:
            n_rows = table.count_rows()
            table.create_index(
                metric='cosine',
                num_partitions=min(256, max(1, n_rows // 100)),
                num_sub_vectors=32,
            )
            vector_index_ok = True
            print("[indexer] ANN index created", flush=True)
        except Exception as e:
            print(f"[indexer] ANN index skipped (brute-force fallback): {e}", flush=True)

    elapsed = time.time() - t_start
    manifest = write_manifest(len(chunks), indexed, len(failed_ids), elapsed, vector_index_ok)

    if failed_ids:
        with open(INDEX_DIR / 'embed_failures.json', 'w') as f:
            json.dump(failed_ids, f)

    print()
    print("=" * 60, flush=True)
    print("INDEXING COMPLETE", flush=True)
    for k, v in manifest.items():
        print(f"  {k}: {v}", flush=True)
    print("=" * 60, flush=True)
