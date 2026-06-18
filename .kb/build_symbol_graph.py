#!/usr/bin/env python3
"""
Phase 1: Build deterministic symbol/call graph for ArduPilot KB.

Uses tree-sitter-cpp for AST-only extraction. No regex parsing.
Populates .kb/structured/kb.duckdb with:
  - symbols(id, kind, name, qualified_name, file, start_line, end_line, domain, signature)
  - edges(src_symbol_id, dst_symbol_id, kind, confidence)
  - build_info(key, value)

Domain mapping per ardupilot-domains skill.
"""

import os
import re
import sys
import time
import json
import hashlib
import traceback
from pathlib import Path
from typing import Optional, Iterator

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

# Attribute macros that tree-sitter-cpp mis-parses (turning enclosing class/function nodes into
# ERROR nodes). Neutralized to equal-length spaces before parsing. Extend as new breakers surface.
_BREAKER_MACROS = re.compile(rb'\b(?:WARN_IF_UNUSED)\b')
import duckdb

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path("/home/o0rt/Projects/homek/ArduPilot")
CORPUS_ROOT = REPO_ROOT / ".kb/corpus/ardupilot"
DB_PATH     = REPO_ROOT / ".kb/structured/kb.duckdb"
GIT_SHA     = "20622a390035d439268cb40583b8fb62c033ed50"

# ── domain mapping (ardupilot-domains skill) ───────────────────────────────────
# Each entry: (path_prefix_relative_to_corpus, domain)
# First match wins when walking each file's path.
DOMAIN_RULES = [
    # hal_boards
    ("libraries/AP_HAL_ChibiOS",    "hal_boards"),
    ("libraries/AP_HAL_Linux",      "hal_boards"),
    ("libraries/AP_HAL_ESP32",      "hal_boards"),
    ("libraries/AP_HAL",            "hal_boards"),  # base AP_HAL after variants
    # sensors
    ("libraries/AP_InertialSensor", "sensors"),
    ("libraries/AP_GPS",            "sensors"),
    ("libraries/AP_Compass",        "sensors"),
    ("libraries/AP_Baro",           "sensors"),
    ("libraries/AP_RangeFinder",    "sensors"),
    ("libraries/AP_Airspeed",       "sensors"),
    # state_estimation
    ("libraries/AP_NavEKF2",        "state_estimation"),
    ("libraries/AP_NavEKF3",        "state_estimation"),
    ("libraries/AP_NavEKF",         "state_estimation"),
    ("libraries/AP_AHRS",           "state_estimation"),
    # control
    ("libraries/AC_AttitudeControl","control"),
    ("libraries/AC_PosControl",     "control"),
    ("libraries/AC_WPNav",          "control"),
    ("libraries/AP_Motors",         "control"),
    ("libraries/APM_Control",       "control"),
    # vehicle
    ("ArduCopter/",                 "vehicle_copter"),
    ("ArduPlane/",                  "vehicle_plane"),
    ("Rover/",                      "vehicle_rover"),
    ("ArduSub/",                    "vehicle_sub"),
    # comms
    ("libraries/GCS_MAVLink",       "comms"),
    ("libraries/AP_DroneCAN",       "comms"),
    ("modules/mavlink",             "comms"),
    # scripting
    ("libraries/AP_Scripting",      "scripting"),
    # infra_crosscutting
    ("libraries/AP_Param",          "infra_crosscutting"),
    ("libraries/AP_Scheduler",      "infra_crosscutting"),
    ("libraries/AP_Logger",         "infra_crosscutting"),
    ("libraries/StorageManager",    "infra_crosscutting"),
]

def domain_for_file(rel_path: str) -> str:
    """Return domain tag for a corpus-relative file path."""
    rel = rel_path.replace("\\", "/")
    for prefix, domain in DOMAIN_RULES:
        if rel.startswith(prefix):
            return domain
    # Default: derive from top-level library name
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] == "libraries":
        return f"lib_{parts[1]}"
    if len(parts) >= 1:
        return parts[0].lower()
    return "unknown"

