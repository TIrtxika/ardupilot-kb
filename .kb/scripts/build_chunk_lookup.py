#!/usr/bin/env python3
"""Build a lightweight chunk_id -> {source_path, chunk_type} lookup for BM25 eval."""
import sys, json
from pathlib import Path

KB_ROOT    = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CHUNKS_DIR = KB_ROOT / 'chunks'
INDEX_DIR  = KB_ROOT / 'index'

lookup = {}

chunk_files = [
    CHUNKS_DIR / 'cpp_chunks.jsonl',
    CHUNKS_DIR / 'rst_chunks.jsonl',
    CHUNKS_DIR / 'param_chunks.jsonl',
    CHUNKS_DIR / 'message_chunks.jsonl',
]

for cf in chunk_files:
    if not cf.exists():
        continue
    with open(cf) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    c = json.loads(line)
                    lookup[c['chunk_id']] = {
                        'source_path': c.get('source_path', ''),
                        'chunk_type': c.get('chunk_type', ''),
                        'start_line': c.get('start_line', 0),
                        'domain': c.get('domain', ''),
                    }
                except Exception:
                    pass

out_path = INDEX_DIR / 'chunk_lookup.json'
with open(out_path, 'w') as f:
    json.dump(lookup, f)
print(f"Saved {len(lookup)} entries to {out_path}")
