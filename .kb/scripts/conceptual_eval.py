#!/usr/bin/env python3
"""
Rubric eval for LLM-path CONCEPTUAL answers (which token-grading can't measure).
For each conceptual_gold.jsonl item: run ask.respond(), then score
  - key_fact recall: fraction of the must-include facts present in the answer,
  - grounded: answer carries >=1 [file:line] citation (when expect_citation),
  - refused: answer is a refusal (should be False for these answerable questions).
Reports per-item + aggregate. Honest proxy for conceptual quality / regression detection.
"""
import sys, json, re
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'serve'))
import ask

CITE = re.compile(r'\[[^\]]+?:\d+(?:-\d+)?\]')
items = [json.loads(l) for l in open(KB / 'eval' / 'conceptual_gold.jsonl')]
tot_recall = cited_ok = refused = full = 0
for it in items:
    r = ask.respond(it['question'])
    ans = r['final']
    al = ans.lower()
    hits = [kf for kf in it['key_facts'] if kf.lower() in al]
    recall = len(hits) / len(it['key_facts'])
    is_cited = bool(CITE.search(ans))
    is_ref = 'not supported by the indexed corpus' in al
    tot_recall += recall
    full += 1 if recall == 1.0 else 0
    cited_ok += 1 if is_cited else 0
    refused += 1 if is_ref else 0
    miss = [kf for kf in it['key_facts'] if kf.lower() not in al]
    print(f"[{it['id']:34}] keyfacts {len(hits)}/{len(it['key_facts'])} cited={is_cited} "
          f"refused={is_ref} mode={r['mode']}" + (f"  miss={miss}" if miss else ""))

n = len(items)
print(f"\n=== conceptual eval: {n} items ===")
print(f"mean key-fact recall: {tot_recall/n:.0%}")
print(f"fully covered (all key facts): {full}/{n}")
print(f"grounded (cited): {cited_ok}/{n}   refused (should be 0): {refused}/{n}")
