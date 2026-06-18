#!/usr/bin/env python3
"""
Targeted libclang augmentation of the symbol graph.

tree-sitter loses classes/members in ~22% of headers where ArduPilot interleaves
`#if AP_*_ENABLED ... #endif` inside class bodies (the whole class_specifier becomes an
ERROR node, e.g. AP_AHRS). libclang runs a REAL preprocessor and parses these correctly.

Strategy (hybrid, no compile_commands / no build needed):
  1. Find header files where tree-sitter reports parse errors.
  2. Parse each with libclang (best-effort, -I libraries, skip function bodies).
  3. Extract class/struct + their methods/fields that are DEFINED in that file.
  4. INSERT into symbols any (name, file, start_line) not already present. Dedup against the
     existing tree-sitter graph; prefer real definitions (end_line > start_line).
Edges/call-graph are left to the tree-sitter pass (full type-resolved call edges = later).
"""
import sys, glob
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CORPUS = KB / 'corpus' / 'ardupilot'
sys.path.insert(0, str(KB))
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import duckdb
import clang.cindex as cx
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser
from build_symbol_graph import domain_for_file  # reuse canonical domain mapping

TS = Parser(Language(tscpp.language()))
IDX = cx.Index.create()
ARGS = ['-x', 'c++', '-std=c++11', '-ferror-limit=0', '-w',
        f'-I{CORPUS}', f'-I{CORPUS}/libraries', f'-I{CORPUS}/libraries/AP_HAL',
        f'-I{CORPUS}/modules/mavlink']
OPTS = (cx.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES |
        cx.TranslationUnit.PARSE_INCOMPLETE)
KINDS = {cx.CursorKind.CLASS_DECL: 'class', cx.CursorKind.STRUCT_DECL: 'struct',
         cx.CursorKind.CXX_METHOD: 'method', cx.CursorKind.FUNCTION_DECL: 'function',
         cx.CursorKind.FIELD_DECL: 'field'}


def error_headers():
    out = []
    for h in glob.glob(f'{CORPUS}/libraries/**/*.h', recursive=True):
        if TS.parse(open(h, 'rb').read()).root_node.has_error:
            out.append(h)
    return out


def qualname(cur):
    parts = []
    c = cur
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        if c.spelling:
            parts.append(c.spelling)
        c = c.semantic_parent
    return '::'.join(reversed(parts))


def extract(tu, rel):
    """Yield (kind, name, qualified_name, rel, start, end) for defs located in this file."""
    fname = (CORPUS / rel).name
    seen = set()
    # class/struct: require a real definition (body), not a forward decl.
    # method/function/field: header declarations are not is_definition() but are still the
    # symbols we want ("which file declares X"), so accept them.
    needdef = {cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL}
    def walk(cur):
        for c in cur.get_children():
            loc = c.location.file
            if (loc and fname in loc.name and c.kind in KINDS and c.spelling
                    and (c.is_definition() or c.kind not in needdef)):
                s, e = c.extent.start.line, c.extent.end.line
                key = (c.spelling, s)
                if key not in seen:
                    seen.add(key)
                    yield (KINDS[c.kind], c.spelling, qualname(c), rel, s, e)
            yield from walk(c)
    yield from walk(tu.cursor)


def main():
    con = duckdb.connect(str(KB / 'structured' / 'kb.duckdb'))
    existing = set(con.execute("SELECT name, file, start_line FROM symbols").fetchall())
    next_id = con.execute("SELECT max(id) FROM symbols").fetchone()[0] + 1
    hdrs = error_headers()
    print(f"error headers: {len(hdrs)}")
    rows, by_kind = [], {}
    for h in hdrs:
        rel = str(Path(h).relative_to(CORPUS))
        try:
            tu = IDX.parse(h, args=ARGS, options=OPTS)
        except Exception as e:
            print(f"  parse fail {rel}: {e}"); continue
        domain = domain_for_file(rel)
        for kind, name, qn, r, s, e in extract(tu, rel):
            if (name, r, s) in existing:
                continue
            existing.add((name, r, s))
            rows.append((next_id, kind, name, qn, r, s, e, domain, f"{kind} {qn}"))
            by_kind[kind] = by_kind.get(kind, 0) + 1
            next_id += 1
    if rows:
        con.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?)", rows)
    print(f"recovered symbols: {len(rows)}  by_kind: {by_kind}")
    print("total symbols now:", con.execute("SELECT count(*) FROM symbols").fetchone()[0])
    con.close()


if __name__ == '__main__':
    main()
