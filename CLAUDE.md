# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# ArduPilot Local Knowledge Base — Project Instructions

This repo builds a **fully local, offline, domain-decomposed knowledge system** over the
ArduPilot source repo + wiki. Claude Code is the **build/maintenance tool** here, not the
runtime. The delivered system runs without any cloud: local LLM (Ollama), local vector DB,
local embeddings. Do not introduce any cloud-runtime dependency into the system itself.

## Hard rules (non-negotiable — "no corner cutting")

1. **Deterministic layer first.** The symbol/call graph and the parameter/MAVLink tables are
   ground truth. Any factual claim about a symbol, file, parameter default, range, or message
   field MUST be answerable from these tables WITHOUT an LLM. The LLM only synthesizes prose
   on top of retrieved, verified facts.
2. **Eval before features.** No new retrieval/model/chunking change ships without measuring it
   against the gold eval set (.kb/eval/gold.jsonl). "It feels better" is not evidence.
3. **AST-aware chunking only.** Never split C++ by fixed character/line windows. Chunk on
   function/class/method boundaries via tree-sitter, preserving file + symbol context.
   RST docs are chunked on section boundaries.
4. **Grounding + citations are mandatory.** Every answer cites file:line and/or symbol id and
   is cross-checked against the symbol table by the retrieval-auditor. Unverifiable claim => drop it.
5. **Domains map to ArduPilot architecture, not invented categories.** See the
   `ardupilot-domains` skill for the canonical taxonomy.
6. **No fine-tuning until retrieval is proven insufficient by eval.** LoRA adapters are Phase 4,
   gated on measured gaps, never the default.
7. **Pin everything to a git SHA.** The KB records the ArduPilot commit it was built from.
   Re-index is tied to git, never a one-off.

## Layout produced by this project

```
.kb/
  corpus/        # cloned ardupilot + ardupilot_wiki (read-only, pinned SHA)
  structured/    # DuckDB: symbols, edges, params, messages  (deterministic layer)
  chunks/        # AST/section chunks with metadata (jsonl)
  index/         # per-domain vector indices (LanceDB) + BM25
  eval/          # gold.jsonl + eval reports
  serve/         # local CLI/API that routes -> retrieves -> grounds -> answers
```

## Local runtime stack (the system itself)

- Parsing: tree-sitter (tree-sitter-cpp), docutils for RST.
- Structured store: DuckDB (embedded, no server).
- Embeddings: Qwen3-Embedding-8B (best MTEB-Code) or BGE-M3 (hybrid dense+sparse). Run via
  local server or Ollama embeddings. Pick one, record it, keep it fixed per index version.
- Vector store: LanceDB (embedded; fully offline, no docker needed).
- Lexical: BM25 (DuckDB FTS or tantivy) for hybrid retrieval.
- Reranker: bge-reranker-v2 (local).
- Generation: Ollama — Qwen 3.6 27B (balanced) or Devstral Small 24B (agentic). Qwen3 8B on low VRAM.

**Pinned for this deployment** (AMD Phoenix APU, ~512 MB VRAM, 30 GB RAM → CPU inference; see
`.claude/settings.json`): generation `qwen3-30b` (Qwen3-30B-**A3B** MoE, ~3B active → CPU-fast),
embeddings `bge-m3` (hybrid dense+sparse, CPU-feasible). The aspirational `Qwen3-Embedding-8B`
is too heavy for CPU here; `Qwen3-Embedding-0.6B` is the code-optimized upgrade path if eval
shows BGE-M3 underperforms on code retrieval. Reranker deferred (Ollama serves rerankers poorly;
use a `sentence-transformers` CPU reranker if/when eval justifies it).

## Dev vs runtime — what may use cloud

The "no cloud" rule constrains the **delivered runtime**, not the build process. Claude Code
itself is cloud and is the sanctioned build tool. The boundary, by component:

- **May use cloud (not part of the shipped artifact):** orchestration/scripting via Claude Code,
  and prototyping answer prompts to iterate faster. These are drafts, never an eval number.
