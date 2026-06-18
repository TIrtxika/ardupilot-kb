#!/usr/bin/env python3
"""
End-to-end SERVE eval: run every gold question through the full ask.py pipeline
(route -> retrieve -> ground -> generate -> deterministic audit) and grade the
AUDITED answer text against the gold value. Measures real system answer quality,
not just retrieval. Writes incremental progress + a final report.
"""
import sys, json, re, time
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'serve'))
sys.path.insert(0, str(KB / 'scripts'))
import ask  # reuses answer(), audit(), con

REFUSAL = 'not supported by the indexed corpus'
PROG = KB / 'eval' / 'serve_eval_progress.json'
OUT = KB / 'eval' / 'eval_report_serve.json'


def gold_tokens(item):
    """Run gold_support SQL -> list of expected answer tokens (non-null values)."""
    gs = item.get('gold_support', '')
    try:
        rows = ask.con.execute(gs).fetchall()
    except Exception as e:
        return None, f"sql_error:{e}"
    if not rows:
        return None, "no_rows"
    toks = []
    for v in rows[0]:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() != 'none':
            toks.append(s)
    return toks, None


def graded(item, clean):
    """Is the audited answer correct? token-containment of gold values."""
    toks, err = gold_tokens(item)
    t = item.get('type')
    if t == 'relationship':
        return None  # set-valued; not graded on generated text
    if err or toks is None:
        return None
    cl = clean.lower()
    if t == 'localization':
        # expect the file path (or basename) cited
        f = toks[0].lower()
        return (f in cl) or (f.split('/')[-1] in cl)
    # param_fact / concept: every non-null value token present
    if not toks:
        return None
    ok = True
    for tk in toks:
        tl = tk.lower()
        if tl in cl:
            continue
        # numeric tolerance
        try:
            if any(abs(float(tk) - float(x)) < 1e-6 for x in re.findall(r'-?\d+(?:\.\d+)?', cl)):
                continue
        except ValueError:
            pass
        ok = False
        break
    return ok


def main():
    gold = [json.loads(l) for l in open(KB / 'eval' / 'gold.jsonl')]
    results, t0 = [], time.time()
    agg = {'n': 0, 'graded': 0, 'correct': 0, 'refused': 0, 'struck_any': 0}
    by_type = {}
    by_mode = {}
    for i, item in enumerate(gold):
        q = item['question']
        r = ask.respond(q)
        clean = r['final']
        refused = REFUSAL in clean.lower()
        g = graded(item, clean)
        bm = by_mode.setdefault(r['mode'], [0, 0])
        if g is not None:
            bm[1] += 1; bm[0] += 1 if g else 0
        rec = {'id': item['id'], 'type': item.get('type'), 'domain': item.get('domain'),
               'mode': r['mode'], 'n_facts': len(r['facts']), 'struck': r['struck'],
               'refused': refused, 'correct': g}
        struck = r['struck']
        results.append(rec)
        agg['n'] += 1
        agg['refused'] += 1 if refused else 0
        agg['struck_any'] += 1 if struck else 0
        if g is not None:
            agg['graded'] += 1
            agg['correct'] += 1 if g else 0
            bt = by_type.setdefault(item.get('type'), [0, 0])
            bt[1] += 1; bt[0] += 1 if g else 0
        PROG.write_text(json.dumps({'done': i + 1, 'total': len(gold),
                                    'elapsed_s': round(time.time() - t0, 1), 'agg': agg}))
        print(f"[{i+1}/{len(gold)}] {item['id']:34} {r['mode']:6} type={item.get('type'):11} "
              f"facts={rec['n_facts']} struck={struck} correct={g}", flush=True)

    report = {'agg': agg,
              'answer_accuracy': f"{agg['correct']}/{agg['graded']}" +
                                 (f" ({100*agg['correct']/agg['graded']:.1f}%)" if agg['graded'] else ""),
              'by_type': {k: f"{v[0]}/{v[1]}" for k, v in by_type.items()},
              'refusal_rate': f"{agg['refused']}/{agg['n']}",
              'auditor_struck_any': f"{agg['struck_any']}/{agg['n']}",
              'by_mode_accuracy': {k: f"{v[0]}/{v[1]}" for k, v in by_mode.items()},
              'elapsed_min': round((time.time() - t0) / 60, 1),
              'results': results}
    OUT.write_text(json.dumps(report, indent=2))
    print("\n=== SERVE EVAL DONE ===")
    print(f"answer accuracy: {report['answer_accuracy']}  by_type: {report['by_type']}")
    print(f"refusal: {report['refusal_rate']}  struck-any: {report['auditor_struck_any']}  "
          f"elapsed: {report['elapsed_min']} min")


if __name__ == '__main__':
    main()
