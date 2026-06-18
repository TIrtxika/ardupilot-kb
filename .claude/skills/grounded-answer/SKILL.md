---
name: grounded-answer
description: The grounding and citation protocol for the ArduPilot KB. Use when drafting or verifying any answer. Defines what counts as support, how to cite, and the rule that unverifiable claims are removed, not softened.
---

# Grounded Answer Protocol

The system must never produce a confident claim it cannot back. "No corner cutting" means: if it
isn't in the deterministic tables or a retrieved chunk, it does not go in the answer.

## What counts as support

- **Exact facts** (parameter default/range/units, function signature, caller/subclass relation,
  message field): a row in `.kb/structured/kb.duckdb`. The table is authoritative over prose.
- **Explanations / how-it-works**: one or more retrieved chunks with `source_path:start-end`.

## Citation format

Every factual sentence ends with its support: `[AP_Motors/AP_MotorsMatrix.cpp:142]` for code,
`[wiki: Copter > Flight Modes > Loiter]` for docs, or `[params: ANGLE_MAX]` for a table fact.
A claim with no citation is not allowed to remain.

## Drafting rules

1. Answer the exact-fact part from DuckDB first; only then add prose from chunks.
2. Do not paraphrase a default/range/signature — quote it as stored.
3. If retrieval did not return support for part of the question, say "not found in the indexed
   corpus at <SHA>" — do not fill the gap from prior knowledge.

## Verification rules (for the auditor)

- Each claim -> VERIFIED / UNSUPPORTED / CONTRADICTED.
- UNSUPPORTED or CONTRADICTED -> remove or rewrite to the evidence. Never weaken wording to make
  an unverifiable claim "technically okay". Fail closed.
