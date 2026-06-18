#!/usr/bin/env python3
"""
libclang_callgraph.py — High-confidence, type-resolved call edge pass for ArduPilot KB.

Strategy:
  1. Parse each .cpp under libraries/, ArduCopter/, ArduPlane/, Rover/, ArduSub/ with libclang
     using best-effort include flags (no compile_commands needed).
  2. Walk CALL_EXPR nodes via a stack-based traversal; for each with a non-null .referenced
     cursor inside the corpus:
     - The innermost enclosing FUNCTION_DECL / CXX_METHOD on the stack = the caller.
     - The referenced.canonical = the callee.
     - Map both to symbol ids in kb.duckdb (qualified_name match first, file+line fallback).
  3. Insert edges (src, dst, kind='calls', confidence='high', callee_name='__libclang__') as an
     idempotency marker. On re-run: delete old libclang rows first, then re-insert.
  4. Never delete tree-sitter edges. Dedupe against existing edges.

Schema: edges(id INTEGER PK, src_symbol_id INTEGER, dst_symbol_id INTEGER,
              kind VARCHAR, confidence VARCHAR, callee_name VARCHAR)
"""

import sys
import time
import traceback
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
KB            = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CORPUS        = KB / 'corpus' / 'ardupilot'
DB_PATH       = KB / 'structured' / 'kb.duckdb'
LOG_PATH      = KB / 'structured' / 'libclang_callgraph.log'
SCRIPT_MARKER = '__libclang__'   # stored in callee_name to mark rows added by this script
CORPUS_STR    = str(CORPUS)

sys.path.insert(0, str(KB))
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import duckdb
import clang.cindex as cx

# ── libclang setup ─────────────────────────────────────────────────────────────
IDX = cx.Index.create()
# PARSE_INCOMPLETE: best-effort even with missing headers; do NOT skip function bodies
PARSE_OPTS = cx.TranslationUnit.PARSE_INCOMPLETE

CLANG_ARGS = [
    '-x', 'c++',
    '-std=c++11',
    '-ferror-limit=0',
    '-w',                           # suppress warnings
    f'-I{CORPUS}',
    f'-I{CORPUS}/libraries',
    f'-I{CORPUS}/libraries/AP_HAL',
    f'-I{CORPUS}/modules/mavlink',
    f'-I{CORPUS}/libraries/AP_Common',
    f'-I{CORPUS}/libraries/AP_Math',
    f'-I{CORPUS}/libraries/AP_Param',
]

FUNC_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.FUNCTION_TEMPLATE,
})

# ── target directories ─────────────────────────────────────────────────────────
TARGET_DIRS = [
    CORPUS / 'libraries',
    CORPUS / 'ArduCopter',
    CORPUS / 'ArduPlane',
    CORPUS / 'Rover',
    CORPUS / 'ArduSub',
]


def collect_cpp_files():
    files = []
    for d in TARGET_DIRS:
        if d.exists():
            for p in sorted(d.rglob('*.cpp')):
                files.append(p)
    return files


# ── symbol lookup structures ───────────────────────────────────────────────────
def build_lookup(con):
    """
    Build two lookup dicts from the symbols table:
      qname_to_ids: qualified_name -> list of symbol ids  (may be ambiguous)
      file_line_to_id: (relative_file, start_line) -> symbol id
    Only consider method/function kinds (those can be callers/callees of call edges).
    """
    qname_to_ids = {}
    file_line_to_id = {}

    rows = con.execute(
        "SELECT id, qualified_name, file, start_line FROM symbols "
        "WHERE kind IN ('method', 'function')"
    ).fetchall()

    for sym_id, qname, file_rel, start_line in rows:
        if qname:
            qname_to_ids.setdefault(qname, []).append(sym_id)
        if file_rel and start_line is not None:
            file_line_to_id[(file_rel, start_line)] = sym_id

    return qname_to_ids, file_line_to_id


def cursor_qualname(cur):
    """Compute fully-qualified name from a libclang cursor."""
    parts = []
    c = cur
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        sp = c.spelling
        if sp:
            parts.append(sp)
        c = c.semantic_parent
    return '::'.join(reversed(parts))


def make_file_rel(path_str):
    """Convert absolute path to corpus-relative path."""
    if path_str and path_str.startswith(CORPUS_STR):
        rel = path_str[len(CORPUS_STR):]
        return rel.lstrip('/')
    return None


def resolve_symbol(qname, file_rel, line, qname_to_ids, file_line_to_id):
    """
    Try to map a (qualname, file, line) to a single symbol id.
    Returns (symbol_id, method_str) or (None, reason_str).
    """
    # 1. Exact qualified name match
    if qname and qname in qname_to_ids:
        ids = qname_to_ids[qname]
        if len(ids) == 1:
            return ids[0], 'qname_unique'
        # Ambiguous — try to disambiguate by file+line
        if file_rel and line is not None:
            key = (file_rel, line)
            if key in file_line_to_id and file_line_to_id[key] in ids:
                return file_line_to_id[key], 'qname_disambig'
        return None, 'ambiguous'

    # 2. File + line fallback
    if file_rel and line is not None:
        key = (file_rel, line)
        if key in file_line_to_id:
            return file_line_to_id[key], 'file_line'

    return None, 'miss'


