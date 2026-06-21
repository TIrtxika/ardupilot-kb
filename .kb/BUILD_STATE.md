# KB Build State

Snapshot of where the ArduPilot KB build stands. Use this to resume in a later session.

_Last updated: 2026-06-18_

**Published:** https://github.com/TIrtxika/ardupilot-kb (public, branch `main`, commit `0f548fe`+).
Runtime artifacts committed (`.kb/structured/kb.duckdb` + `.kb/index/lancedb/`) so a fresh clone
runs immediately — no re-index needed. corpus/venv/chunks/bm25 are gitignored (rebuild via USAGE.md).
Note: a 95.6 MB `.lance` file exceeds GitHub's recommended 50 MB; if a future re-index pushes any
file past 100 MB, switch `*.lance` to Git LFS. Setup/usage: see `USAGE.md`.

## Pinned source (hard rule #7)

| Repo | SHA |
|---|---|
| ardupilot | `20622a390035d439268cb40583b8fb62c033ed50` |
| ardupilot_wiki | `f8c078cb7cc3df01a0987dfba9d3b2b475055f7e` |
| modules/mavlink | `035ffa630d54de8e5fc0f4fafc32182287738819` |
| modules/DroneCAN/DSDL | `04e0e818b06c180eb1720fcdf16484d0f12895ee` |

## Local models (pinned in .claude/settings.json)

- Generation: `qwen3-30b` (Qwen3-30B-A3B MoE) — installed, NOT yet exercised
- Embeddings: `bge-m3`, 1024-dim — installed, verified, used to build the index
- Reranker: not deployed (Phase 2)
- Hardware: AMD Phoenix APU, ~512 MB VRAM, 30 GB RAM → CPU inference
- Ollama 0.23.2: no `ollama embed` CLI; embed via HTTP `POST /api/embed`

## Phase status

| Phase | Status |
|---|---|
| 0 — corpus clone + inventory | DONE |
| 1 — deterministic layer (symbols/edges/params/messages) | DONE |
| 0 — gold eval set | DONE |
| 2 — AST/section chunking | DONE — 40,058 chunks |
| 2 — BM25 lexical index | DONE |
| 2 — dense embedding (bge-m3) into LanceDB | DONE — 40,058/40,058, 0 fails |
| 2 — vector + hybrid eval | **NOT RUN** — next step |
| 3 — per-domain indices + router | DONE — KEEP (vector@5 47.8%→84.8%) |
| 3 — held-out validation (overfit check) | DONE — router generalizes, NOT overfit |
| 4 — LoRA | gated OFF — retrieval strong, deterministic layer 100%, no measured gap |

## Held-out validation (20 Q, never tuned on) — eval_report_phase3_heldout.json

Routing 20/20 · exact-fact 15/15 · localization 5/5 · grounding 100% (all = tunable set).
Vector@5 64.7% (vs 84.8% tunable). The drop is NOT overfit — it is two index bugs (below).

## Retrieval-bug investigation (2026-06-17, systematic root-cause)

Diagnostics: `.kb/scripts/diag_retrieval.py`, `diag_hybrid.py`. Evidence overturned the
held-out subagent's bug list:

1. **"Coverage bug" — REFUTED.** All gold chunks exist in the index (28/28 param+concept
   `exists=True`); messages all in `domain_comms`, MAG_EF_LIM in `domain_state_estimation`.
   The misses are RANKING, not absence. (Subagent confused chunk_id scheme: real ids are
   `param_NNNN` / `msg_NAME`, both present.)
2. **BM25/RRF fusion — would REGRESS, do NOT apply.** Tested: dense 22/28 @5, RRF 21/28,
   bm25 11/28. For message chunks BM25 returns None (short structured text drowned by code
   mentions of "heartbeat"/"attitude"/"id"), so fusion drags perfect dense ranks down
   (heartbeat 1→23, battery-status 2→15). Concept would drop 7/9 → 3/9.
