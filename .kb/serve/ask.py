#!/usr/bin/env python3
"""
Local KB serve path: route -> retrieve (deterministic + semantic) -> ground -> answer.
Fully offline: bge-m3 (embeddings) + llama3.1:8b (generation) via Ollama HTTP; LanceDB; DuckDB.

Per hard rule #1: when the deterministic layer confidently resolves an exact fact (param/
message/symbol), the answer is EMITTED DIRECTLY from that fact (authoritative, auto-cited).
The LLM is used ONLY to synthesize prose when no single exact fact answers the question, and
its output is gated by the deterministic retrieval-auditor.

Usage: python ask.py "your question"
"""
import os, sys, json, re, urllib.request
from pathlib import Path

KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'scripts'))
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import lancedb, duckdb
from phase3_router import classify_query

OLLAMA = 'http://localhost:11434'
EMBED_MODEL = os.environ.get('KB_EMBED_MODEL', 'bge-m3')
# Configurable generation model (e.g. set KB_GEN_MODEL=qwen3-30b when >14 GB RAM is free).
# Falls back to llama3.1:8b if the configured model errors/times out (see respond()).
GEN_MODEL = os.environ.get('KB_GEN_MODEL', 'llama3.1:8b')
GEN_FALLBACK = 'llama3.1:8b'
SHA = '20622a390035d439268cb40583b8fb62c033ed50'
VEHICLES = {'arduplane': 'plane', 'arducopter': 'copter', 'copter': 'copter',
            'plane': 'plane', 'rover': 'rover', 'ardusub': 'sub', 'sub': 'sub',
            'blimp': 'blimp', 'tracker': 'antennatracker', 'antennatracker': 'antennatracker'}

con = duckdb.connect(str(KB / 'structured' / 'kb.duckdb'), read_only=True)
db = lancedb.connect(str(KB / 'index' / 'lancedb'))


