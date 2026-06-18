---
description: Query the local KB end-to-end — route, retrieve, ground, audit — and return a cited answer. This mirrors the runtime serving path.
---

Answer the question in $ARGUMENTS using the full grounded pipeline:

1. **domain-classifier** subagent -> routing JSON (domains, needs_structured, needs_semantic,
   cross_domain_join).
2. If needs_structured: query `.kb/structured/kb.duckdb` for exact facts (defaults, signatures,
   callers, message fields). The table is authoritative.
3. If needs_semantic: retrieve from the selected domain indices (+ the shared crosscutting index),
   hybrid + rerank. For cross_domain_join, join via the symbol/param graph.
4. Draft the answer using the `grounded-answer` protocol (every sentence cited).
5. **retrieval-auditor** subagent -> verify every claim; strike or rewrite anything UNSUPPORTED
   or CONTRADICTED.
6. Return the audited, cited answer. If support is missing, say so against the pinned SHA — do
   not fill gaps from outside the corpus.

Note: this command uses Claude Code to exercise the pipeline. The production serving path under
`.kb/serve/` does the identical routing/retrieval/grounding using only local models (Ollama +
local embeddings), with no cloud dependency.
