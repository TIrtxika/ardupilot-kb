#!/usr/bin/env python3
"""
Phase 2 — AST/section-aware chunker for ArduPilot KB.

Produces:
  .kb/chunks/cpp_chunks.jsonl        — C++ function/class/method chunks via tree-sitter
  .kb/chunks/rst_chunks.jsonl        — RST section chunks
  .kb/chunks/param_chunks.jsonl      — one chunk per param row
  .kb/chunks/message_chunks.jsonl    — one chunk per message (grouped by msg_name)

Each chunk has metadata:
  {chunk_id, domain, source_path, symbol_id?, start_line, end_line,
   enclosing_class?, includes?, section_path?, chunk_type, text}
"""

import os, sys, json, re, hashlib, time
from pathlib import Path

# Paths
KB_ROOT     = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
CORPUS_CPP  = KB_ROOT / 'corpus' / 'ardupilot'
CORPUS_WIKI = KB_ROOT / 'corpus' / 'ardupilot_wiki'
CHUNKS_DIR  = KB_ROOT / 'chunks'
DB_PATH     = KB_ROOT / 'structured' / 'kb.duckdb'

CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

# Add venv to path
sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import duckdb
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

# ──────────────────────────────────────────────────────────────────────────────
# Domain assignment helper (mirrors ardupilot-domains skill)
# ──────────────────────────────────────────────────────────────────────────────
DOMAIN_PREFIXES = [
    ('libraries/AP_HAL',               'hal_boards'),
    ('libraries/AP_HAL_ChibiOS',       'hal_boards'),
    ('libraries/AP_HAL_Linux',         'hal_boards'),
    ('libraries/AP_HAL_ESP32',         'hal_boards'),
    ('libraries/AP_InertialSensor',    'sensors'),
    ('libraries/AP_GPS',               'sensors'),
    ('libraries/AP_Compass',           'sensors'),
    ('libraries/AP_Baro',              'sensors'),
    ('libraries/AP_RangeFinder',       'sensors'),
    ('libraries/AP_Airspeed',          'sensors'),
    ('libraries/AP_NavEKF',            'state_estimation'),
    ('libraries/AP_NavEKF2',           'state_estimation'),
    ('libraries/AP_NavEKF3',           'state_estimation'),
    ('libraries/AP_AHRS',              'state_estimation'),
    ('libraries/AC_AttitudeControl',   'control'),
    ('libraries/AC_PosControl',        'control'),
    ('libraries/AC_WPNav',             'control'),
    ('libraries/AP_Motors',            'control'),
    ('libraries/APM_Control',          'control'),
    ('ArduCopter/',                    'vehicle_copter'),
    ('ArduPlane/',                     'vehicle_plane'),
    ('Rover/',                         'vehicle_rover'),
    ('ArduSub/',                       'vehicle_sub'),
    ('libraries/GCS_MAVLink',          'comms'),
    ('libraries/AP_DroneCAN',          'comms'),
    ('modules/mavlink',                'comms'),
    ('libraries/AP_Scripting',         'scripting'),
    ('libraries/AP_Param',             'infra_crosscutting'),
    ('libraries/AP_Scheduler',         'infra_crosscutting'),
    ('libraries/AP_Logger',            'infra_crosscutting'),
    ('libraries/StorageManager',       'infra_crosscutting'),
]

def assign_domain(rel_path: str) -> str:
    for prefix, domain in DOMAIN_PREFIXES:
        if rel_path.startswith(prefix):
            return domain
    return 'other'

# ──────────────────────────────────────────────────────────────────────────────
# Tree-sitter C++ chunker
# ──────────────────────────────────────────────────────────────────────────────
CPP_LANG = Language(tscpp.language())
CPP_PARSER = Parser(CPP_LANG)

MAX_TOKENS_APPROX = 1500  # ~4 chars/token → 6000 chars max per chunk

def chunk_id(source_path: str, start: int, end: int, extra: str = '') -> str:
    raw = f"{source_path}:{start}:{end}:{extra}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

def extract_includes(source: str) -> list[str]:
    """Extract #include lines from source text."""
    return re.findall(r'#include\s+[<"]([^>"]+)[>"]', source)

