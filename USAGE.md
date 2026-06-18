# ArduPilot Local KB — Usage & Local-Run Guide

A fully local, offline question-answering system over the ArduPilot source + wiki.
Pipeline: **route → retrieve (deterministic DuckDB + semantic LanceDB) → ground → generate
(local LLM) → deterministic audit → cited answer.** No cloud at runtime.

Built from ArduPilot commit `20622a390035d439268cb40583b8fb62c033ed50` (see `.kb/BUILD_STATE.md`).

---

## 1. Prerequisites

**Ollama** (local model server) with these models:
```bash
ollama pull bge-m3        # embeddings (1024-dim) — required for indexing & queries
ollama pull llama3.1:8b   # generation (~4.9 GB, 24 tok/s CPU) — current KB_GEN_MODEL
# optional, only if you have >14 GB free RAM:
ollama pull qwen3-30b     # higher-quality generation (MoE A3B)
```
Models/paths are recorded in `.claude/settings.json` (`KB_EMBED_MODEL`, `KB_GEN_MODEL`).

**Python** (3.12+; a venv lives at `.kb/venv`):
```bash
python3 -m venv .kb/venv
.kb/venv/bin/pip install -r .kb/requirements.txt   # duckdb, lancedb, tree-sitter(-cpp), rank-bm25, libclang, pyarrow, numpy
```

**Hardware note:** built/tested on an AMD APU (CPU inference, ~512 MB VRAM, 30 GB RAM).
`qwen3-30b` (14 GB) needs >14 GB free RAM or it thrashes — `llama3.1:8b` is the working default.

---

## 2. Quick start — ask a question

If the KB is already built (`.kb/structured/kb.duckdb` + `.kb/index/` present):
```bash
.kb/venv/bin/python3 .kb/serve/ask.py "What is the default value and range of EK3_HGT_DELAY?"
.kb/venv/bin/python3 .kb/serve/ask.py "How does Return to Launch (RTL) mode work in ArduCopter?"
```
- **Exact-fact** questions (param default/range/units, message ID, "which file defines X")
  are answered DIRECTLY from the deterministic DuckDB layer (instant, authoritative).
- **Conceptual / how-to** questions go through retrieval + the local LLM, then the
  deterministic auditor strikes any claim it can't ground. Every claim is cited `[file:line]`.
- If the corpus doesn't contain the answer, you get: `Not supported by the indexed corpus.`
  (It will NOT fabricate — see the case studies in `.kb/BUILD_STATE.md`.)

Ollama must be running (`ollama serve`) and `bge-m3` + the gen model pulled.

Optional stricter grounding: set `KB_SEMANTIC_AUDIT=1` to add an entailment check that strikes
conceptual claims not actually supported by their cited chunk (catches ~70% of semantic
hallucinations the deterministic auditor misses, 0 false positives on the adversarial set). Costs
~+10-20s per conceptual query on CPU; off by default. Set `KB_GEN_MODEL=qwen3-30b` if >14 GB RAM.

The judge backend is selectable with `KB_SEMANTIC_JUDGE`:
- `llm` (default) — reuses the gen model; no extra deps; +10-20s/query.
- `nli` — local NLI cross-encoder (same 70%/0-FP quality, ~100ms/pair after a one-time ~35s model
  load). Better for a persistent server, worse for one-shot CLI. Needs `pip install -r
  .kb/requirements-nli.txt` (~2 GB torch) and downloads cross-encoder/nli-deberta-v3-base.

---

## 3. Rebuild the KB from scratch

Only needed to (re)index or to pin a new ArduPilot commit. Run **in this order** (the symbol
grapher deletes & rebuilds the DB, so params/messages MUST be rebuilt after it):

```bash
V=.kb/venv/bin/python3
# Phase 0 — corpus (clone ardupilot + wiki at a pinned SHA into .kb/corpus, init submodules
#   modules/mavlink + modules/DroneCAN/DSDL). See .kb/MANIFEST.json for the recorded SHAs.
# Phase 1 — deterministic layer:
$V .kb/build_symbol_graph.py            # tree-sitter symbols + call graph -> kb.duckdb
$V .kb/scripts/libclang_augment.py      # libclang recovers classes tree-sitter missed (#if-gated)
$V .kb/scripts/build_params_messages.py # params + MAVLink/DroneCAN messages
$V .kb/scripts/fix_param_groups.py      # qualified param names (full_name = group + leaf)
# Phase 2 — retrieval index (CPU-bound; dense embedding ~1.5h for 40k chunks):
$V .kb/scripts/phase2_chunker.py        # AST/section chunks -> .kb/chunks/*.jsonl
$V .kb/scripts/phase2_indexer.py        # bge-m3 dense vectors -> LanceDB  (resumable: --resume)
$V .kb/scripts/build_bm25.py            # BM25 lexical index
# Phase 3 — per-domain partition + router:
$V .kb/scripts/phase3_build.py          # per-domain LanceDB tables + BM25 (reuses embeddings)
$V .kb/scripts/repack_index.py          # split large LanceDB fragments (<25 MB) so the index is git-friendly
```

---

## 4. Evaluate

Gold set: `.kb/eval/gold.jsonl` (tunable) + `.kb/eval/gold_heldout.jsonl` (never tune on it).
```bash
.kb/venv/bin/python3 .kb/scripts/serve_eval.py   # end-to-end: answer accuracy per type
.kb/venv/bin/python3 .kb/scripts/phase3_eval.py  # retrieval Retrieval@k (routed)
```
Project rule: no retrieval/model/chunking change ships without an eval number (see CLAUDE.md).
Current: serve answer accuracy **55/55 (100%)** on graded items; deterministic layer 100%.

---

## 5. How it works (one paragraph)

`ask.py` routes the query to domains (`phase3_router.py`), pulls exact facts from `kb.duckdb`
(params/messages/symbols), and retrieves AST/section chunks from the routed per-domain LanceDB
indices (bge-m3). Exact-fact questions are answered straight from the verified DB; conceptual
ones are synthesized by the local LLM over the retrieved chunks. A deterministic auditor
(`audit()`) then removes any sentence with a fabricated number, an invalid citation, or
speculation — failing closed to `Not supported by the indexed corpus.` rather than guessing.
```
