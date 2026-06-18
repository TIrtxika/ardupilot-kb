---
name: ast-chunking
description: How to chunk ArduPilot C++ and RST documentation for retrieval without destroying meaning. Use whenever producing chunks for embedding/indexing. Forbids fixed-size character/line splitting of code.
---

# AST / Section-Aware Chunking

Naive fixed-window splitting of C++ is the top cause of bad code RAG. Do not do it.

## C++ (tree-sitter-cpp)

- One chunk = one top-level unit: a function, a method, a class/struct (small classes whole;
  large classes split per method with a class-header chunk that lists the members).
- Carry into chunk metadata (not just text): `source_path`, `enclosing_class`, `symbol_id`
  (FK to the symbols table), `start_line`, `end_line`, and the list of `#include`s in the file.
- Prepend a one-line synthetic header to the chunk text: `// <qualified_name> @ <path>:<start>-<end>`
  so the embedding has anchoring context even when the body is short.
- Keep a chunk under ~1500 tokens; if a single function exceeds that, split on logical blocks
  but never mid-statement, and link the parts with `part_of` metadata.
- Do not strip comments — ArduPilot doc-comments (`// @Param`, behavior notes) are high-signal.

## RST docs

- Chunk on section/subsection boundaries (heading hierarchy), not byte windows.
- Keep the heading path in metadata (e.g. "Copter > Flight Modes > Loiter").
- Code blocks inside docs stay attached to their section.

## Structured rows (params / messages)

- Emit each param row and each message definition as its own structured chunk straight from
  DuckDB, with the canonical fields in the text and `symbol_id`/`param_name` in metadata. These
  are exact-fact chunks; retrieval may surface them, but the deterministic table is authoritative.

## Invariants

- Every chunk is traceable to `source_path:start-end`. No orphan chunks.
- Same embedding model across an entire index version; record it in the index manifest.