3. **Infra-removal — net zero** (22/28 → 22/28): rescues some, breaks infra-domain gold items.
4. **Real driver of residual misses:** (a) `infra_crosscutting` is a 21,473-chunk catch-all
   (53% of corpus) always appended, diluting; (b) bare param names (no group prefix) create
   competing duplicates; (c) near-duplicate short names (ATTITUDE vs ATTITUDE_TARGET) that
   dense similarity cannot separate. None fixable by the obvious routing/fusion changes.
5. **Key context:** every residual vector miss is a param_fact/concept question — types the
   DETERMINISTIC layer answers at 100%. User-facing correctness is unaffected; the held-out
   "−20pp vector@5" is largely a measurement artifact of grading deterministic Qs by vector rank.
   The genuinely retrieval-dependent type (localization) is healthy: 17/18 tunable, 4/5 held-out.

### DONE: param group-prefix data fix (2026-06-17) — KEEP, verified

Root cause (confirmed in source): extractor took `// @Param:` leaf names; `params.group` empty.
Real names need the `@Group:`/`@Path:`/`GOBJECT*` recursive prefix (ArduPilot param_parse.py method).
Fix (`.kb/scripts/fix_param_groups.py`): added `full_name` col (= group+leaf), populated `group`,
kept `name` (leaf) verbatim. 2846 prefixed · 402 top-level · 158 unresolved (AP_SUBGROUPINFO
nesting, conservative leaf fallback) · 720 indexed-array params take first prefix (canonical).
Verified: ANGLE_MAX→ATC_ANGLE_MAX, HGT_DELAY→EK3_/EK2_HGT_DELAY.
Cascade: regenerated param_chunks.jsonl (full_name in text), re-embedded 3406 chunks (240s),
rebuilt per-domain tables + BM25, global still 40,058 rows. Gold grew 53→61 (+8 full-name Qs).
Eval (61-set, verified from eval_report_phase3_reembed.json):
  exact-fact 43/43 (100%, incl 8 new) · localization 18/18 · vector@5 90.7% (param_fact 25/27)
  · NO vector regression on originals · the 8 full-name items 8/8 vector + 8/8 exact.
KNOWN REGRESSION (logged, low-stakes): BM25@5 dropped (~15%→7%) — the `(leaf X)` text format +
naive whitespace tokenization splits `ins_tcal1_tmax` so leaf token no longer matches. BM25 is a
SECONDARY signal NOT in the served path (fusion was shown to hurt concept, see above), so impact
is cosmetic. Fix later via BM25 tokenizer (split on `_`/punctuation) as its own measured pass.

### Remaining genuine (non-refuted) fixes, eval-gated
- **CPU reranker** (cross-encoder over dense top-N): the only tool that addresses mis-ranked-
   but-present items. Slow on CPU; mostly helps deterministic-answered Qs. Experiment, measure.
- **param group-prefix data fix** (params.group empty): real correctness defect — names should
   be qualified (ATC_ANG_MAX not ANG_MAX). Deterministic-layer fix; re-cascades to chunks/gold.
- **.h vs .cpp localization nudge:** 1 item (loc-ac-attitudecontrol .h at @10). Marginal.
- Phase-1 symbol-grapher gap: missing class bodies (AP_Scheduler, AP_GPS, AP_AHRS, ...).

## Serve path (.kb/serve/ask.py) — BUILT + tested 2026-06-17

End-to-end local pipeline works: route (phase3_router) -> deterministic facts (DuckDB) +
routed semantic retrieval (bge-m3/LanceDB) -> grounded prompt -> generate -> cite. Fully offline.

GENERATION MODEL CHANGED: qwen3-30b does NOT fit (14 GB model vs ~12 GB free RAM -> timed out
>300s, thrashes). Switched KB_GEN_MODEL -> `llama3.1:8b` (4.9 GB, 24 tok/s on CPU, works).
qwen3-30b remains the preferred model IF >14 GB RAM is freed.

Tests:
- Happy path: "default/range/units of EK3_HGT_DELAY" -> deterministic fact default=60 range=0..250
  units=ms [AP_NavEKF3.cpp:262]; llama3.1:8b answered correctly with per-claim file:line citations.
  (Router over-fired to all 13 domains — didn't recognise EK3_ param prefix — but deterministic
  layer made it correct anyway. Follow-up: add param-prefix->domain rule to router.)
