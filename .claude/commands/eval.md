---
description: Build or run the gold evaluation set and report metrics with deltas vs the previous index.
---

If `.kb/eval/gold.jsonl` is missing or $ARGUMENTS says "build": use the **eval-builder** subagent
to author the gold set across all domains and types.

Otherwise: run the eval per the `kb-eval` skill against the current indices and report
Retrieval@k, exact-fact accuracy, localization accuracy, grounding rate, and hallucination rate,
broken down per domain and per type, as deltas vs the previous index version.

Never report "looks better" — report numbers. A change within noise does not ship.
