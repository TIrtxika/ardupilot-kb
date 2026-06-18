#!/usr/bin/env python3
"""Build BM25 index independently of embedding."""
import sys, json, time, pickle
from pathlib import Path

KB_ROOT    = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CHUNKS_DIR = KB_ROOT / 'chunks'
INDEX_DIR  = KB_ROOT / 'index'

sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

from rank_bm25 import BM25Okapi

def load_all_chunks() -> list[dict]:
    chunk_files = [
        CHUNKS_DIR / 'cpp_chunks.jsonl',
        CHUNKS_DIR / 'rst_chunks.jsonl',
        CHUNKS_DIR / 'param_chunks.jsonl',
        CHUNKS_DIR / 'message_chunks.jsonl',
    ]
    all_chunks = []
    for cf in chunk_files:
        if not cf.exists():
            continue
        with open(cf) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_chunks.append(json.loads(line))
                    except Exception:
                        pass
        print(f"  {cf.name}: {len(all_chunks)} total so far")
    return all_chunks

print("Loading chunks...")
t0 = time.time()
chunks = load_all_chunks()
print(f"Loaded {len(chunks)} chunks in {time.time()-t0:.1f}s")

print("Tokenizing...")
t1 = time.time()
tokenized = [c.get('text', '').lower().split() for c in chunks]
chunk_ids = [c.get('chunk_id', '') for c in chunks]
print(f"Tokenized in {time.time()-t1:.1f}s")

print("Building BM25...")
t2 = time.time()
bm25 = BM25Okapi(tokenized)
print(f"BM25 built in {time.time()-t2:.1f}s, avgdl={bm25.avgdl:.1f}")

bm25_dir = INDEX_DIR / 'bm25'
bm25_dir.mkdir(parents=True, exist_ok=True)
with open(bm25_dir / 'bm25.pkl', 'wb') as f:
    pickle.dump({'bm25': bm25, 'chunk_ids': chunk_ids}, f)
with open(bm25_dir / 'bm25_meta.json', 'w') as f:
    json.dump({'chunk_count': len(chunks), 'avgdl': bm25.avgdl,
               'corpus_size': bm25.corpus_size}, f)

print(f"BM25 saved to {bm25_dir}")
print(f"Total time: {time.time()-t0:.1f}s")