- Refusal path: out-of-corpus question -> 0 deterministic facts -> llama3.1:8b said "Not supported
  by the indexed corpus" BUT then hedged and INFERRED an unsupported answer from unrelated RST.

DONE: deterministic retrieval-auditor gate in ask.py (`audit()`), enforces rule #4 with NO LLM:
  1. terminal refusal — once "Not supported by the indexed corpus" appears, drop everything after;
  2. speculation strike — sentences with infer/assume/likely/"we can"/... are removed;
  3. citation validity — any [path:line] must be from the provided FACTS/CONTEXT set;
  4. numeric grounding — every standalone number asserted must literally appear in a source cited
     IN THAT sentence (catches fabricated defaults/ranges/IDs). NUM_RE excludes digits inside
     identifiers (EK3) and inside [..] citations.
Tested: 3 unit cases (happy path 0-struck / leak->refusal / fabricated 999->struck) + 1 live
end-to-end (the WiFi-password leak now collapses to clean "Not supported by the indexed corpus").

## End-to-end SERVE eval (2026-06-17, eval_report_serve.json) — capstone

Ran all 61 gold through full ask.py (route->retrieve->ground->generate->audit), graded the
AUDITED answer. Runtime 19.9 min (llama3.1:8b CPU).
  answer accuracy 39/54 (72.2%) — param_fact 18/27 · localization 16/18 · concept 5/9
  refusals 8/61 · auditor struck content in 29/61.
WELL BELOW deterministic layer (100%). Bottleneck = generation+audit, NOT retrieval (~90%).
Confirmed root causes (deterministic_facts inspected, not guessed):
  BUG A: message facts emitted as "MESSAGE X: id=N" with NO [file:line] -> auditor strikes any
         restated msg id -> all msg-id concept questions fail.
  BUG B: param fact lookup matches bare leaf `name` too broadly -> "ANGLE_MAX" returns 3
         conflicting params (SAIL_/ATC_/QWIK_) -> 8B model garbles -> false refusal.
  ARCH: asking the 8B model to RESTATE authoritative facts is the weak link; per rule #1 the
        system should EMIT the deterministic fact directly (templated) for exact-fact types and
        use the LLM only for prose synthesis. That should push param_fact/concept toward 100%.
GOOD NEWS: anti-hallucination holds (fail-closed: refusals, not wrong answers). Auditor works;
it just lacks a direct-answer path so it over-refuses on exact facts.

FIX APPLIED + measured (ask.py): (1) message facts now carry [file:line]; (2) resolve_param
narrows by file/class/vehicle hint before collapsing equal-valued rows; (3) build_direct_answer
emits authoritative templated answers for param/message/symbol exact facts (LLM only when no
single fact resolves); (4) resolve_message: explicit message-id intent + quoted-field preference
+ scaffolding-word stoplist (fixed 'id'/'type' field-name collisions in BATTERY_STATUS).

SERVE EVAL AFTER FIX (eval_report_serve.json): answer accuracy 72.2% -> 96.3% -> **100% (54/54)**
  param_fact 27/27 · localization 18/18 · concept 9/9 · by_mode direct 53/53, llm 1/1
  refusals 2/61 · auditor struck 4/61 · runtime 19.9min -> 2.2min (direct path skips LLM).
Anti-hallucination preserved (fail-closed; auditor still gates the LLM path). System complete.

## DONE: symbol-grapher class-body fix (2026-06-17)