def _post(path, payload, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def embed(text):
    return _post('/api/embed', {'model': EMBED_MODEL, 'input': [text]})['embeddings'][0]


# ── Deterministic resolvers (single best fact, or None) ─────────────────────────
def _rng(lo, hi):
    return f"{lo}..{hi}" if lo is not None else "n/a"


def resolve_param(q):
    """Return one best params row, or None if absent/ambiguous."""
    ups = re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', q)
    hints = [h for h in re.findall(r'\b[A-Z][A-Za-z0-9_]{4,}\b', q)]  # file/class hints
    qlow = q.lower()
    veh = next((v for k, v in VEHICLES.items() if k in qlow), None)
    cols = "full_name,name,default_val,range_min,range_max,units,file,line,vehicle,values_map"
    for tok in ups:
        rows = con.execute(f"SELECT {cols} FROM params WHERE full_name=?", [tok]).fetchall()
        if not rows:
            rows = con.execute(f"SELECT {cols} FROM params WHERE name=?", [tok]).fetchall()
        if not rows:
            continue
        # narrow by file/class hint, then by vehicle, BEFORE collapsing equal-valued rows
        byfile = [r for r in rows if any(h.lower() in (r[6] or '').lower()
                                         for h in hints if h not in ups)]
        if byfile:
            rows = byfile
        if veh:
            # keep vehicle-agnostic (library) params + those matching the requested vehicle;
            # if the query names a vehicle that NONE of these params belong to, this token does
            # not answer the question — skip it rather than return a wrong-vehicle param.
            matching = [r for r in rows if r[8] in (None, '') or r[8] == veh]
            if not matching:
                continue
            rows = matching
        if len(rows) == 1:
            return rows[0]
        # equal-valued rows (e.g. EK2_/EK3_ share defaults) -> any is a correct answer
        if len({(r[2], r[3], r[4], r[5]) for r in rows}) == 1:
            return rows[0]
    return None


_MSG_STOP = {'id', 'type', 'units', 'field', 'message', 'msg', 'value', 'default',
             'range', 'name', 'mavlink', 'the', 'and'}


def resolve_message(q):
    """Return ('id', name, (msg_id,file,line)) or ('field', name, (fn,ft,units,file,line)) or None."""
    qlow = q.lower()
    # explicit message-id intent (do NOT let a field literally named 'id' hijack it)
    id_intent = bool(re.search(r'\b(message|msg)\s+id\b|\bid\s+(of|for|number)\b', qlow))
    quoted = set(re.findall(r"['\"]([a-z_][a-z0-9_]*)['\"]", q))
    for tok in re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', q):
        idrow = con.execute(
            "SELECT DISTINCT msg_id, file, line FROM messages WHERE name=? AND msg_id IS NOT NULL "
            "LIMIT 1", [tok]).fetchall()
        fields = con.execute(
            "SELECT field_name, field_type, units, file, line FROM messages "
            "WHERE name=? AND field_name IS NOT NULL", [tok]).fetchall()
        if not idrow and not fields:
            continue
        if id_intent and idrow:
            return ('id', tok, idrow[0])
        # prefer a quoted field name; else match a non-scaffolding field word
        fmatch = [f for f in fields if f[0] and f[0].lower() in quoted]
        if not fmatch:
            fmatch = [f for f in fields if f[0] and f[0].lower() not in _MSG_STOP
                      and re.search(r'\b' + re.escape(f[0].lower()) + r'\b', qlow)]
        if fmatch:
            return ('field', tok, fmatch[0])
        if idrow:
            return ('id', tok, idrow[0])
    return None


def resolve_symbol(q):
    """For 'which file defines X' style questions -> one symbol row, or None."""
    if not re.search(r'which file|where .*(defined|implemented|located)|defines|implements', q, re.I):
        return None
    for tok in re.findall(r'\b[A-Z][A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)?\b', q):
        rows = con.execute(
            "SELECT kind,qualified_name,file,start_line,end_line FROM symbols "
            "WHERE qualified_name=? OR name=? ORDER BY (end_line-start_line) DESC LIMIT 1",
            [tok, tok]).fetchall()
        if rows:
            return rows[0]
    return None


# "who calls X" -> callers; "what does X call" / "callees of X" -> callees. Answered deterministically
# from the libclang-enriched callers_of/callees_of views (no LLM).
_CG_CALLEES = re.compile(r'\bcallees?\s+of\b|\bdoes\b[^.?]{0,90}\bcalls?\b|\bcalled\s+by\b', re.I)
_CG_CALLERS = re.compile(r'\bcallers?\s+of\b|\b(who|what|which)\b[^.?]{0,60}\bcalls?\b|'
                         r'\bwhere\s+is\b[^.?]{0,60}\bcalled\b|\bis\s+called\s+by\b', re.I)
_CG_SYM = re.compile(r'\b\w+::\w+\b|\b[A-Z][A-Za-z0-9_]{2,}\b|\b[a-z][a-z0-9]*_[a-z0-9_]+\b')


def resolve_callgraph(q):
    """Templated caller/callee answer from the call graph, or None."""
    direction = 'callees' if _CG_CALLEES.search(q) else ('callers' if _CG_CALLERS.search(q) else None)
    if not direction:
        return None
    # qualified (A::b) tokens first, then CamelCase / snake_case
    cands = sorted(set(_CG_SYM.findall(q)), key=lambda t: (0 if '::' in t else 1, -len(t)))
    for tok in cands:
        if direction == 'callers':
            rows = con.execute(
                "SELECT DISTINCT caller_name, caller_file, caller_line FROM callers_of "
                "WHERE callee_name = ? OR callee_name LIKE ? ORDER BY caller_name LIMIT 10",
                [tok, '%::' + tok]).fetchall()
            if rows:
                items = "; ".join(f"{r[0]} [{r[1]}:{r[2]}]" for r in rows)
                more = " (+more)" if len(rows) == 10 else ""
                return f"Functions that call {tok}: {items}{more}"
        else:
            rows = con.execute(
                "SELECT DISTINCT callee_name FROM callees_of "
                "WHERE caller_name = ? OR caller_name LIKE ? ORDER BY callee_name LIMIT 15",
                [tok, '%::' + tok]).fetchall()
            if rows:
                items = ", ".join(r[0] for r in rows)
                more = " (+more)" if len(rows) == 15 else ""
                return f"{tok} calls: {items}{more}"
    return None


def build_direct_answer(q):
    """Authoritative templated answer from the deterministic layer, or None."""
    cg = resolve_callgraph(q)
    if cg:
        return cg
    p = resolve_param(q)
    if p:
        dflt = p[2] if p[2] is not None else "none (runtime/no compiled-in default)"
        vals = f" values={{{p[9]}}}" if len(p) > 9 and p[9] else ""
        return (f"{p[0]}: default={dflt}, range={_rng(p[3], p[4])}, units={p[5] or 'n/a'}.{vals} "
                f"[{p[6]}:{p[7]}]")
    m = resolve_message(q)
    if m:
        if m[0] == 'id':
            mid, f, l = m[2]
            cite = f"[{f}:{l}]" if l else f"[{f}]"
            return f"The MAVLink message {m[1]} has ID {mid}. {cite}"
        fn, ft, u, f, l = m[2]
        cite = f"[{f}:{l}]" if l else f"[{f}]"
        return f"In {m[1]}, field '{fn}' has type {ft}, units {u or 'n/a'}. {cite}"
    s = resolve_symbol(q)
    if s:
        return f"{s[0]} {s[1]} is defined in {s[2]}:{s[3]}-{s[4]}."
    return None


# ── Deterministic facts for the LLM context (citations included) ─────────────────
def deterministic_facts(q):
    facts = []
    for tok in set(re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', q)):
        for r in con.execute(
            "SELECT full_name,name,default_val,range_min,range_max,units,file,line,values_map "
            "FROM params WHERE full_name=? OR name=? LIMIT 4", [tok, tok]).fetchall():
            vals = f" values={{{r[8]}}}" if r[8] else ""
            facts.append(f"PARAM {r[0]}: default={r[2]} range={_rng(r[3], r[4])} "
                         f"units={r[5] or 'n/a'}{vals} [{r[6]}:{r[7]}]")
        for r in con.execute(
            "SELECT DISTINCT name,msg_id,file,line FROM messages WHERE name=? AND msg_id IS NOT NULL "
            "LIMIT 2", [tok]).fetchall():
            facts.append(f"MESSAGE {r[0]}: id={r[1]} [{r[2]}:{r[3]}]")
        for r in con.execute(
            "SELECT name,field_name,field_type,units,file,line FROM messages "
            "WHERE name=? AND field_name IS NOT NULL LIMIT 12", [tok]).fetchall():
            facts.append(f"  {r[0]}.{r[1]}: type={r[2]} units={r[3] or 'n/a'} [{r[4]}:{r[5]}]")
    for tok in set(re.findall(r'\b[A-Z][A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)?\b', q)):
        for r in con.execute(
            "SELECT kind,qualified_name,file,start_line,end_line FROM symbols "
            "WHERE qualified_name=? OR name=? ORDER BY (end_line-start_line) DESC LIMIT 2",
            [tok, tok]).fetchall():
            facts.append(f"SYMBOL {r[0]} {r[1]} [{r[2]}:{r[3]}-{r[4]}]")
    seen, out = set(), []
    for f in facts:
        if f not in seen:
            seen.add(f); out.append(f)
    return out[:20]


_RERANK = None


def _rerank_gate(q, hits):
    """Relevance-gate (#Q4 fix, KB_RERANK=1): cross-encoder re-scores query<->chunk relevance and
    DROPS chunks below KB_RERANK_MIN (default 3.5). Bi-encoder distance can't separate a good-but-
    far chunk from an off-topic-but-token-overlapping one; the cross-encoder can. If nothing clears
    the bar -> [] -> the LLM gets no context -> refuses (avoids grounded-but-wrong-topic answers)."""
    global _RERANK
    try:
        if _RERANK is None:
            from sentence_transformers import CrossEncoder
            _RERANK = CrossEncoder(os.environ.get('KB_RERANK_MODEL', 'cross-encoder/ms-marco-MiniLM-L-6-v2'))
        thr = float(os.environ.get('KB_RERANK_MIN', '3.5'))
        scores = _RERANK.predict([(q, h[4]) for h in hits])
        kept = [(h, float(s)) for h, s in zip(hits, scores) if float(s) >= thr]
        kept.sort(key=lambda x: -x[1])
        return [h for h, _ in kept]
    except Exception:
        return hits  # fail open: never worse than no gate


def retrieve(q, k=6):
    routed = classify_query(q)
    qv = embed(q)
    hits = []
    for dom in routed:
        try:
            t = db.open_table(f'domain_{dom}')
        except Exception:
            continue
        for r in t.search(qv).select(
                ['source_path', 'start_line', 'end_line', 'text', '_distance']).limit(k).to_list():
            hits.append((r['_distance'], r['source_path'], r['start_line'], r['end_line'], r['text']))
    hits.sort(key=lambda x: x[0])
    hits = hits[:k]
    if os.environ.get('KB_RERANK') == '1' and hits:
        hits = _rerank_gate(q, hits)
    return routed, hits


# ── Deterministic retrieval-auditor (rule #4: no LLM, fail closed) ───────────────
CITE_RE = re.compile(r'\[([^\]\[]*?:\d+(?:-\d+)?)\]')
NUM_RE = re.compile(r'(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])')
SPECULATION = ('infer', 'assume', 'presum', 'probably', 'likely', 'guess', 'i think',
               'we can', 'might be', 'may be', 'perhaps', 'does not directly answer',
               'it seems', 'appears to', 'could be')
REFUSAL = 'not supported by the indexed corpus'


def _split_sentences(text):
    # strip leading list ordinals/bullets per line (e.g. "1.", "2)", "- ", "* ") so they are
    # not later misread as fabricated numeric claims or orphaned into number-only fragments.
    lines = [re.sub(r'^\s*(?:\d+[.)]|[-*•])\s+', '', ln) for ln in text.splitlines()]
    raw = re.split(r'(?<=[.!?])\s+|\n+', "\n".join(lines))
    return [s.strip(' \t-*•').strip() for s in raw if s.strip(' \t-*•').strip()]


def audit(draft, facts, hits, semantic=False):
    fact_text = "\n".join(facts)
    fact_files = set(re.findall(r'\[([^\]]+?):\d+', fact_text))
    path_text = {}
    for _, sp, s, e, txt in hits:
        path_text.setdefault(sp, "")
        path_text[sp] += " " + txt
    valid_paths = set(path_text) | fact_files

    kept, log, refused = [], [], False
    for sent in _split_sentences(draft):
        low = sent.lower()
        if refused:
            log.append(("STRUCK", "after refusal", sent)); continue
        if REFUSAL in low:
            kept.append("Not supported by the indexed corpus."); refused = True; continue
        if any(m in low for m in SPECULATION):
            log.append(("STRUCK", "speculation", sent)); continue
        cited = CITE_RE.findall(sent)
        cited_paths = {c.rsplit(':', 1)[0] for c in cited}
        bad = [c for c in cited_paths if c not in valid_paths]
        if bad:
            log.append(("STRUCK", f"invalid citation {bad}", sent)); continue
        body = CITE_RE.sub('', sent)
        nums = NUM_RE.findall(body)
        if nums:
            if not cited_paths:
                log.append(("STRUCK", "numeric claim, no citation", sent)); continue
            src = fact_text + " " + " ".join(path_text.get(p, "") for p in cited_paths)
            ungrounded = [n for n in nums if n not in src]
            if ungrounded:
                log.append(("STRUCK", f"ungrounded number(s) {ungrounded}", sent)); continue
        # semantic pass (#3): for cited sentences, verify the cited source actually entails them
        if semantic and cited_paths:
            ctx = " ".join(path_text.get(p, "") for p in cited_paths)
            if not semantic_judge(sent, ctx):
                log.append(("STRUCK", "semantic: not entailed by cited source", sent)); continue
        kept.append(sent)
        if cited or nums:
            log.append(("KEPT", "grounded", sent))
    clean = " ".join(s for s in kept if s.strip()).strip()
    if not clean:
        clean = "Not supported by the indexed corpus."
    return clean, log


# ── Semantic judge (#3): LLM entailment check on top of the deterministic auditor ───────────────
# Catches claims that are token-present (pass numeric/citation checks) but NOT actually entailed by
# the cited source (wrong direction/cause/quantity). Runs only on the LLM serve path.
JUDGE_SYS = (
    "You are a strict fact-checker. Given CONTEXT (verbatim ArduPilot source/docs) and a CLAIM, "
    "decide if the CONTEXT directly states or implies the CLAIM. Answer with exactly one word: "
    "SUPPORTED or UNSUPPORTED.\n"
    "Answer UNSUPPORTED if the CLAIM changes, flips, adds, or conflates ANY fact versus the CONTEXT. "
    "When the CONTEXT is CODE, check carefully: arithmetic operators (* vs /, + vs -), comparison "
    "direction (< vs >, <= vs >=), and logical negation (x vs !x) — if the CLAIM uses the opposite "
    "operator, comparison, or negation, answer UNSUPPORTED. "
    "Also answer UNSUPPORTED if the CLAIM asserts a cause, effect, default, or condition that the "
    "CONTEXT does not actually state, even if it reuses the same identifiers or numbers.")


def semantic_judge_llm(claim, context_text):
    """LLM judge (default). True=supported. Fails OPEN on error. Adversarial set: rec 70%, 0 FP."""
    prompt = f"{JUDGE_SYS}\n\nCONTEXT:\n{context_text[:1500]}\n\nCLAIM: {claim}\n\nAnswer:"
    try:
        r = _post('/api/generate', {'model': GEN_MODEL, 'prompt': prompt, 'stream': False,
                                    'options': {'temperature': 0, 'num_predict': 4}}, timeout=120)
        return 'unsupported' not in r.get('response', '').strip().lower()
    except Exception:
        return True


_NLI = None


def semantic_judge_nli(claim, context_text):
    """NLI cross-encoder judge (KB_SEMANTIC_JUDGE=nli). Strike only on contradiction (prob>=0.4) ->
    rec 70%, 0 FP, same quality as LLM but ~100ms/pair after load (good for persistent servers;
    needs torch + ~35s one-time model load, so poor for one-shot CLI). Fails OPEN on error."""
    global _NLI
    try:
        if _NLI is None:
            from sentence_transformers import CrossEncoder
            _NLI = CrossEncoder(os.environ.get('KB_NLI_MODEL', 'cross-encoder/nli-deberta-v3-base'))
        import numpy as np
        logits = np.asarray(_NLI.predict([(context_text[:1500], claim)])[0])
        contra = float(np.exp(logits[0]) / np.exp(logits).sum())  # label 0 = contradiction
        return contra < 0.4
    except Exception:
        return True


def semantic_judge(claim, context_text):
    """True if the context supports the claim. Dispatches LLM (default) or NLI per KB_SEMANTIC_JUDGE."""
    if not context_text.strip():
        return True
    if os.environ.get('KB_SEMANTIC_JUDGE', 'llm').lower() == 'nli':
        return semantic_judge_nli(claim, context_text)
    return semantic_judge_llm(claim, context_text)


SYS = (
    "You are an ArduPilot code assistant. Answer ONLY using the FACTS and CONTEXT provided. "
    "FACTS come from a verified database and are authoritative. "
    "Put the [file:line] citation INLINE immediately after each claim it supports. "
    "Do NOT collect citations in a trailing 'References' section. Use one [file:line] tag per "
    "citation (never a range joined with 'and'). Write in prose sentences, not a numbered list. "
    "If the material does not contain the answer, reply exactly: "
    "'Not supported by the indexed corpus.' Do not use outside knowledge.")


# "how-to"/conceptual questions need LLM synthesis over retrieved docs, NOT a bare param fact —
# even when they happen to mention a PARAM token. Only exact-fact phrasings use the direct path.
_CONCEPTUAL = re.compile(
    r'\b(how|why|explain|describe|difference|set ?up|configure|configur|tune|tuning|'
    r'tutorial|guide|steps|walk|overview|work)\b', re.I)


def respond(q):
    facts = deterministic_facts(q)
    routed, hits = retrieve(q)
    direct = None if _CONCEPTUAL.search(q) else build_direct_answer(q)
    if direct:
        return {'q': q, 'routed': routed, 'facts': facts, 'hits': hits,
                'draft': direct, 'final': direct, 'mode': 'direct', 'struck': 0, 'log': []}
    # Fail-closed: a conceptual question with NO relevant chunks (e.g. relevance-gate dropped all)
    # must NOT be answered from the LLM's own knowledge — refuse rather than hallucinate.
    if not hits:
        msg = "Not supported by the indexed corpus."
        return {'q': q, 'routed': routed, 'facts': facts, 'hits': hits,
                'draft': msg, 'final': msg, 'mode': 'refused', 'struck': 0, 'log': []}
    ctx = "\n".join(f"[{sp}:{s}-{e}] {txt[:400]}" for _, sp, s, e, txt in hits)
    factblock = "\n".join(facts) if facts else "(none)"
    prompt = f"{SYS}\n\nFACTS:\n{factblock}\n\nCONTEXT:\n{ctx}\n\nQUESTION: {q}\n\nAnswer:"
    opts = {'prompt': prompt, 'stream': False, 'options': {'temperature': 0, 'num_predict': 800}}
    try:
        draft = _post('/api/generate', {'model': GEN_MODEL, **opts}).get('response', '').strip()
    except Exception as e:
        if GEN_MODEL == GEN_FALLBACK:
            raise
        # configured model unavailable / OOM / timeout -> fall back to the lightweight model
        draft = _post('/api/generate', {'model': GEN_FALLBACK, **opts}).get('response', '').strip()
    clean, log = audit(draft, facts, hits, semantic=os.environ.get('KB_SEMANTIC_AUDIT') == '1')
    return {'q': q, 'routed': routed, 'facts': facts, 'hits': hits, 'draft': draft,
            'final': clean, 'mode': 'llm', 'struck': sum(1 for x in log if x[0] == 'STRUCK'),
            'log': log}


if __name__ == '__main__':
    q = ' '.join(sys.argv[1:]) or "What is the default value, range and units of EK3_HGT_DELAY?"
    r = respond(q)
    print(f"Q: {q}\nmode: {r['mode']} | routed: {len(r['routed'])} domains")
    print(f"\n--- deterministic facts ({len(r['facts'])}) ---")
    for f in r['facts'][:8]: print(" ", f)
    if r['mode'] == 'llm':
        print(f"\n--- DRAFT (llama3.1:8b) ---\n{r['draft']}")
        for v, reason, sent in r['log']:
            if v == 'STRUCK': print(f"  ✗ {reason}: {sent[:90]}")
    print(f"\n--- ANSWER ({r['mode']}, grounded @ {SHA[:10]}) ---\n{r['final']}")
