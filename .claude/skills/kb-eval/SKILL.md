---
name: kb-eval
description: How to build, run, and report the ArduPilot KB evaluation. Use when authoring gold questions or measuring any retrieval/model/chunking change. Defines the gold schema, metrics, and the rule that no change ships without an eval number.
---

# KB Evaluation

You cannot improve what you don't measure. Every change is judged against the gold set.

## Gold schema (`.kb/eval/gold.jsonl`, one JSON object per line)

```
{ "id": "param-angle-max-default",
  "question": "What is the default and range of the Copter ANGLE_MAX parameter?",
  "domain": "control",
  "type": "param_fact",            // param_fact | localization | relationship | concept
  "gold_answer": "...",
  "gold_support": "SELECT range_min, range_max FROM params WHERE name='ANGLE_MAX' AND vehicle='Copter'",
  "notes": "" }
```

`gold_support` is either a DuckDB query (exact-fact types) or a `file:line` / `wiki:section`
locator (localization/concept). It must allow automatic grading with no human in the loop.

## Metrics to report

- **Retrieval@k** (k=1,5,10): did the gold support appear in retrieved chunks/rows?
- **Exact-fact accuracy**: for param_fact/relationship, does the produced value equal the gold
  table value? (string/number exact match, not "close").
- **Localization accuracy**: did the answer cite the correct file/class?
- **Grounding rate**: fraction of answer sentences carrying a valid citation (target: 100%).
- **Hallucination rate**: claims the auditor marked UNSUPPORTED/CONTRADICTED per 100 answers.

## Process rules

1. Keep a held-out split; never tune chunking/retrieval on it.
2. Report deltas vs the previous index version, per domain and per type.
3. A change that does not move eval (within noise) does not ship. State that plainly.
4. Over-weight cross-cutting (params/HAL) cases — they expose naive systems first.