Root cause (probed with tree-sitter, two distinct bugs):
  CAUSE 1 (fixed): build_symbol_graph.py `_walk_node` did NOT descend into preprocessor
    conditional nodes (preproc_if/ifdef/else/elif). ArduPilot gates many whole classes behind
    `#if AP_*_ENABLED`, so those class_specifier nodes were never visited. Fix = add those node
    types to the descent list.
  CAUSE 2 (PARTIAL fix + architectural limit found): added _BREAKER_MACROS neutralizer to
    build_symbol_graph.py that blanks `WARN_IF_UNUSED` (99 files) to equal-length spaces before
    parsing. Marginal effect (+20 symbols) — because the deep cases (AP_AHRS, AP_Compass) are
    NOT just macro breakage: they have `#if AP_*_ENABLED ... #endif` interleaved INSIDE the class
    body gating members, which tree-sitter-cpp cannot parse (the whole class_specifier becomes an
    ERROR node). This is an architectural limit of parsing without a real C preprocessor. Stopped
    hacking macros (diminishing returns). REAL FIX = libclang + compile_commands.json (the same
    upgrade path noted for type-resolved call edges). AP_AHRS/AP_Compass/AP_RangeFinder remain
    missing as class bodies; deferred to the libclang upgrade.
IMPACT: symbols 21,003 -> 49,624 (+136%, 0 duplicates), edges 24,599 -> 60,875. Recovered
  AP_GPS/AP_Scheduler/AP_BattMonitor (+ presumably many gated classes corpus-wide).
GOTCHA (important): build_symbol_graph.py does `DB_PATH.unlink()` — it DELETES the whole DB and
  rebuilds only symbols/edges/build_info. A regraph MUST be followed by build_params_messages.py
  + fix_param_groups.py (this is the /ingest order). I re-ran both to restore params(3406)/
  messages(3642)+full_name. Made the grapher's table writes idempotent too.
VERIFIED: serve eval STILL 100% (54/54) after the 2.4x symbol expansion + param/message rebuild.

## DONE: libclang hybrid augmentation (2026-06-17) — Cause 2 resolved

Spike proved libclang's real preprocessor parses the `#if`-in-class-body files tree-sitter can't
(AP_AHRS: 262 members), WITHOUT compile_commands/build (just -I libraries, best-effort).
Right-sizing found: Compass/RangeFinder were FALSE alarms (real class names are `Compass`/
`RangeFinder`, already in graph); 325/1446 headers (22%) have tree-sitter ERROR nodes.
Built `.kb/scripts/libclang_augment.py` (HYBRID: tree-sitter primary; libclang only on the 325
error-headers; INSERT symbols not already present, dedup by (name,file,start_line)).
Dependency added: `pip install libclang` in .kb/venv (system libclang.so.22 also present).
RESULT: +13,895 symbols recovered (109 class, 754 struct, 9839 field, 1458 method, 1735 function).
  symbols 21,003 (orig) -> 49,644 (preproc fix) -> 63,539 (libclang). 0 duplicates.
  AP_AHRS now present: class AP_AHRS.h:43-1156 with 331 methods; serve path answers
  "which file defines AP_AHRS" and "where is AP_AHRS::get_location defined".
VERIFIED: serve eval STILL 100% (54/54), no regression.
NOTE: re-index order is now grapher -> libclang_augment -> build_params_messages -> fix_param_groups.
Call-graph type-resolution (61% low-conf edges) NOT done — needs full compile_commands; separate.

## DONE: vehicle-mismatch guard in resolve_param (2026-06-17)
Surfaced by a real test query ("default RTL_ALT in ArduCopter"): the system returned the PLANE
param Q_RTL_ALT because it was the only leaf-name match. Two findings: (1) at this SHA copter's
RTL altitude param is RTL_ALT_M (meters), not the old RTL_ALT name people Google; (2) resolve_param
returned a wrong-vehicle param. Fix: when the query names a vehicle, keep only vehicle-agnostic +
matching-vehicle rows; if none match, return None (fall to retrieval/LLM) instead of a wrong
answer. Verified: RTL_ALT_M answered correctly; old RTL_ALT->None; serve eval still 100% (54/54).

## LLM-path tested with popular queries (2026-06-17)
Ran "how does throttle failsafe work" + "how does RTL work" through the llm path (route ->
retrieve -> ground -> llama3.1:8b -> audit). RTL: clean grounded summary with per-line
[file:line] citations. Throttle FS: ~1600-char grounded answer, 0 strikes (verified in-process).
FOUND+FIXED: auditor misread markdown list ordinals ("1.", "2.") as fabricated numeric claims
(_split_sentences orphaned them) -> now strips leading list ordinals/bullets per line.
TUNED: num_predict 400->800 (no more truncation) and SYS prompt now requires INLINE citations
(no trailing References section, one [file:line] per cite, prose not numbered list). Re-ran:
both queries now produce complete, fully inline-cited grounded answers; Q1 0 strikes; Q2 auditor
correctly struck 1 speculative claim ("likely due to missing terrain data"). Serve LLM path is
now solid for conceptual questions on a local 8B model.

