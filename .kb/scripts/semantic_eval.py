#!/usr/bin/env python3
"""
Measure the semantic judge (#3) on the adversarial set (.kb/eval/semantic_gold.jsonl).
Reports confusion matrix + precision/recall for catching UNSUPPORTED claims, and the
false-positive rate (supported claims wrongly flagged — these would over-strike real answers).
"""
import sys, json
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'serve'))
import ask

items = [json.loads(l) for l in open(KB / 'eval' / 'semantic_gold.jsonl')]
# tp = unsupported correctly flagged; fn = unsupported missed; fp = supported wrongly flagged; tn ok
tp = fn = fp = tn = 0
wrong = []
for it in items:
    entailed = ask.semantic_judge(it['claim'], it['chunk_text'])  # True=supported
    pred = 'supported' if entailed else 'unsupported'
    gold = it['label']
    if gold == 'unsupported':
        if pred == 'unsupported':
            tp += 1
        else:
            fn += 1; wrong.append((it['id'], 'MISSED unsupported'))
    else:
        if pred == 'supported':
            tn += 1
        else:
            fp += 1; wrong.append((it['id'], 'over-flagged supported'))

n = len(items)
prec = tp / (tp + fp) if (tp + fp) else 0.0
rec = tp / (tp + fn) if (tp + fn) else 0.0
print(f"items: {n} | catch unsupported: recall={rec:.0%} ({tp}/{tp+fn}) precision={prec:.0%}")
print(f"false-positive (supported over-flagged): {fp}/{tn+fp}")
print(f"overall accuracy: {(tp+tn)}/{n} = {(tp+tn)/n:.0%}")
for wid, why in wrong:
    print(f"  x {wid}: {why}")