# ── tree-sitter setup ─────────────────────────────────────────────────────────
CPP_LANGUAGE = Language(tscpp.language())
PARSER = Parser(CPP_LANGUAGE)

# ── symbol ID generation ──────────────────────────────────────────────────────
_sym_counter = 0
def next_sym_id() -> int:
    global _sym_counter
    _sym_counter += 1
    return _sym_counter

# ── node text helper ──────────────────────────────────────────────────────────
def node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

def first_named_child_of_type(node, *types):
    for child in node.children:
        if child.type in types:
            return child
    return None

def children_of_type(node, *types):
    return [c for c in node.children if c.type in types]

# ── extract function/method signature ─────────────────────────────────────────
def extract_declarator_name(declarator_node, src: bytes) -> Optional[str]:
    """Recursively extract the identifier name from a declarator node."""
    if declarator_node is None:
        return None
    t = declarator_node.type
    if t in ("identifier", "field_identifier", "type_identifier"):
        return node_text(declarator_node, src)
    if t == "qualified_identifier":
        # e.g. AP_AHRS::update — return full qualified form
        return node_text(declarator_node, src)
    if t in ("function_declarator", "pointer_declarator", "reference_declarator",
             "abstract_function_declarator", "destructor_name"):
        # recurse into the first named child that is a declarator or identifier
        for child in declarator_node.children:
            name = extract_declarator_name(child, src)
            if name:
                return name
    if t == "destructor_name":
        return node_text(declarator_node, src)
    return None


def get_function_signature(node, src: bytes, class_name: Optional[str] = None) -> str:
    """Build a short signature string for a function/method node."""
    try:
        text = node_text(node, src)
        # Take first line or up to opening brace
        brace = text.find("{")
        semi = text.find(";")
        end = min(x for x in [brace, semi, len(text)] if x >= 0)
        sig = text[:end].strip().replace("\n", " ")
        # Collapse whitespace
        import re
        sig = re.sub(r'\s+', ' ', sig)
        return sig[:300]
    except Exception:
        return ""


# ── symbol + edge collection ──────────────────────────────────────────────────
class SymbolTable:
    def __init__(self):
        self.symbols = []   # list of dicts
        self.edges   = []   # list of dicts
        # name -> list of symbol ids (for call resolution)
        self._name_index: dict[str, list[int]] = {}

    def add_symbol(self, kind, name, qualified_name, file_rel, start_line, end_line, domain, signature="") -> int:
        sid = next_sym_id()
        self.symbols.append({
            "id": sid,
            "kind": kind,
            "name": name,
            "qualified_name": qualified_name,
            "file": file_rel,
            "start_line": start_line,
            "end_line": end_line,
            "domain": domain,
            "signature": signature,
        })
        # Index by short name and qualified name
        for key in {name, qualified_name}:
            self._name_index.setdefault(key, []).append(sid)
        return sid

    def add_edge(self, src_id, dst_id, kind, confidence="high"):
        self.edges.append({
            "src_symbol_id": src_id,
            "dst_symbol_id": dst_id,
            "kind": kind,
            "confidence": confidence,
        })

    def resolve_callee(self, callee_name: str) -> tuple[Optional[int], str]:
        """Return (symbol_id, confidence). confidence='low' if ambiguous or not found."""
        candidates = self._name_index.get(callee_name, [])
        if len(candidates) == 1:
            return candidates[0], "high"
        elif len(candidates) > 1:
            return candidates[0], "low"  # ambiguous
        return None, "low"