## CASE STUDY: out-of-distribution "5-motor copter" query (2026-06-17) — anti-hallucination win

User asked "how to set up a copter with 5 motors". ArduCopter has NO 5-motor frame class
(FRAME_CLASS @Values: 1=Quad 2=Hexa 3=Octa 4=OctaQuad 5=Y6(6 motors) 7=Tri 8=Single 9=Coax
10=BiCopter 12=DodecaHexa 14=Deca 15=Scripting Matrix ... — none is 5 motors). What happened:
  - First run: a bare PARAM token (FRAME_CLASS) hijacked the DIRECT path -> unhelpful one-line
    param dump. FIX: added _CONCEPTUAL intent triage in respond() — how-to/why/configure/work
    questions skip direct and use LLM synthesis even if a param token appears (0/61 gold match
    the trigger, so no eval regression).
  - Added @Values to deterministic param facts AND to build_direct_answer output (FRAME_CLASS
    values now visible -> exposes that no 5-motor option exists).
  - On the LLM path the 8B model TRIED to hallucinate ("Y6 uses 5 motors" [false], "set
    FRAME_CLASS to 15 for 5 motors") — the deterministic auditor STRUCK all 5 such sentences
    (uncited numbers + speculation). No false claim reached the user (fail-closed). Downside:
    the audited answer was terse/fragmentary — conservative auditor + weak 8B model.
  - serve eval stayed 100% (54/54). Lesson: terse truth beats plausible fabrication.
Truth for the record: a symmetric 5-motor multicopter is unsupported; use Quad/Hexa/Y6 or
FRAME_CLASS=15 (Scripting Matrix) for an arbitrary Lua-defined motor mix.

## GCS setup-instruction capability + "empty answer" resolved (2026-06-18)
Confirmed the KB produces real step-by-step GROUND STATION setup instructions when the wiki
covers the topic: "RC radio calibration in Mission Planner" -> grounded numbered steps citing
common-radio-control-calibration.rst / planner2/radio-calibration.rst, 0 strikes. Where the wiki
lacks coverage (dual-RC trainer/buddy-box) it honestly says so. Boundary = the corpus.
RESOLVED the intermittent "empty ANSWER": NOT a system bug — my diagnostic `grep -v -iE
"...ensure|persist..."` filter was deleting the single-line answers (setup steps contain the word
"ensure"; I'd added it for the zoxide warning). audit() never returns empty (verified). Added a
defensive guard anyway: audit() now collapses whitespace-only kept -> "Not supported...".

## Improvements batch (2026-06-18) — #1,#2,#4,#5,#6 DONE, all eval-gated, pushed

- #1 ROUTER: added PARAM_PREFIX_PATTERNS (EK3_/ATC_/INS_/SERIAL\d*_/GPS\d*_/...) to phase3_router.
  `EK3_HGT_DELAY` now routes to [state_estimation, infra] instead of all-13 fallback. routing 62/62.
- #2 CONFIGURABLE GEN: ask.py reads KB_GEN_MODEL/KB_EMBED_MODEL from env + graceful fallback to
  llama3.1:8b if the configured model errors/OOMs (set KB_GEN_MODEL=qwen3-30b when >14 GB RAM free).
- #4 LIBCLANG CALL GRAPH: `.kb/scripts/libclang_callgraph.py` — best-effort libclang (no
  compile_commands), 1545 .cpp parsed, 90.7% CALL_EXPR resolved, +18,604 high-confidence call
  edges (deduped, idempotent via __libclang__ marker; tree-sitter edges untouched). High-conf call
  share 35.5% -> 54.7%; callers_of/callees_of far more complete. 8.6 min runtime. DB backed up to
  kb.duckdb.bak (gitignored). NOTE: relationship gold answers were built on old edges and are now
  conservative subsets — regenerate if exact relationship grading is added.
- #5 REBUILD: `.kb/rebuild.sh` runs the whole pipeline in order (incl. callgraph + repack + eval).
- #6 BM25 TOKENIZER: split on _/punct (build_bm25/phase3_build/phase3_eval) -> EK3_HGT_DELAY
  tokenizes to ek3 hgt delay. BM25@5 28.3% -> 43.6%. (BM25 still not in ask.py serve path.)
All verified: serve eval 55/55, vector@5 89.1%, routing 62/62, largest LanceDB fragment <28 MB.

## DONE: #3 semantic auditor (2026-06-18) — approach B (LLM judge), opt-in

Chose B (per-sentence LLM judge reusing llama3.1:8b) over C (NLI cross-encoder) for THIS box:
C needs torch (~2 GB) and the judge only runs on the already-slow LLM serve path, so B = no new
dependency. `ask.semantic_judge(claim, context)` asks the gen model SUPPORTED/UNSUPPORTED; fails
OPEN (keeps) on error. Wired into audit(semantic=True), gated by env KB_SEMANTIC_AUDIT=1
(default OFF — adds ~per-cited-sentence LLM call, +10-20s/conceptual query on CPU).
Adversarial test set: `.kb/eval/semantic_gold.jsonl` (20 items, 10 supported / 10 unsupported,
real chunks, plausible token-overlapping false claims). Runner: `.kb/scripts/semantic_eval.py`.
MEASURED: catch-unsupported recall 70% (7/10), precision 100%, false-positives 0/10, overall 85%.
INTEGRATION eval: serve_eval with KB_SEMANTIC_AUDIT=1 stays 55/55 (judge struck 3 genuinely
cross-chunk-blended sentences on the RTL conceptual query; final answer still 1042 chars).
Also fixed a lance `_distance` deprecation warning in retrieve().

## DONE: #3 approach C (NLI cross-encoder) — MEASURED, shipped as OPTION, B stays default

torch 2.12.1+cpu has a py3.14 wheel, so C is feasible. Built semantic_eval_nli.py (threshold sweep)
+ semantic_judge_nli() in ask.py, selectable via env KB_SEMANTIC_JUDGE=nli (default 'llm').
KEY MEASUREMENT (adversarial set, .kb/eval/semantic_nli*.log):
  - nli-deberta-v3-SMALL: entailment criteria recall 90% but precision 69% / FP 4/10 (over-strikes
    good claims — labels technical paraphrases non-entailment). WORSE than LLM.
  - nli-deberta-v3-BASE: entailment criteria recall 100% but FP 3/10. BUT criterion
    `contradiction_prob >= 0.4 -> strike` gives recall 70%, precision 100%, FP 0/10 — TIES the LLM
    judge exactly. semantic_judge_nli() uses this contra criterion.
DECISION: C does NOT improve quality (70%/0-FP tie). Its only edge is latency in a PERSISTENT
server (~100ms/pair after load) vs LLM (+10-20s); but CLI ask.py pays ~35s model-load per
invocation + 2 GB torch. So B (LLM judge) stays DEFAULT; C shipped as opt-in for servers.
Deps isolated in .kb/requirements-nli.txt (NOT in main requirements). Eval-gate prevented
shipping the tempting high-recall-but-3/4-FP entailment criterion.

## DONE: #3 LLM judge prompt sharpened (2026-06-18) — recall 70% -> 90%, still 0 FP

Diagnosed the 3 misses: subtle code-logic flips (sem-02 *100 vs "divides", sem-05 `< && !armed`
vs "exceeds ... as long as armed", sem-09 raise vs lower + unstated cause). Sharpened JUDGE_SYS
to explicitly check arithmetic operators (* vs /), comparison direction (< vs >), logical negation
(x vs !x) in CODE, and to strike unstated cause/condition claims. Result on adversarial set:
recall 90% (9/10), precision 100%, FP 0/10, overall 95% — dependency-free (prompt only).
Only sem-02 (`return ... * 100;` read as "divides by 100") still missed by the 8B model.
serve_eval with KB_SEMANTIC_AUDIT=1 stays 55/55. This makes the default LLM judge (B) strictly
better than the NLI option (C, 70%); C remains opt-in for persistent-server latency only.

## DONE: call-graph cheap middle-ground (2026-06-18) — no build, near-compile_commands

Chose the cheap path over a full waf SITL build (build was infeasible/fragile: missing modules/waf
submodule + empy/pexpect/future, and g++16/clang22 are too new for ArduPilot @20622a39). Instead
improved libclang_callgraph.py args: -std=gnu++11, -DCONFIG_HAL_BOARD=HAL_BOARD_SITL,
-DCONFIG_HAL_BOARD_SUBTYPE=HAL_BOARD_SUBTYPE_NONE, and -I every libraries/* dir + mavlink v2.0.
Effect: the correct SITL #if branches are now active -> CALL_EXPRs seen 258k -> 309k, net new
high-confidence call edges 18,604 -> 23,065 (+4,461), high-conf call share 54.7% -> 57.7%
(resolution rate ~89.8%, ~same but on a larger correct set). serve_eval 55/55.

## FINDING (2026-06-21, from hard-query test): retrieval-relevance gap
Hard query "dual-GPS blending via GCS" pulled an IRRELEVANT chunk (ros-apriltag-detection.rst with
EK2_* params) on a niche topic with no good match. The LLM parroted it; the auditor PASSED the
EK2_POSNE_M_NSE=0.1 etc. claims because they ARE in the cited chunk. ROOT: the auditor verifies
claim<->chunk grounding but NOT chunk<->question relevance. Correct answer would cite GPS_AUTO_SWITCH.
Other hard queries were fine: GPS_TYPE @Values (direct, perfect), GPS-glitch (good), attitude->motor
(mediocre, 8B blends layers — auditor trimmed 3). Call-graph demo works (real type-resolved callers).

## DONE: relevance-gate (2026-06-21) — fixes the Q4 grounded-but-wrong-topic class

Calibration proved bi-encoder DISTANCE can't separate good-far from bad-near (GOOD EKF chunks at
0.733 dist vs BAD dual-GPS at 0.674 — overlap). A CROSS-ENCODER reranker CAN: rerank scores
GOOD RTL +5.0 / EKF +6.75 / glitch +4.6 vs BAD dual-GPS +3.17. So #Q4 fixed with a reranker, not
a distance threshold. Added `_rerank_gate()` (cross-encoder/ms-marco-MiniLM-L-6-v2) in retrieve(),
opt-in via KB_RERANK=1, drops chunks below KB_RERANK_MIN (default 3.5), re-sorts by relevance.
ALSO added an ALWAYS-ON fail-closed guard in respond(): a conceptual question with 0 hits ->
"Not supported by the indexed corpus." (don't let the LLM answer from its own knowledge).
VERIFIED: dual-GPS now refuses (was apriltag hallucination); GOOD RTL/EKF still answer; serve_eval
with KB_RERANK=1 stays 55/55. Reranker reuses sentence-transformers (requirements-nli.txt); model
downloads on first use. Opt-in (CLI pays ~load each call; ideal for a persistent server).

## DONE: call-graph in serve path (2026-06-21) — uses #4 graph

Added resolve_callgraph() to ask.py (in build_direct_answer, deterministic path): "who/what/which
calls X" / "callers of X" -> callers_of; "what does X call" / "callees of X" -> callees_of. Returns
templated caller/callee qualified names with file:line, no LLM. Matches qualified (A::b) or bare
(LIKE %::name) symbols. Intent regexes _CG_CALLEES/_CG_CALLERS (windows widened to 60-90 chars for
long qualified names). Gold grew 62->65: 3 graded call-graph questions (type=param_fact so
serve_eval token-grades them; rel-callers-ahrs-lock-home, rel-callees-heli-rate-bf-to-motor,
rel-callers-gcs-send-ahrs2), all correct via mode=direct. serve_eval 55/55 -> 58/58.

## Candidate next improvements (prioritized)
- (system is feature-complete; remaining items are marginal — see below)

## Open follow-ups (logged, eval-gated)
- sem-02 (arithmetic */÷ flip) needs a stronger gen model (qwen3-30b) or code-exec check; marginal.
- True compile_commands (full SITL build) would add the generated headers (ap_config.h, mavlink
  generated dialects) for the last few %; deferred — fragile on g++16/clang22, marginal gain.
