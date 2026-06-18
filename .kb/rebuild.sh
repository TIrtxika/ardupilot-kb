#!/usr/bin/env bash
#
# Rebuild the ArduPilot KB from the cloned corpus, in the correct order.
# Prereqs: corpus cloned at .kb/corpus (see USAGE.md / .kb/MANIFEST.json for the pinned SHA),
#          .kb/venv with .kb/requirements.txt installed, Ollama running with bge-m3 pulled.
# Phase 2 dense embedding is CPU-bound (~1.5h for ~40k chunks); phase2_indexer.py is resumable.
#
set -euo pipefail
cd "$(dirname "$0")/.."        # repo root
V=.kb/venv/bin/python3

[ -d .kb/corpus/ardupilot ] || {
  echo "ERROR: .kb/corpus/ardupilot missing — clone the corpus first (see USAGE.md)." >&2
  exit 1
}

echo "== Phase 1: deterministic layer (symbols, params, messages) =="
$V .kb/build_symbol_graph.py            # tree-sitter symbols + call graph (DELETES & rebuilds DB)
$V .kb/scripts/libclang_augment.py      # libclang recovers #if-gated classes tree-sitter missed
$V .kb/scripts/build_params_messages.py # params + MAVLink/DroneCAN messages
$V .kb/scripts/fix_param_groups.py      # qualified param names (full_name = group + leaf)

echo "== Phase 2: retrieval index (CPU-bound dense embedding) =="
$V .kb/scripts/phase2_chunker.py        # AST/section chunks
$V .kb/scripts/phase2_indexer.py        # bge-m3 dense vectors -> LanceDB (resumable: --resume)
$V .kb/scripts/build_bm25.py            # BM25 lexical index

echo "== Phase 3: per-domain partition + router =="
$V .kb/scripts/phase3_build.py          # per-domain LanceDB tables + BM25
$V .kb/scripts/repack_index.py          # split large fragments (<25 MB) for git-friendliness

echo "== Eval (must pass before shipping) =="
$V .kb/scripts/serve_eval.py

echo "== Rebuild complete. =="