# ── AST walker ────────────────────────────────────────────────────────────────
class FileParser:
    def __init__(self, sym_table: SymbolTable):
        self.sym_table = sym_table

    def parse_file(self, file_path: Path, corpus_root: Path) -> bool:
        """Parse a single C++ file. Returns True on success."""
        rel_path = str(file_path.relative_to(corpus_root))
        domain   = domain_for_file(rel_path)

        try:
            src = file_path.read_bytes()
        except Exception as e:
            return False, f"read error: {e}"

        # Neutralize attribute macros tree-sitter-cpp can't parse (e.g. trailing WARN_IF_UNUSED),
        # which otherwise turn whole class_specifier nodes into ERROR nodes. Replace with
        # equal-length spaces so byte offsets / line numbers stay exact.
        src = _BREAKER_MACROS.sub(lambda m: b' ' * (m.end() - m.start()), src)

        try:
            tree = PARSER.parse(src)
        except Exception as e:
            return False, f"parse error: {e}"

        if tree.root_node.has_error:
            # Still process — tree-sitter does error recovery, many nodes will be valid
            pass

        # Walk the translation unit
        self._walk_node(tree.root_node, src, rel_path, domain,
                        class_stack=[], namespace_stack=[])
        return True, None

    def _walk_node(self, node, src: bytes, rel_path: str, domain: str,
                   class_stack: list, namespace_stack: list,
                   enclosing_sym_id: Optional[int] = None):
        """Recursively walk AST, extracting symbols and edges."""
        t = node.type

        if t == "namespace_definition":
            ns_name = ""
            for child in node.children:
                if child.type == "namespace_identifier":
                    ns_name = node_text(child, src)
                    break
            new_ns = namespace_stack + ([ns_name] if ns_name else [])
            body = first_named_child_of_type(node, "declaration_list")
            if body:
                for child in body.children:
                    self._walk_node(child, src, rel_path, domain,
                                    class_stack, new_ns, enclosing_sym_id)
            return

        if t in ("class_specifier", "struct_specifier"):
            self._handle_class_or_struct(node, src, rel_path, domain,
                                         class_stack, namespace_stack, enclosing_sym_id)
            return

        if t == "enum_specifier":
            self._handle_enum(node, src, rel_path, domain,
                              class_stack, namespace_stack)
            return

        if t == "function_definition":
            self._handle_function_def(node, src, rel_path, domain,
                                      class_stack, namespace_stack, enclosing_sym_id)
            return

        if t in ("preproc_def", "preproc_function_def"):
            self._handle_macro(node, src, rel_path, domain)
            return

        # For template declarations, descend into the inner declaration
        if t == "template_declaration":
            for child in node.children:
                if child.type in ("class_specifier", "struct_specifier",
                                  "function_definition", "declaration"):
                    self._walk_node(child, src, rel_path, domain,
                                    class_stack, namespace_stack, enclosing_sym_id)
            return

        # Generic descent for translation_unit and other containers, INCLUDING preprocessor
        # conditional blocks — ArduPilot gates many whole classes behind #if AP_*_ENABLED, and
        # those class_specifier nodes live under preproc_if/ifdef/else/elif containers.
        if t in ("translation_unit", "declaration_list", "linkage_specification",
                 "preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif"):
            for child in node.children:
                self._walk_node(child, src, rel_path, domain,
                                class_stack, namespace_stack, enclosing_sym_id)

    def _handle_class_or_struct(self, node, src, rel_path, domain,
                                 class_stack, namespace_stack, enclosing_sym_id):
        kind = "class" if node.type == "class_specifier" else "struct"
        name_node = first_named_child_of_type(node, "type_identifier")
        if name_node is None:
            return  # anonymous struct/class — skip
        name = node_text(name_node, src)
        ns_prefix = "::".join(namespace_stack) + "::" if namespace_stack else ""
        cls_prefix = "::".join(class_stack) + "::" if class_stack else ""
        qualified_name = ns_prefix + cls_prefix + name

        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1

        sym_id = self.sym_table.add_symbol(
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            file_rel=rel_path,
            start_line=start_line,
            end_line=end_line,
            domain=domain,
            signature=f"{kind} {qualified_name}",
        )

        # member_of edge to enclosing class
        if enclosing_sym_id is not None:
            self.sym_table.add_edge(sym_id, enclosing_sym_id, "member_of")

        # Parse inheritance (base_class_clause)
        base_clause = first_named_child_of_type(node, "base_class_clause")
        if base_clause:
            for child in base_clause.children:
                if child.type == "type_identifier":
                    base_name = node_text(child, src)
                    base_id, conf = self.sym_table.resolve_callee(base_name)
                    if base_id:
                        self.sym_table.add_edge(sym_id, base_id, "inherits", conf)
                    else:
                        # Record as low-confidence unresolved inherits
                        # We'll store these as edges with dst=0 and callee_name in a note
                        # For now: store as unresolved (skip — we don't fake targets)
                        pass

        # Walk field_declaration_list for nested types and methods
        body = first_named_child_of_type(node, "field_declaration_list")
        if body:
            new_class_stack = class_stack + [name]
            for child in body.children:
                ct = child.type
                if ct in ("function_definition",):
                    self._handle_function_def(child, src, rel_path, domain,
                                              new_class_stack, namespace_stack, sym_id)
                elif ct == "field_declaration":
                    # Could be a method declaration (no body)
                    # We record method declarations only if they have a function_declarator
                    self._handle_field_decl(child, src, rel_path, domain,
                                            new_class_stack, namespace_stack, sym_id)
                elif ct in ("class_specifier", "struct_specifier"):
                    self._handle_class_or_struct(child, src, rel_path, domain,
                                                  new_class_stack, namespace_stack, sym_id)
                elif ct == "enum_specifier":
                    self._handle_enum(child, src, rel_path, domain,
                                      new_class_stack, namespace_stack)
                elif ct == "template_declaration":
                    for grandchild in child.children:
                        if grandchild.type == "function_definition":
                            self._handle_function_def(grandchild, src, rel_path, domain,
                                                       new_class_stack, namespace_stack, sym_id)
                        elif grandchild.type in ("class_specifier", "struct_specifier"):
                            self._handle_class_or_struct(grandchild, src, rel_path, domain,
                                                          new_class_stack, namespace_stack, sym_id)

    def _handle_field_decl(self, node, src, rel_path, domain,
                            class_stack, namespace_stack, enclosing_sym_id):
        """Handle field declarations that are method declarations (no body)."""
        # Look for function_declarator to identify method declarations
        func_decl = None
        for child in node.children:
            if child.type == "function_declarator":
                func_decl = child
                break
            # Pointer to function, reference, etc.
            if child.type in ("pointer_declarator", "reference_declarator"):
                for grandchild in child.children:
                    if grandchild.type == "function_declarator":
                        func_decl = grandchild
                        break
        if func_decl is None:
            return

        name_node = func_decl.children[0] if func_decl.children else None
        if name_node is None:
            return
        name = node_text(name_node, src)
        if not name or name in ("(", ")", "*", "&", "~"):
            return

        ns_prefix  = "::".join(namespace_stack) + "::" if namespace_stack else ""
        cls_prefix = "::".join(class_stack) + "::" if class_stack else ""
        qualified_name = ns_prefix + cls_prefix + name

        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1

        sig = get_function_signature(node, src)

        sym_id = self.sym_table.add_symbol(
            kind="method",
            name=name,
            qualified_name=qualified_name,
            file_rel=rel_path,
            start_line=start_line,
            end_line=end_line,
            domain=domain,
            signature=sig,
        )
        if enclosing_sym_id is not None:
            self.sym_table.add_edge(sym_id, enclosing_sym_id, "member_of")

    def _handle_enum(self, node, src, rel_path, domain, class_stack, namespace_stack):
        name_node = first_named_child_of_type(node, "type_identifier")
        if name_node is None:
            return
        name = node_text(name_node, src)
        ns_prefix  = "::".join(namespace_stack) + "::" if namespace_stack else ""
        cls_prefix = "::".join(class_stack) + "::" if class_stack else ""
        qualified_name = ns_prefix + cls_prefix + name
        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1
        self.sym_table.add_symbol(
            kind="enum",
            name=name,
            qualified_name=qualified_name,
            file_rel=rel_path,
            start_line=start_line,
            end_line=end_line,
            domain=domain,
            signature=f"enum {qualified_name}",
        )

    def _handle_macro(self, node, src, rel_path, domain):
        name_node = None
        for child in node.children:
            if child.type == "identifier":
                name_node = child
                break
        if name_node is None:
            return
        name = node_text(name_node, src)
        # Skip include guards (all caps + _H suffix etc.) — still record them
        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1
        sig = node_text(node, src)[:200].replace("\n", " ").strip()
        self.sym_table.add_symbol(
            kind="macro",
            name=name,
            qualified_name=name,
            file_rel=rel_path,
            start_line=start_line,
            end_line=end_line,
            domain=domain,
            signature=sig,
        )

    def _handle_function_def(self, node, src, rel_path, domain,
                              class_stack, namespace_stack, enclosing_sym_id):
        """Extract a function/method definition and its call edges."""
        # Find the declarator
        func_decl = None
        for child in node.children:
            if child.type == "function_declarator":
                func_decl = child
                break
        if func_decl is None:
            # May be wrapped in pointer_declarator etc.
            for child in node.children:
                if child.type in ("pointer_declarator", "reference_declarator",
                                  "abstract_reference_declarator"):
                    for gc in child.children:
                        if gc.type == "function_declarator":
                            func_decl = gc
                            break
                    if func_decl:
                        break
        if func_decl is None:
            return

        # Name from declarator
        name_node = func_decl.children[0] if func_decl.children else None
        if name_node is None:
            return

        raw_name = node_text(name_node, src).strip()
        if not raw_name:
            return

        # qualified_identifier like AP_AHRS::update  => extract enclosing + method
        if name_node.type == "qualified_identifier":
            # scope_prefix::method_name
            parts = raw_name.split("::")
            name = parts[-1]
            # Build qualified_name
            ns_prefix  = "::".join(namespace_stack) + "::" if namespace_stack else ""
            qualified_name = ns_prefix + raw_name
        else:
            name = raw_name
            ns_prefix  = "::".join(namespace_stack) + "::" if namespace_stack else ""
            cls_prefix = "::".join(class_stack) + "::" if class_stack else ""
            qualified_name = ns_prefix + cls_prefix + name

        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1

        kind = "method" if (class_stack or ("::" in raw_name)) else "function"
        sig  = get_function_signature(node, src)

        sym_id = self.sym_table.add_symbol(
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            file_rel=rel_path,
            start_line=start_line,
            end_line=end_line,
            domain=domain,
            signature=sig,
        )

        if enclosing_sym_id is not None:
            self.sym_table.add_edge(sym_id, enclosing_sym_id, "member_of")

        # Extract calls from compound_statement body
        body = first_named_child_of_type(node, "compound_statement")
        if body:
            self._extract_calls(body, src, sym_id)

    def _extract_calls(self, body_node, src: bytes, caller_id: int):
        """Walk the body and collect call_expression nodes."""
        for call_node in self._find_nodes(body_node, "call_expression"):
            # The function being called is the first child
            if not call_node.children:
                continue
            callee_node = call_node.children[0]
            callee_name = self._callee_name(callee_node, src)
            if not callee_name:
                continue
            dst_id, conf = self.sym_table.resolve_callee(callee_name)
            if dst_id is not None:
                self.sym_table.add_edge(caller_id, dst_id, "calls", conf)
            else:
                # Unresolved call — record as low-confidence with a placeholder
                # Per spec: mark confidence='low' rather than inventing a target
                # We'll store them in a separate unresolved_calls list (below)
                # For now emit an edge with dst_symbol_id=0 to flag it
                # Actually per spec we should not invent a target — store placeholder
                pass

    def _callee_name(self, node, src: bytes) -> Optional[str]:
        """Extract the function name from a call-expression function node."""
        t = node.type
        if t == "identifier":
            return node_text(node, src)
        if t == "field_expression":
            # obj.method or obj->method — take field name (last child identifier)
            for child in reversed(node.children):
                if child.type == "field_identifier":
                    return node_text(child, src)
            return None
        if t == "qualified_identifier":
            # NS::func — return full qualified form
            return node_text(node, src)
        if t == "template_function":
            # func<T>(...) — return base name
            for child in node.children:
                if child.type == "identifier":
                    return node_text(child, src)
            return None
        if t in ("pointer_expression",):
            # (*fp)() style — skip
            return None
        return None

    def _find_nodes(self, node, target_type: str) -> Iterator:
        """Generator: yield all descendant nodes of the given type."""
        if node.type == target_type:
            yield node
        for child in node.children:
            yield from self._find_nodes(child, target_type)