- compile_commands-grade call resolution (vs current best-effort 90.7%) if a SITL build is set up.
- Grow gold with localization Qs for now-recovered classes (AP_AHRS/AP_GPS/AP_Scheduler) to lock the win.
- Router over-fires to all 13 domains for `EK3_`-style param-prefix queries — add prefix->domain rule.
- BM25 tokenizer cleanup (split on _/punct) — BM25 is a secondary, non-served signal.
- qwen3-30b generation needs >14 GB free RAM; llama3.1:8b is the working fallback.
- Auditor limitation: deterministic checks catch fabricated numbers/citations/speculation, NOT
  semantically-wrong-but-token-present claims; an LLM judge or NLI pass could add semantic checks.

## Artifacts

- `.kb/structured/kb.duckdb` — symbols 21,003 · edges 24,599 · params 3,406 · messages 3,642 · build_info
- `.kb/eval/gold.jsonl` — 53 tunable · `.kb/eval/gold_heldout.jsonl` — 20 held-out (NEVER tune on held-out)
- `.kb/chunks/{cpp,rst,param,message}_chunks.jsonl` — 40,058 total (cpp 25,758 · rst 10,340 · param 3,406 · msg 554)
- `.kb/index/lancedb/` — dense index (40,058 × 1024-dim), table `chunks`
- `.kb/index/bm25/bm25.pkl` — BM25 (avgdl 80.1)
- `.kb/index/manifest.json` — index version 2.0 metadata
- `.kb/MANIFEST.json`, `.kb/inventory.json` — corpus provenance (in .kb/, not .kb/corpus/, due to read-only deny rule)
- Scripts: `.kb/scripts/phase2_chunker.py`, `phase2_indexer.py` (resumable via `--resume`), `phase2_eval.py`, `build_params_messages.py`

