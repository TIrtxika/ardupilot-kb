#!/usr/bin/env python3
"""
Tune & measure an NLI cross-encoder semantic judge (#3 approach C) on the adversarial set.
Sweeps the entailment threshold and also tries the argmax==entailment criterion, reporting
recall(unsupported)/precision/false-positives so we can pick the criterion before wiring into ask.py.
Compare against the LLM judge baseline: recall 70%, precision 100%, FP 0/10.
"""
import sys, json, time
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import numpy as np
from sentence_transformers import CrossEncoder

MODEL = 'cross-encoder/nli-deberta-v3-base'   # labels: 0=contradiction, 1=entailment, 2=neutral
items = [json.loads(l) for l in open(KB / 'eval' / 'semantic_gold.jsonl')]

t0 = time.time()
model = CrossEncoder(MODEL)
pairs = [(it['chunk_text'], it['claim']) for it in items]
logits = np.asarray(model.predict(pairs))
probs = np.exp(logits) / np.exp(logits).sum(axis=1, keepdims=True)
dt = time.time() - t0
print(f"model={MODEL} | scored {len(items)} pairs in {dt:.1f}s ({1000*dt/len(items):.0f} ms/pair)")
labels = [it['label'] for it in items]


def score(supported_pred):
    tp = fn = fp = tn = 0
    for sup, gold in zip(supported_pred, labels):
        if gold == 'unsupported':
            tp += (not sup); fn += sup
        else:
            tn += sup; fp += (not sup)
    rec = tp / (tp + fn) if tp + fn else 0
    prec = tp / (tp + fp) if tp + fp else 0
    return rec, prec, fp, (tp + tn)


print("\ncriterion              recall  precision  false_pos  overall")
# argmax == entailment
pred = [int(np.argmax(p)) == 1 for p in probs]
r, pr, fp, ok = score(pred)
print(f"argmax==entailment     {r:5.0%}   {pr:6.0%}     {fp}/10      {ok}/20")
# entailment-prob thresholds
for T in (0.3, 0.4, 0.5, 0.6, 0.7):
    pred = [p[1] >= T for p in probs]
    r, pr, fp, ok = score(pred)
    print(f"entail_prob >= {T:.1f}      {r:5.0%}   {pr:6.0%}     {fp}/10      {ok}/20")
# not-contradiction criterion
for T in (0.4, 0.5, 0.6):
    pred = [p[0] < T for p in probs]
    r, pr, fp, ok = score(pred)
    print(f"contra_prob < {T:.1f}       {r:5.0%}   {pr:6.0%}     {fp}/10      {ok}/20")