def walk_calls(tu):
    """
    Stack-based walk of the translation unit AST.
    Collect (caller_qname, caller_frel, caller_line,
             callee_qname, callee_frel, callee_line)
    for every CALL_EXPR whose referenced cursor is located inside the corpus.
    """
    results = []

    def _walk(node, func_stack):
        is_func_def = (node.kind in FUNC_KINDS and node.is_definition())
        if is_func_def:
            func_stack = func_stack + [node]

        if node.kind == cx.CursorKind.CALL_EXPR and func_stack:
            ref = node.referenced
            if ref is not None and ref.location.file is not None:
                callee_path = ref.location.file.name
                if callee_path.startswith(CORPUS_STR):
                    caller     = func_stack[-1]
                    caller_loc = caller.location

                    caller_qname = cursor_qualname(caller)
                    caller_frel  = make_file_rel(
                        caller_loc.file.name if caller_loc.file else None
                    )
                    caller_line  = caller_loc.line if caller_loc.file else None

                    canonical    = ref.canonical
                    callee_qname = cursor_qualname(canonical)
                    callee_frel  = make_file_rel(
                        canonical.location.file.name
                        if canonical.location.file else None
                    )
                    callee_line  = (
                        canonical.location.line
                        if canonical.location.file else None
                    )

                    results.append((
                        caller_qname, caller_frel, caller_line,
                        callee_qname, callee_frel, callee_line,
                    ))

        for child in node.get_children():
            _walk(child, func_stack)

    _walk(tu.cursor, [])
    return results