def get_file_source(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except Exception:
        return None

# Map of qualified_name -> symbol row from DB
_symbol_map: dict[str, dict] = {}
_file_symbols: dict[str, list[dict]] = {}

def load_symbol_map(con):
    """Load symbols keyed by (file, start_line) for fast lookup."""
    rows = con.execute(
        'SELECT id, kind, name, qualified_name, file, start_line, end_line, domain, signature '
        'FROM symbols WHERE start_line IS NOT NULL'
    ).fetchall()
    for r in rows:
        sym = dict(zip(['id','kind','name','qualified_name','file','start_line','end_line','domain','signature'], r))
        key = f"{sym['file']}:{sym['start_line']}"
        _symbol_map[key] = sym
        _file_symbols.setdefault(sym['file'], []).append(sym)
    print(f"[chunker] Loaded {len(_symbol_map)} symbols from DB")

def find_symbol(rel_file: str, start_line: int) -> dict | None:
    key = f"{rel_file}:{start_line}"
    if key in _symbol_map:
        return _symbol_map[key]
    # Try ±2 lines for off-by-one
    for delta in [1, -1, 2, -2]:
        key2 = f"{rel_file}:{start_line + delta}"
        if key2 in _symbol_map:
            return _symbol_map[key2]
    return None

def node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode('utf-8', errors='replace')

def node_lines(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1

def split_large_chunk(text: str, source_path: str, start_line: int, chunk_type: str,
                       metadata_base: dict, includes: list) -> list[dict]:
    """Split a large chunk on blank lines, keeping parts linked."""
    if len(text) <= MAX_TOKENS_APPROX * 4:
        return []  # not oversized, caller handles

    lines = text.split('\n')
    parts = []
    current = []
    current_start = start_line
    part_idx = 0

    for i, line in enumerate(lines):
        current.append(line)
        if len('\n'.join(current)) > MAX_TOKENS_APPROX * 4 and line.strip() == '':
            part_text = '\n'.join(current)
            part_end = current_start + len(current) - 1
            m = dict(metadata_base)
            m['chunk_id'] = chunk_id(source_path, current_start, part_end, f'part{part_idx}')
            m['start_line'] = current_start
            m['end_line'] = part_end
            m['text'] = part_text
            m['part_index'] = part_idx
            m['part_of'] = chunk_id(source_path, start_line, start_line + len(lines) - 1)
            m['includes'] = includes
            parts.append(m)
            part_idx += 1
            current = []
            current_start = start_line + i + 1

    if current:
        part_text = '\n'.join(current)
        part_end = current_start + len(current) - 1
        m = dict(metadata_base)
        m['chunk_id'] = chunk_id(source_path, current_start, part_end, f'part{part_idx}')
        m['start_line'] = current_start
        m['end_line'] = part_end
        m['text'] = part_text
        m['part_index'] = part_idx
        m['part_of'] = chunk_id(source_path, start_line, start_line + len(lines) - 1)
        m['includes'] = includes
        parts.append(m)

    return parts

def chunk_cpp_file(abs_path: Path, rel_path: str) -> list[dict]:
    """Parse a C++ file with tree-sitter and emit function/class/method chunks."""
    source_bytes = get_file_source(abs_path)
    if source_bytes is None:
        return []

    source_text = source_bytes.decode('utf-8', errors='replace')
    includes = extract_includes(source_text)
    domain = assign_domain(rel_path)

    try:
        tree = CPP_PARSER.parse(source_bytes)
    except Exception as e:
        return []

    chunks = []
    root = tree.root_node

    def emit_chunk(node, enclosing_class: str | None = None, chunk_type: str = 'function'):
        text = node_text(node, source_bytes)
        start_l, end_l = node_lines(node)
        sym = find_symbol(rel_path, start_l)

        # Synthetic header per ast-chunking skill
        if sym:
            qualified = sym['qualified_name']
            symbol_id = sym['id']
            sym_domain = sym['domain'] or domain
        else:
            # Derive name from node
            name_node = node.child_by_field_name('name') or node.child_by_field_name('declarator')
            qualified = name_node.text.decode('utf-8', errors='replace') if name_node else f"unknown@{start_l}"
            symbol_id = None
            sym_domain = domain

        header = f"// {qualified} @ {rel_path}:{start_l}-{end_l}\n"
        full_text = header + text

        base_meta = {
            'chunk_id': chunk_id(rel_path, start_l, end_l),
            'chunk_type': chunk_type,
            'domain': sym_domain,
            'source_path': rel_path,
            'symbol_id': symbol_id,
            'start_line': start_l,
            'end_line': end_l,
            'enclosing_class': enclosing_class,
            'includes': includes,
        }

        # Check if oversized
        if len(full_text) > MAX_TOKENS_APPROX * 4:
            parts = split_large_chunk(full_text, rel_path, start_l, chunk_type, base_meta, includes)
            if parts:
                chunks.extend(parts)
                return

        base_meta['text'] = full_text
        chunks.append(base_meta)

    def visit(node, enclosing_class: str | None = None, depth: int = 0):
        ntype = node.type

        if ntype in ('function_definition', 'function_declaration'):
            emit_chunk(node, enclosing_class, 'function')
            # Don't recurse into function body for nested functions
            return

        elif ntype == 'class_specifier':
            # Get class name
            class_name_node = node.child_by_field_name('name')
            class_name = class_name_node.text.decode('utf-8', errors='replace') if class_name_node else 'unknown'
            start_l, end_l = node_lines(node)

            # Emit class header chunk (members list without full bodies)
            body = node.child_by_field_name('body')
            if body:
                # Build a header chunk: class decl + member signatures only
                class_text = source_text.split('\n')
                header_lines = []
                for i in range(start_l - 1, min(end_l, start_l + 50)):
                    if i < len(class_text):
                        header_lines.append(class_text[i])
                class_header_text = f"// {class_name} @ {rel_path}:{start_l}-{end_l}\n" + '\n'.join(header_lines[:50])

                sym = find_symbol(rel_path, start_l)
                chunks.append({
                    'chunk_id': chunk_id(rel_path, start_l, end_l, 'class_header'),
                    'chunk_type': 'class_header',
                    'domain': sym['domain'] if sym else domain,
                    'source_path': rel_path,
                    'symbol_id': sym['id'] if sym else None,
                    'start_line': start_l,
                    'end_line': end_l,
                    'enclosing_class': None,
                    'includes': includes,
                    'text': class_header_text,
                })

                # Recurse into class body for methods
                for child in body.children:
                    visit(child, class_name, depth + 1)
            return

        elif ntype in ('struct_specifier', 'union_specifier'):
            start_l, end_l = node_lines(node)
            text = node_text(node, source_bytes)
            name_node = node.child_by_field_name('name')
            struct_name = name_node.text.decode('utf-8', errors='replace') if name_node else 'anon'
            sym = find_symbol(rel_path, start_l)

            full_text = f"// {struct_name} @ {rel_path}:{start_l}-{end_l}\n" + text
            chunks.append({
                'chunk_id': chunk_id(rel_path, start_l, end_l),
                'chunk_type': 'struct',
                'domain': sym['domain'] if sym else domain,
                'source_path': rel_path,
                'symbol_id': sym['id'] if sym else None,
                'start_line': start_l,
                'end_line': end_l,
                'enclosing_class': enclosing_class,
                'includes': includes,
                'text': full_text,
            })
            return

        elif ntype == 'namespace_definition':
            for child in node.children:
                visit(child, enclosing_class, depth + 1)
            return

        elif ntype in ('preproc_ifdef', 'preproc_if', 'preproc_else', 'preproc_elif'):
            for child in node.children:
                visit(child, enclosing_class, depth + 1)
            return

        else:
            # Recurse into other top-level constructs
            if depth < 3:
                for child in node.children:
                    visit(child, enclosing_class, depth + 1)

    visit(root)
    return chunks


def chunk_all_cpp(con) -> int:
    """Chunk all C++ files in the corpus, write to cpp_chunks.jsonl."""
    load_symbol_map(con)

    out_path = CHUNKS_DIR / 'cpp_chunks.jsonl'

    cpp_files = list(CORPUS_CPP.rglob('*.cpp')) + list(CORPUS_CPP.rglob('*.h'))
    # Filter out modules subdirectory (not fetched) and test files that bloat
    cpp_files = [f for f in cpp_files if 'modules' not in str(f)]

    print(f"[chunker] Processing {len(cpp_files)} C++ files...")

    total_chunks = 0
    errors = []
    t0 = time.time()

    with open(out_path, 'w') as fout:
        for i, abs_path in enumerate(cpp_files):
            rel_path = str(abs_path.relative_to(CORPUS_CPP))
            try:
                file_chunks = chunk_cpp_file(abs_path, rel_path)
                for c in file_chunks:
                    fout.write(json.dumps(c) + '\n')
                total_chunks += len(file_chunks)
            except Exception as e:
                errors.append({'file': rel_path, 'error': str(e)})

            if (i + 1) % 200 == 0:
                elapsed = time.time() - t0
                print(f"  [{i+1}/{len(cpp_files)}] {total_chunks} chunks so far, {elapsed:.0f}s elapsed")

    print(f"[chunker] C++ done: {total_chunks} chunks from {len(cpp_files)} files, {len(errors)} errors")
    if errors:
        error_path = CHUNKS_DIR / 'cpp_chunk_errors.json'
        with open(error_path, 'w') as f:
            json.dump(errors[:50], f, indent=2)  # save first 50 errors
        print(f"  Errors logged to {error_path}")

    return total_chunks


# ──────────────────────────────────────────────────────────────────────────────
# RST section chunker
# ──────────────────────────────────────────────────────────────────────────────

# RST heading underline chars in precedence order
RST_HEADING_CHARS = '=-~^"\'`#*+<>'

def rst_heading_level(char: str, overline: bool) -> int:
    """Return numeric level for a heading underline char."""
    base = RST_HEADING_CHARS.find(char)
    return base if base >= 0 else 99

def parse_rst_sections(text: str, filepath: str) -> list[dict]:
    """
    Parse RST into sections by heading boundaries.
    Returns list of {heading, heading_path, level, start_line, end_line, text, section_path}.
    """
    lines = text.split('\n')
    n = len(lines)

    # Detect heading positions
    # A heading in RST is: optional overline + title + underline (same char, >= title length)
    sections_raw = []  # (line_idx, title, char, level)

    # Build level map as we encounter chars (per-file hierarchy)
    char_to_level: dict[str, int] = {}
    level_counter = 0

    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if len(stripped) >= 2 and all(c == stripped[0] for c in stripped) and stripped[0] in RST_HEADING_CHARS:
            char = stripped[0]
            # Check if this is an underline (line above is title)
            if i > 0 and lines[i-1].strip() and len(stripped) >= len(lines[i-1].strip()):
                title = lines[i-1].strip()
                if char not in char_to_level:
                    char_to_level[char] = level_counter
                    level_counter += 1
                lvl = char_to_level[char]
                # Overline check (line before title)
                start = i - 1
                if i >= 2 and lines[i-2].strip() == stripped:
                    start = i - 2
                sections_raw.append((start, title, char, lvl))
                i += 1
                continue
        i += 1

    if not sections_raw:
        # No sections found — emit the whole file as one chunk
        return [{
            'heading': Path(filepath).stem,
            'heading_path': [Path(filepath).stem],
            'level': 0,
            'start_line': 1,
            'end_line': len(lines),
            'text': text,
            'section_path': Path(filepath).stem,
        }]

    # Build section chunks
    results = []
    for idx, (line_idx, title, char, lvl) in enumerate(sections_raw):
        start_line = line_idx + 1  # 1-indexed
        end_line = sections_raw[idx + 1][0] if idx + 1 < len(sections_raw) else len(lines)
        chunk_text = '\n'.join(lines[line_idx:end_line])

        # Build heading path based on level hierarchy
        path_components = [title]
        # Walk back to find ancestor headings
        for prev_idx in range(idx - 1, -1, -1):
            prev_lvl = sections_raw[prev_idx][3]
            if prev_lvl < lvl:
                path_components.insert(0, sections_raw[prev_idx][1])
                lvl = prev_lvl

        results.append({
            'heading': title,
            'heading_path': path_components,
            'level': sections_raw[idx][3],
            'start_line': start_line,
            'end_line': end_line,
            'text': chunk_text,
            'section_path': ' > '.join(path_components),
        })

    return results


def wiki_domain_from_path(rel_path: str) -> list[str]:
    """Assign 1-2 domains to a wiki RST file based on its path."""
    p = rel_path.lower()
    domains = []
    if 'copter' in p:
        domains.append('vehicle_copter')
    if 'plane' in p:
        domains.append('vehicle_plane')
    if 'rover' in p:
        domains.append('vehicle_rover')
    if 'sub' in p:
        domains.append('vehicle_sub')
    if 'antenna' in p:
        domains.append('other')
    if 'param' in p or 'parameter' in p:
        domains.append('infra_crosscutting')
    if 'mavlink' in p or 'dronecan' in p:
        domains.append('comms')
    if 'ekf' in p or 'ahrs' in p:
        domains.append('state_estimation')
    if 'motors' in p or 'attitude' in p or 'pid' in p:
        domains.append('control')
    if not domains:
        domains = ['other']
    return list(set(domains[:2]))


def chunk_all_rst() -> int:
    """Chunk all RST files in the wiki corpus."""
    out_path = CHUNKS_DIR / 'rst_chunks.jsonl'

    rst_files = list(CORPUS_WIKI.rglob('*.rst'))
    print(f"[chunker] Processing {len(rst_files)} RST files...")

    total_chunks = 0
    errors = []
    t0 = time.time()

    with open(out_path, 'w') as fout:
        for i, abs_path in enumerate(rst_files):
            rel_path = str(abs_path.relative_to(CORPUS_WIKI))
            try:
                text = abs_path.read_text(errors='replace')
                sections = parse_rst_sections(text, rel_path)
                domains = wiki_domain_from_path(rel_path)

                for sec in sections:
                    # Split oversized sections
                    sec_text = sec['text']
                    if len(sec_text) > MAX_TOKENS_APPROX * 4:
                        lines = sec_text.split('\n')
                        max_lines = MAX_TOKENS_APPROX * 4 // 80  # ~80 chars/line
                        for part_i, offset in enumerate(range(0, len(lines), max_lines)):
                            part_lines = lines[offset:offset + max_lines]
                            part_text = '\n'.join(part_lines)
                            part_start = sec['start_line'] + offset
                            part_end = part_start + len(part_lines) - 1
                            c = {
                                'chunk_id': chunk_id(rel_path, part_start, part_end, f'rst{part_i}'),
                                'chunk_type': 'rst_section',
                                'domain': domains[0],
                                'domains': domains,
                                'source_path': rel_path,
                                'symbol_id': None,
                                'start_line': part_start,
                                'end_line': part_end,
                                'heading': sec['heading'],
                                'section_path': sec['section_path'],
                                'part_index': part_i,
                                'text': part_text,
                            }
                            fout.write(json.dumps(c) + '\n')
                            total_chunks += 1
                    else:
                        c = {
                            'chunk_id': chunk_id(rel_path, sec['start_line'], sec['end_line'], 'rst'),
                            'chunk_type': 'rst_section',
                            'domain': domains[0],
                            'domains': domains,
                            'source_path': rel_path,
                            'symbol_id': None,
                            'start_line': sec['start_line'],
                            'end_line': sec['end_line'],
                            'heading': sec['heading'],
                            'section_path': sec['section_path'],
                            'text': sec['text'],
                        }
                        fout.write(json.dumps(c) + '\n')
                        total_chunks += 1
            except Exception as e:
                errors.append({'file': rel_path, 'error': str(e)})

            if (i + 1) % 200 == 0:
                elapsed = time.time() - t0
                print(f"  [{i+1}/{len(rst_files)}] {total_chunks} chunks so far, {elapsed:.0f}s elapsed")

    print(f"[chunker] RST done: {total_chunks} chunks from {len(rst_files)} files, {len(errors)} errors")
    return total_chunks


# ──────────────────────────────────────────────────────────────────────────────
# Structured param + message chunks
# ──────────────────────────────────────────────────────────────────────────────

def chunk_params(con) -> int:
    """Emit one chunk per param row from DuckDB."""
    out_path = CHUNKS_DIR / 'param_chunks.jsonl'

    rows = con.execute(
        'SELECT id, name, vehicle, "group", display_name, description, units, '
        'range_min, range_max, increment, values_map, default_val, default_note, '
        'bitmask, file, line, domain FROM params'
    ).fetchall()

    cols = ['id','name','vehicle','group','display_name','description','units',
            'range_min','range_max','increment','values_map','default_val','default_note',
            'bitmask','file','line','domain']

    total = 0
    with open(out_path, 'w') as fout:
        for row in rows:
            r = dict(zip(cols, row))
            # Build human-readable text for embedding
            parts = [f"Parameter: {r['name']}"]
            if r['vehicle']:
                parts.append(f"Vehicle: {r['vehicle']}")
            if r['group']:
                parts.append(f"Group: {r['group']}")
            if r['display_name']:
                parts.append(f"Display name: {r['display_name']}")
            if r['description']:
                parts.append(f"Description: {r['description']}")
            if r['units']:
                parts.append(f"Units: {r['units']}")
            if r['range_min'] or r['range_max']:
                parts.append(f"Range: {r['range_min']} to {r['range_max']}")
            if r['default_val']:
                parts.append(f"Default: {r['default_val']}")
            if r['values_map']:
                parts.append(f"Values: {r['values_map']}")
            if r['bitmask']:
                parts.append(f"Bitmask: {r['bitmask']}")
            if r['default_note']:
                parts.append(f"Note: {r['default_note']}")

            file_ref = r['file'] or ''
            line_ref = r['line'] or 0

            chunk = {
                'chunk_id': f"param_{r['id']}",
                'chunk_type': 'param',
                'domain': r['domain'] or 'infra_crosscutting',
                'source_path': file_ref,
                'symbol_id': None,
                'param_id': r['id'],
                'param_name': r['name'],
                'vehicle': r['vehicle'],
                'start_line': line_ref,
                'end_line': line_ref,
                'text': '\n'.join(parts),
                # Keep raw fields for exact-fact matching
                'default_val': r['default_val'],
                'range_min': r['range_min'],
                'range_max': r['range_max'],
                'units': r['units'],
            }
            fout.write(json.dumps(chunk) + '\n')
            total += 1

    print(f"[chunker] Params done: {total} param chunks")
    return total


def chunk_messages(con) -> int:
    """Emit one chunk per MAVLink/log message (grouped by name)."""
    out_path = CHUNKS_DIR / 'message_chunks.jsonl'

    rows = con.execute(
        'SELECT id, msg_id, name, field_name, field_type, units, description, source, file, line '
        'FROM messages ORDER BY name, id'
    ).fetchall()

    cols = ['id','msg_id','name','field_name','field_type','units','description','source','file','line']

    # Group by message name
    from collections import defaultdict
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        r = dict(zip(cols, row))
        by_name[r['name']].append(r)

    total = 0
    with open(out_path, 'w') as fout:
        for msg_name, fields in by_name.items():
            first = fields[0]
            parts = [f"Message: {msg_name}"]
            if first['msg_id']:
                parts.append(f"ID: {first['msg_id']}")
            if first['source']:
                parts.append(f"Source: {first['source']}")
            parts.append("Fields:")
            for f in fields:
                field_desc = f"  {f['field_name']}: {f['field_type']}"
                if f['units']:
                    field_desc += f" [{f['units']}]"
                if f['description']:
                    field_desc += f" — {f['description']}"
                parts.append(field_desc)

            file_ref = first['file'] or ''
            line_ref = first['line'] or 0

            chunk = {
                'chunk_id': f"msg_{msg_name}",
                'chunk_type': 'message',
                'domain': 'comms',
                'source_path': file_ref,
                'symbol_id': None,
                'msg_name': msg_name,
                'msg_id': first['msg_id'],
                'start_line': line_ref,
                'end_line': line_ref,
                'text': '\n'.join(parts),
            }
            fout.write(json.dumps(chunk) + '\n')
            total += 1

    print(f"[chunker] Messages done: {total} message chunks")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    print("Phase 2 — AST/Section-Aware Chunker")
    print("=" * 60)

    con = duckdb.connect(str(DB_PATH))

    t_start = time.time()

    cpp_count = chunk_all_cpp(con)
    rst_count = chunk_all_rst()
    param_count = chunk_params(con)
    msg_count = chunk_messages(con)

    total = cpp_count + rst_count + param_count + msg_count
    elapsed = time.time() - t_start

    print()
    print("=" * 60)
    print(f"CHUNKING COMPLETE")
    print(f"  C++ chunks:     {cpp_count}")
    print(f"  RST chunks:     {rst_count}")
    print(f"  Param chunks:   {param_count}")
    print(f"  Message chunks: {msg_count}")
    print(f"  TOTAL:          {total}")
    print(f"  Elapsed:        {elapsed:.1f}s")
    print(f"  Output dir:     {CHUNKS_DIR}")
    print("=" * 60)

    # Write summary
    summary = {
        'cpp_chunks': cpp_count,
        'rst_chunks': rst_count,
        'param_chunks': param_count,
        'message_chunks': msg_count,
        'total_chunks': total,
        'elapsed_seconds': round(elapsed, 1),
    }
    with open(CHUNKS_DIR / 'chunk_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)