- **Must be local (becomes the artifact or substitutes for the real serving path):**
  - *Embeddings for any index that ships.* An index built with model X **requires model X at
    query time** — query and document vectors must come from the same model. Build the shipped
    index with the local embedder (`KB_EMBED_MODEL`) from the start; a cloud-built index cannot
    be served locally.
  - *Eval numbers that gate a ship/revert decision* (rule #2). Eval must exercise the real
    serving path, so generation (`KB_GEN_MODEL`) and reranking run on the local stack. A number
    produced with a cloud model measures a different system and cannot gate anything.
- **No model at all:** the deterministic layer (symbol graph, params, messages) is tree-sitter +
  DuckDB — the cloud-vs-local question does not arise.

Consequence: Phase 0/1 (`/ingest` → structured layer → `/eval build`) needs no models and can run
without Ollama. Local models become mandatory at Phase 2 (indexing) and for any deciding eval.
Do not substitute cloud models there to "unblock" — re-order work instead.

## Build phases (run in order)

- Phase 0 — `corpus-ingestor` + `eval-builder`: inventory corpus, build gold eval set.
- Phase 1 — `symbol-grapher` + `param-extractor`: deterministic structured layer.
- Phase 2 — `rag-indexer`: AST chunking + single hybrid index. Measure on eval.
- Phase 3 — `domain-classifier` + `rag-indexer`: per-domain indices + router. Measure.
- Phase 4 — LoRA adapters, ONLY if Phase 2/3 eval shows a real gap.

## Current repository state

This repo currently contains **only the scaffold**: `CLAUDE.md`, `README.md`, and `.claude/`
(7 subagents, 5 skills, 4 slash commands, `settings.json`). It is **not a git repo** and the
entire `.kb/` tree is **generated, not committed** — it does not exist until you run `/ingest`.
Do not assume the corpus, DuckDB, or indices are present; check, and build them in phase order.

## Commands & conventions

There is no compiler/linter/unit-test suite for this repo. **The eval set is the test:**
`/eval` against `.kb/eval/gold.jsonl` is how every change is validated (see hard rule #2).

- `/ingest [SHA|tag]` — Phase 0/1: clone corpus at a pinned SHA + build the deterministic layer.
- `/eval build` — author the gold set; `/eval` — run it and report metrics as deltas vs. previous.
- `/index-domain` — Phase 2 global index; `/index-domain <domain>` — Phase 3 per-domain index.
  Always runs `/eval` after and recommends reverting if the delta did not improve.
- `/ask <question>` — full query path (route → retrieve → ground → audit), mirrors `.kb/serve/`.

Canonical names come from `.claude/settings.json` env, not from prose — keep them consistent:
`KB_ROOT=.kb`, `KB_EMBED_MODEL=Qwen3-Embedding-8B`, `KB_GEN_MODEL=qwen3:27b`,
`KB_VECTOR_STORE=lancedb`. The single structured store is `.kb/structured/kb.duckdb`.

Guardrails enforced by `settings.json`: `.kb/corpus/**` is **read-only** (Write/Edit denied) —
to change corpus content, re-pin to a new SHA via `/ingest`, never edit clones in place.
`rm -rf` is denied; `curl`/`wget` prompt. Allowed Bash is scoped to `git clone`,
`git -C .kb/corpus`, `python`/`python3`, `pip install`, `uv`, `ollama`, `duckdb`.

## How to drive this

Use the slash commands in `.claude/commands/` (`/ingest`, `/index-domain`, `/ask`, `/eval`).
For larger jobs delegate to the subagents in `.claude/agents/` (Claude auto-delegates by
description, or call explicitly e.g. "Use the symbol-grapher subagent on libraries/AP_NavEKF").

Reminder: subagents do NOT inherit my skills — each agent declares the skills it needs in its
own frontmatter. Edits to agent files on disk require a session restart to take effect.