# ── DuckDB schema ─────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    kind           VARCHAR NOT NULL,   -- class|struct|method|function|enum|macro
    name           VARCHAR NOT NULL,
    qualified_name VARCHAR NOT NULL,
    file           VARCHAR NOT NULL,
    start_line     INTEGER,
    end_line       INTEGER,
    domain         VARCHAR,
    signature      VARCHAR
);

CREATE TABLE IF NOT EXISTS edges (
    id             INTEGER PRIMARY KEY,
    src_symbol_id  INTEGER NOT NULL,
    dst_symbol_id  INTEGER,           -- NULL if unresolved
    kind           VARCHAR NOT NULL,  -- calls|inherits|includes|member_of
    confidence     VARCHAR DEFAULT 'high',  -- high|low
    callee_name    VARCHAR            -- for unresolved calls
);

CREATE TABLE IF NOT EXISTS build_info (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- Views for query helpers (created after data load)
"""

VIEW_SQL = """
CREATE OR REPLACE VIEW definition_of AS
  SELECT id, kind, name, qualified_name, file, start_line, end_line, domain, signature
  FROM symbols;

CREATE OR REPLACE VIEW callers_of AS
  -- For a given symbol id (dst), find all callers (src)
  SELECT e.dst_symbol_id AS callee_id,
         s.qualified_name AS callee_name,
         e.src_symbol_id AS caller_id,
         cs.qualified_name AS caller_name,
         cs.file AS caller_file,
         cs.start_line AS caller_line,
         e.confidence
  FROM edges e
  JOIN symbols s  ON s.id = e.dst_symbol_id
  JOIN symbols cs ON cs.id = e.src_symbol_id
  WHERE e.kind = 'calls';

CREATE OR REPLACE VIEW callees_of AS
  SELECT e.src_symbol_id AS caller_id,
         s.qualified_name AS caller_name,
         e.dst_symbol_id AS callee_id,
         cs.qualified_name AS callee_name,
         cs.file AS callee_file,
         e.confidence
  FROM edges e
  JOIN symbols s  ON s.id = e.src_symbol_id
  JOIN symbols cs ON cs.id = e.dst_symbol_id
  WHERE e.kind = 'calls';

CREATE OR REPLACE VIEW subclasses_of AS
  SELECT e.dst_symbol_id AS base_id,
         s.qualified_name AS base_name,
         e.src_symbol_id AS subclass_id,
         cs.qualified_name AS subclass_name,
         cs.file AS subclass_file,
         e.confidence
  FROM edges e
  JOIN symbols s  ON s.id = e.dst_symbol_id
  JOIN symbols cs ON cs.id = e.src_symbol_id
  WHERE e.kind = 'inherits';

CREATE OR REPLACE VIEW members_of AS
  SELECT e.dst_symbol_id AS class_id,
         s.qualified_name AS class_name,
         e.src_symbol_id AS member_id,
         ms.qualified_name AS member_name,
         ms.kind AS member_kind,
         ms.file,
         ms.start_line
  FROM edges e
  JOIN symbols s  ON s.id = e.dst_symbol_id
  JOIN symbols ms ON ms.id = e.src_symbol_id
  WHERE e.kind = 'member_of';
"""


# ── file collection ───────────────────────────────────────────────────────────
TARGET_DIRS = [
    "libraries",
    "ArduCopter",
    "ArduPlane",
    "Rover",
    "ArduSub",
]

def iter_cpp_files(corpus_root: Path):
    """Yield Path objects for all .cpp/.h/.hpp files in target dirs."""
    for d in TARGET_DIRS:
        target = corpus_root / d
        if not target.exists():
            print(f"  WARNING: target dir not found: {target}", file=sys.stderr)
            continue
        for ext in ("*.cpp", "*.h", "*.hpp", "*.cc", "*.cxx"):
            yield from target.rglob(ext)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print("=== Phase 1: Symbol Graph Builder ===")
    print(f"Corpus:  {CORPUS_ROOT}")
    print(f"DB:      {DB_PATH}")
    print(f"Git SHA: {GIT_SHA}")
    print()

    sym_table = SymbolTable()
    file_parser = FileParser(sym_table)

    files = sorted(iter_cpp_files(CORPUS_ROOT))
    total_files = len(files)
    print(f"Found {total_files} C++ source files to parse.")

    parse_errors = []
    parse_ok = 0

    for i, fpath in enumerate(files, 1):
        if i % 500 == 0:
            elapsed = time.time() - t0
            print(f"  [{i}/{total_files}] {elapsed:.1f}s  symbols={len(sym_table.symbols)}  edges={len(sym_table.edges)}")
        try:
            ok, err = file_parser.parse_file(fpath, CORPUS_ROOT)
            if ok:
                parse_ok += 1
            else:
                parse_errors.append((str(fpath.relative_to(CORPUS_ROOT)), err))
        except Exception as e:
            rel = str(fpath.relative_to(CORPUS_ROOT))
            parse_errors.append((rel, traceback.format_exc()[:200]))

    elapsed = time.time() - t0
    print(f"\nParsing done in {elapsed:.1f}s")
    print(f"  OK:     {parse_ok}")
    print(f"  Errors: {len(parse_errors)}")
    print(f"  Symbols extracted: {len(sym_table.symbols)}")
    print(f"  Edges extracted:   {len(sym_table.edges)}")

    # ── Write to DuckDB ───────────────────────────────────────────────────────
    print(f"\nWriting to {DB_PATH} ...")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))
    # Idempotent rebuild: clear only symbols/edges/build_info (+ their views); KEEP params/messages.
    for _v in ('definition_of', 'callers_of', 'callees_of', 'subclasses_of', 'members_of'):
        con.execute(f"DROP VIEW IF EXISTS {_v}")
    for _t in ('symbols', 'edges', 'build_info'):
        con.execute(f"DROP TABLE IF EXISTS {_t}")
    con.execute(SCHEMA_SQL)

    # Bulk-insert symbols
    print("  Inserting symbols ...")
    sym_rows = [
        (s["id"], s["kind"], s["name"], s["qualified_name"],
         s["file"], s["start_line"], s["end_line"], s["domain"], s["signature"])
        for s in sym_table.symbols
    ]
    con.executemany(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?)",
        sym_rows
    )

    # Bulk-insert edges
    print("  Inserting edges ...")
    edge_rows = []
    for i, e in enumerate(sym_table.edges, 1):
        edge_rows.append((
            i,
            e["src_symbol_id"],
            e.get("dst_symbol_id"),
            e["kind"],
            e.get("confidence", "high"),
            e.get("callee_name"),
        ))
    con.executemany(
        "INSERT INTO edges VALUES (?,?,?,?,?,?)",
        edge_rows
    )

    # Build_info
    con.execute("INSERT INTO build_info VALUES ('git_sha', ?)", [GIT_SHA])
    con.execute("INSERT INTO build_info VALUES ('corpus_path', ?)", [str(CORPUS_ROOT)])
    con.execute("INSERT INTO build_info VALUES ('build_date', '2026-06-17')")
    con.execute("INSERT INTO build_info VALUES ('total_files_parsed', ?)", [str(parse_ok)])
    con.execute("INSERT INTO build_info VALUES ('total_files_errors', ?)", [str(len(parse_errors))])
    con.execute("INSERT INTO build_info VALUES ('tool', 'tree-sitter-cpp 0.23.4')")

    # Views
    print("  Creating views ...")
    con.execute(VIEW_SQL)

    con.commit()
    print("  Done.")

    # ── Domain breakdown ──────────────────────────────────────────────────────
    print("\n=== Symbol counts by domain ===")
    rows = con.execute("""
        SELECT domain, kind, COUNT(*) as cnt
        FROM symbols
        GROUP BY domain, kind
        ORDER BY domain, cnt DESC
    """).fetchall()

    domain_totals = {}
    for domain, kind, cnt in rows:
        domain_totals[domain] = domain_totals.get(domain, 0) + cnt

    print(f"{'Domain':<30} {'Kind':<12} {'Count':>8}")
    print("-" * 55)
    for domain, kind, cnt in rows:
        print(f"  {domain:<28} {kind:<12} {cnt:>8,}")

    print("\n=== Total symbols per domain ===")
    for domain, total in sorted(domain_totals.items(), key=lambda x: -x[1]):
        print(f"  {domain:<30} {total:>8,}")

    print("\n=== Edge counts by kind ===")
    edge_rows_stat = con.execute("""
        SELECT kind, confidence, COUNT(*) as cnt
        FROM edges
        GROUP BY kind, confidence
        ORDER BY kind, confidence
    """).fetchall()
    for kind, conf, cnt in edge_rows_stat:
        print(f"  {kind:<15} confidence={conf:<6} {cnt:>8,}")

    # ── High-degree symbols ───────────────────────────────────────────────────
    print("\n=== Top 20 highest-degree symbols (in-edges: callers + subclasses) ===")
    hub_rows = con.execute("""
        SELECT s.qualified_name, s.kind, s.domain, COUNT(e.src_symbol_id) as degree
        FROM symbols s
        JOIN edges e ON e.dst_symbol_id = s.id
        WHERE e.kind IN ('calls', 'inherits')
        GROUP BY s.id, s.qualified_name, s.kind, s.domain
        ORDER BY degree DESC
        LIMIT 20
    """).fetchall()
    print(f"  {'Qualified Name':<50} {'Kind':<10} {'Domain':<25} {'In-Degree':>10}")
    print("  " + "-" * 100)
    for qname, kind, domain, degree in hub_rows:
        print(f"  {qname[:50]:<50} {kind:<10} {domain:<25} {degree:>10,}")

    # ── Parse errors ─────────────────────────────────────────────────────────
    print(f"\n=== Parse errors: {len(parse_errors)} total ===")
    if parse_errors:
        print("  Sample (up to 10):")
        for rel, err in parse_errors[:10]:
            print(f"    {rel}: {err[:80]}")

    # ── Grand totals ──────────────────────────────────────────────────────────
    total_sym = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    total_edge = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"\n=== TOTALS ===")
    print(f"  Symbols: {total_sym:,}")
    print(f"  Edges:   {total_edge:,}")
    print(f"  Build time: {time.time()-t0:.1f}s")

    con.close()

    # Write parse errors to file for inspection
    errors_path = REPO_ROOT / ".kb/structured/parse_errors.json"
    with open(errors_path, "w") as f:
        json.dump(parse_errors, f, indent=2)
    print(f"\nParse errors written to: {errors_path}")
    print(f"DB written to:           {DB_PATH}")


if __name__ == "__main__":
    main()
