---
description: Phase 2/3 — (re)build retrieval indices. With no args, build the single global index; with a domain name, build that domain's index. Always runs eval after.
---

Use the **rag-indexer** subagent.

- No $ARGUMENTS -> Phase 2: one global hybrid index (dense + BM25 + reranker) over AST/section
  chunks.
- $ARGUMENTS = a domain from the `ardupilot-domains` taxonomy -> Phase 3: build that per-domain
  index plus refresh the shared `infra_crosscutting` index.

After building, run the eval (`/eval`) and report retrieval deltas. If eval did not improve,
recommend reverting and explain why.