## Phase 2 baseline (FULL — this is the number Phase 3 must beat)

Measured 2026-06-17 on the complete 40,058-chunk index (`.kb/eval/eval_report_phase2.json`).

Deterministic layer: exact-fact 35/35, localization 18/18, grounding 100%, hallucination 0.

| Layer | @1 | @5 | @10 | graded |
|---|---|---|---|---|
| Vector (bge-m3) | 26.1% | 47.8% | 58.7% | 46 |
| BM25 | 10.9% | 28.3% | 39.1% | 46 |

Per-type vector@5: concept 1/9 (WEAK — message chunks drown in global index),
localization 12/18, param_fact 9/19, relationship N/A (answered by deterministic layer).

Note: eval reports vector and BM25 separately; a fused hybrid (RRF) score is NOT yet computed.

## NEXT STEP (resume here)

Phase 3 — per-domain indices + router. Reuse the existing embeddings (every chunk has a
`domain` tag) — partition by domain, do NOT re-embed. Build the domain-classifier router,
re-run eval routed-per-domain, and compare deltas vs the Phase 2 baseline above. The concept/
comms weakness is the primary thing routing should fix.

## Known follow-ups (eval-gated, do NOT do mid-index)

- symbol-grapher gap: missing class-body symbols for `AP_Scheduler`, `AP_GPS`, `AP_Compass`,
  `AP_AHRS`, `AP_BattMonitor`, `AP_RangeFinder`, `AC_Fence`, `AC_AutoTune` (only forward-decls).
- `params.group` column empty → param names lack group prefix (e.g. `ANG_MIN` not `ATC_ANG_MIN`).
- settings.json deny rule blocks writing manifests into `.kb/corpus/`; consider narrowing it.
- Reranker deferred; deploy a sentence-transformers CPU reranker only if eval justifies it.
