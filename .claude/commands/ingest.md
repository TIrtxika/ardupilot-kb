---
description: Phase 0/1 — clone + inventory the ArduPilot corpus at a pinned SHA, then build the deterministic structured layer (symbol graph, params, messages).
---

Run the build pipeline up to the deterministic layer.

1. Use the **corpus-ingestor** subagent to clone ArduPilot + wiki into `.kb/corpus` at an
   explicit SHA and write MANIFEST.json + inventory.json. If $ARGUMENTS contains a commit/tag,
   pin to it; otherwise pin to current HEAD and record the resolved SHA.
2. Use the **symbol-grapher** subagent to build `.kb/structured/kb.duckdb` (symbols, edges).
3. Use the **param-extractor** subagent to populate params + messages in the same DuckDB.
4. Print a summary: SHAs, table row counts per domain, and any parse failures (do not hide them).