def main():
    t0 = time.time()
    log_lines = []

    def log(msg):
        print(msg, flush=True)
        log_lines.append(msg)

    log(f"libclang_callgraph.py — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Corpus: {CORPUS}")
    log(f"DB:     {DB_PATH}")

    # ── open DB ───────────────────────────────────────────────────────────────
    con = duckdb.connect(str(DB_PATH))

    # ── idempotency: remove previously-inserted libclang edges ───────────────
    prev_count = con.execute(
        f"SELECT count(*) FROM edges WHERE callee_name='{SCRIPT_MARKER}'"
    ).fetchone()[0]
    if prev_count > 0:
        log(f"Removing {prev_count} previous libclang edges (idempotency re-run).")
        con.execute(f"DELETE FROM edges WHERE callee_name='{SCRIPT_MARKER}'")

    edges_before = con.execute('SELECT count(*) FROM edges').fetchone()[0]
    log(f"Edges before pass: {edges_before}")

    # ── load symbol lookup ────────────────────────────────────────────────────
    log("Building symbol lookup tables...")
    qname_to_ids, file_line_to_id = build_lookup(con)
    log(f"  qname entries:     {len(qname_to_ids)}")
    log(f"  file+line entries: {len(file_line_to_id)}")

    # ── load existing call edges for dedup ────────────────────────────────────
    existing_calls = set()
    for row in con.execute(
        "SELECT src_symbol_id, dst_symbol_id FROM edges WHERE kind='calls'"
    ).fetchall():
        existing_calls.add((row[0], row[1]))
    log(f"Existing call edges for dedup: {len(existing_calls)}")

    # ── collect .cpp files ────────────────────────────────────────────────────
    cpp_files = collect_cpp_files()
    log(f"Total .cpp files to parse: {len(cpp_files)}")

    # ── stats ─────────────────────────────────────────────────────────────────
    stats = {
        'files_parsed':      0,
        'files_failed':      0,
        'call_exprs_corpus': 0,
        'both_resolved':     0,
        'new_edges':         0,
        'deduped':           0,
        'ambiguous_caller':  0,
        'ambiguous_callee':  0,
        'miss_caller':       0,
        'miss_callee':       0,
    }

    new_edges_batch = []   # (src_id, dst_id) accumulated before flush
    sample_edges    = []   # (caller_qname, callee_qname) for final report

    BATCH_SIZE = 500

    # Get current max id so we can assign sequential ids for new rows
    # (the schema has id NOT NULL PRIMARY KEY — not SEQUENCE/AUTOINCREMENT)
    max_id_row = con.execute('SELECT COALESCE(MAX(id), 0) FROM edges').fetchone()
    next_id_box = [max_id_row[0] + 1]   # mutable via list

    def flush_batch():
        if not new_edges_batch:
            return
        rows = []
        for src, dst in new_edges_batch:
            rows.append((next_id_box[0], src, dst, 'calls', 'high', SCRIPT_MARKER))
            next_id_box[0] += 1
        con.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?)",
            rows
        )
        new_edges_batch.clear()

    # ── main parse loop ───────────────────────────────────────────────────────
    for i, cpp_path in enumerate(cpp_files):
        if i > 0 and i % 100 == 0:
            elapsed = time.time() - t0
            rate    = i / elapsed if elapsed > 0 else 1
            eta     = (len(cpp_files) - i) / rate
            log(f"  [{i}/{len(cpp_files)}] new_edges={stats['new_edges']} "
                f"elapsed={elapsed:.0f}s ETA={eta:.0f}s")

        try:
            tu = IDX.parse(str(cpp_path), args=CLANG_ARGS, options=PARSE_OPTS)
        except Exception as exc:
            stats['files_failed'] += 1
            log(f"  PARSE FAIL {cpp_path.name}: {exc}")
            continue

        if tu is None:
            stats['files_failed'] += 1
            continue

        stats['files_parsed'] += 1

        try:
            calls = walk_calls(tu)
        except Exception as exc:
            log(f"  WALK FAIL {cpp_path.name}: {exc}")
            traceback.print_exc()
            continue

        stats['call_exprs_corpus'] += len(calls)

        for (caller_qname, caller_frel, caller_line,
             callee_qname, callee_frel, callee_line) in calls:

            # Resolve caller
            caller_id, caller_method = resolve_symbol(
                caller_qname, caller_frel, caller_line,
                qname_to_ids, file_line_to_id
            )
            if caller_id is None:
                if caller_method == 'ambiguous':
                    stats['ambiguous_caller'] += 1
                else:
                    stats['miss_caller'] += 1
                continue

            # Resolve callee
            callee_id, callee_method = resolve_symbol(
                callee_qname, callee_frel, callee_line,
                qname_to_ids, file_line_to_id
            )
            if callee_id is None:
                if callee_method == 'ambiguous':
                    stats['ambiguous_callee'] += 1
                else:
                    stats['miss_callee'] += 1
                continue

            stats['both_resolved'] += 1

            pair = (caller_id, callee_id)
            if pair in existing_calls:
                stats['deduped'] += 1
                continue

            existing_calls.add(pair)   # prevent within-run duplicates
            new_edges_batch.append(pair)
            stats['new_edges'] += 1

            if len(sample_edges) < 5:
                sample_edges.append((caller_qname, callee_qname))

            if len(new_edges_batch) >= BATCH_SIZE:
                flush_batch()

    flush_batch()   # final batch

    edges_after = con.execute('SELECT count(*) FROM edges').fetchone()[0]
    elapsed_total = time.time() - t0

    # ── report ────────────────────────────────────────────────────────────────
    total_corpus = stats['call_exprs_corpus']
    res_rate = (
        100.0 * stats['both_resolved'] / total_corpus
        if total_corpus > 0 else 0.0
    )

    log("")
    log("=" * 60)
    log("RESULTS")
    log("=" * 60)
    log(f"Runtime:              {elapsed_total:.1f}s ({elapsed_total/60:.1f} min)")
    log(f"Files parsed:         {stats['files_parsed']}")
    log(f"Files failed:         {stats['files_failed']}")
    log(f"CALL_EXPRs in corpus: {total_corpus}")
    log(f"Both sides resolved:  {stats['both_resolved']}")
    log(f"Resolution rate:      {res_rate:.1f}%")
    log(f"Ambiguous callers:    {stats['ambiguous_caller']}")
    log(f"Ambiguous callees:    {stats['ambiguous_callee']}")
    log(f"Miss callers:         {stats['miss_caller']}")
    log(f"Miss callees:         {stats['miss_callee']}")
    log(f"Deduped (existing):   {stats['deduped']}")
    log(f"NEW edges added:      {stats['new_edges']}")
    log(f"Edges before pass:    {edges_before}")
    log(f"Edges after pass:     {edges_after}")
    log(f"Net new edges:        {edges_after - edges_before}")
    log("")
    log("Sample of new edges (caller -> callee):")
    for caller_qn, callee_qn in sample_edges:
        log(f"  {caller_qn}  ->  {callee_qn}")
    log("")

    # ── domain breakdown ──────────────────────────────────────────────────────
    log("Domain breakdown of new high-confidence edges:")
    domain_rows = con.execute(f"""
        SELECT s1.domain, s2.domain, count(*) AS cnt
        FROM edges e
        JOIN symbols s1 ON e.src_symbol_id = s1.id
        JOIN symbols s2 ON e.dst_symbol_id = s2.id
        WHERE e.callee_name = '{SCRIPT_MARKER}'
        GROUP BY s1.domain, s2.domain
        ORDER BY cnt DESC
        LIMIT 25
    """).fetchall()
    for src_dom, dst_dom, cnt in domain_rows:
        log(f"  {src_dom:30s} -> {dst_dom:30s}: {cnt}")

    log("")
    log("Highest-degree caller symbols (new edges only):")
    top_rows = con.execute(f"""
        SELECT s.qualified_name, s.domain, count(*) AS out_deg
        FROM edges e
        JOIN symbols s ON e.src_symbol_id = s.id
        WHERE e.callee_name = '{SCRIPT_MARKER}'
        GROUP BY s.qualified_name, s.domain
        ORDER BY out_deg DESC
        LIMIT 10
    """).fetchall()
    for qn, dom, deg in top_rows:
        log(f"  [{deg:4d} edges] {qn}  ({dom})")

    # Write log
    with open(LOG_PATH, 'w') as f:
        f.write('\n'.join(log_lines))
    log(f"\nLog written to {LOG_PATH}")

    con.close()
    log("Done.")


if __name__ == '__main__':
    main()
