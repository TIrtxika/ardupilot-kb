#!/usr/bin/env python3
"""
Build message_handlers and param_bindings tables in kb.duckdb.
Uses grep-based deterministic extraction (no LLM, no regex-parsed C++ AST for structure beyond patterns).
"""
import re
import os
import duckdb
from pathlib import Path

CORPUS = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/corpus/ardupilot")
DB_PATH = "/home/o0rt/Projects/homek/ArduPilot/.kb/structured/kb.duckdb"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def relative_path(p: Path) -> str:
    """Return path relative to corpus root."""
    try:
        return str(p.relative_to(CORPUS))
    except ValueError:
        return str(p)


def read_file_lines(path: Path):
    """Read file lines with error handling for encoding."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# TABLE 1: message_handlers
# ---------------------------------------------------------------------------

# Target files: vehicle GCS MAVLink cpp + libraries/GCS_MAVLink/*.cpp
GCS_FILES = [
    CORPUS / "libraries/GCS_MAVLink/GCS_Common.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_MAVLink.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_Param.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_serial_control.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_ServoRelay.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_Fence.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_FTP.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_DeviceOp.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_Signing.cpp",
    CORPUS / "libraries/GCS_MAVLink/GCS_MAVLink_Parameters.cpp",
    CORPUS / "libraries/GCS_MAVLink/MissionItemProtocol.cpp",
    CORPUS / "libraries/GCS_MAVLink/MissionItemProtocol_Waypoints.cpp",
    CORPUS / "libraries/GCS_MAVLink/MissionItemProtocol_Fence.cpp",
    CORPUS / "libraries/GCS_MAVLink/MissionItemProtocol_Rally.cpp",
    CORPUS / "ArduCopter/GCS_MAVLink_Copter.cpp",
    CORPUS / "ArduCopter/GCS_Copter.cpp",
    CORPUS / "ArduPlane/GCS_MAVLink_Plane.cpp",
    CORPUS / "ArduPlane/GCS_Plane.cpp",
    CORPUS / "Rover/GCS_MAVLink_Rover.cpp",
    CORPUS / "Rover/GCS_Rover.cpp",
    CORPUS / "ArduSub/GCS_MAVLink_Sub.cpp",
    CORPUS / "ArduSub/GCS_Sub.cpp",
]

# Pattern for case MAVLINK_MSG_ID_<NAME>:
CASE_PAT = re.compile(r'case\s+MAVLINK_MSG_ID_([A-Z0-9_]+)\s*(?::{1}|:(?!\:))')

# Pattern for function definition that looks like it handles a message
# e.g. void GCS_MAVLINK::handle_mission_request(...)
# We track these separately to attribute cases to functions
FUNC_DEF_PAT = re.compile(
    r'^(?:void|bool|MAV_RESULT|int|uint\d+_t)\s+'
    r'((?:\w+::)*\w+)\s*\('
)

# Pattern for handle_ method definitions (for source='handler_fn')
HANDLER_FN_PAT = re.compile(
    r'^(?:void|bool|MAV_RESULT|int|uint\d+_t)\s+'
    r'((?:\w+::)*handle_[a-z_A-Z0-9]+)\s*\('
)

# Mapping from handle_snake_name -> MAVLINK message name
# Snake name to uppercase MAVLink message name heuristic:
# handle_mission_request -> MISSION_REQUEST
# handle_heartbeat -> HEARTBEAT
def snake_to_mavlink(snake_name: str) -> str:
    """Convert handle_<snake> -> <UPPER> MAVLink name."""
    # strip handle_ prefix
    if snake_name.startswith("handle_"):
        return snake_name[len("handle_"):].upper()
    return snake_name.upper()


def find_enclosing_function(lines, case_lineno):
    """
    Walk backward from case_lineno (0-indexed) to find the most recent
    function definition. Returns function name or empty string.
    """
    # Simple heuristic: look for a line matching function definition pattern
    # at or before the case, within 1000 lines
    for i in range(case_lineno, max(-1, case_lineno - 1000), -1):
        line = lines[i].rstrip()
        m = FUNC_DEF_PAT.match(line)
        if m:
            return m.group(1)
    return ""


def extract_message_handlers():
    """Extract case MAVLINK_MSG_ID_ entries and handler_ function defs."""
    rows = []  # (msg_name, handler, file, line, source)

    for fpath in GCS_FILES:
        if not fpath.exists():
            continue
        lines = read_file_lines(fpath)
        rel = relative_path(fpath)

        # Track current function context as we scan top-to-bottom
        current_fn = ""
        for lineno_0, line in enumerate(lines):
            lineno_1 = lineno_0 + 1  # 1-based

            # Update current function context
            fn_m = FUNC_DEF_PAT.match(line.lstrip())
            if fn_m:
                current_fn = fn_m.group(1)

            # Check for case MAVLINK_MSG_ID_
            case_m = CASE_PAT.search(line)
            if case_m:
                msg_name = case_m.group(1)
                # Use current_fn if available, else fall back to backward search
                handler = current_fn if current_fn else find_enclosing_function(lines, lineno_0)
                rows.append((msg_name, handler, rel, lineno_1, "case"))

    # Second pass: handler function definitions
    # We scan all GCS files for handle_<name>( method defs and map name -> MAVLink
    for fpath in GCS_FILES:
        if not fpath.exists():
            continue
        lines = read_file_lines(fpath)
        rel = relative_path(fpath)

        for lineno_0, line in enumerate(lines):
            lineno_1 = lineno_0 + 1
            # Match function DEFINITION (not declaration; look for { or multiline)
            # We include .h files? No, only .cpp per task spec
            hfn_m = HANDLER_FN_PAT.match(line.lstrip())
            if hfn_m:
                full_fn_name = hfn_m.group(1)
                # Extract just the method name (after last ::)
                method_part = full_fn_name.split("::")[-1]
                # Convert to MAVLink message name
                mavlink_name = snake_to_mavlink(method_part)
                # Only emit if plausibly a message handler (not e.g. handle_command_xxx
                # unless it maps to a known message - we include all and let verification filter)
                rows.append((mavlink_name, full_fn_name, rel, lineno_1, "handler_fn"))

    return rows


# ---------------------------------------------------------------------------
# TABLE 2: param_bindings
# ---------------------------------------------------------------------------

# Scan all .cpp files in corpus for AP_GROUPINFO variants
# Pattern: AP_GROUPINFO("LEAF", idx, Class, member, default)
# Also: AP_GROUPINFO_FLAGS("LEAF", idx, Class, member, default, flags)
# Also: AP_GROUPINFO_FRAME("LEAF", idx, Class, member, default, frame_flags)
# GSCALAR(member, "LEAF", default) -- class comes from file context (AP_PARAM_VEHICLE_NAME)
# GARRAY(member, index, "LEAF", default) -- similar

AP_GROUPINFO_PAT = re.compile(
    r'AP_GROUPINFO(?:_FLAGS_DEFAULT_POINTER|_FLAGS_FRAME|_FLAGS|_FRAME)?\s*\(\s*'
    r'"([^"]+)"\s*,\s*'       # leaf name
    r'[^,]+,\s*'              # index
    r'(\w+)\s*,\s*'           # class name
    r'(\w+)'                  # member name
    # rest (default, flags) we don't need
)

GSCALAR_PAT = re.compile(
    r'GSCALAR\s*\(\s*'
    r'(\w+)\s*,\s*'           # member (C++ var name)
    r'"([^"]+)"'              # leaf name
    # default follows but we don't need it
)

GARRAY_PAT = re.compile(
    r'GARRAY\s*\(\s*'
    r'(\w+)\s*,\s*'           # member
    r'[^,]+,\s*'              # index
    r'"([^"]+)"'              # leaf name
)


def find_all_cpp_files(root: Path):
    """Find all .cpp files in corpus (excluding tests, modules/mavlink generated)."""
    skip_prefixes = [
        "modules/",
        "tests/",
        "benchmarks/",
        "libraries/AP_Param/tests/",
    ]
    for p in sorted(root.rglob("*.cpp")):
        rel = str(p.relative_to(root))
        if any(rel.startswith(sp) for sp in skip_prefixes):
            continue
        yield p


def extract_param_bindings():
    """
    Extract AP_GROUPINFO/GSCALAR/GARRAY macro calls.
    Returns list of (leaf, class_name, member, file, line).
    """
    rows = []

    for fpath in find_all_cpp_files(CORPUS):
        rel = relative_path(fpath)
        lines = read_file_lines(fpath)

        for lineno_0, line in enumerate(lines):
            lineno_1 = lineno_0 + 1

            # AP_GROUPINFO variants
            m = AP_GROUPINFO_PAT.search(line)
            if m:
                leaf = m.group(1)
                class_name = m.group(2)
                member = m.group(3)
                rows.append((leaf, class_name, member, rel, lineno_1))
                continue  # don't also check GSCALAR on same line

            # GSCALAR
            m = GSCALAR_PAT.search(line)
            if m:
                member = m.group(1)
                leaf = m.group(2)
                # Determine class from file - look for AP_PARAM_VEHICLE_NAME in file
                # or use "Parameters" as default for vehicle Parameters.cpp files
                class_name = _infer_gscalar_class(lines, fpath)
                rows.append((leaf, class_name, member, rel, lineno_1))
                continue

            # GARRAY
            m = GARRAY_PAT.search(line)
            if m:
                member = m.group(1)
                leaf = m.group(2)
                class_name = _infer_gscalar_class(lines, fpath)
                rows.append((leaf, class_name, member, rel, lineno_1))

    return rows


_gscalar_class_cache = {}

def _infer_gscalar_class(lines, fpath: Path) -> str:
    """
    For GSCALAR/GARRAY, infer the Parameters class from the file.
    GSCALAR is defined as referencing AP_PARAM_VEHICLE_NAME.g.<member>
    which means the class is Parameters (the top-level parameter struct).
    We look for 'class Parameters' or 'AP_PARAM_VEHICLE_NAME' define.
    """
    key = str(fpath)
    if key in _gscalar_class_cache:
        return _gscalar_class_cache[key]

    # Look for AP_PARAM_VEHICLE_NAME define to find vehicle name
    vehicle_name_pat = re.compile(r'#define\s+AP_PARAM_VEHICLE_NAME\s+(\w+)')
    # Also look for class Parameters
    for line in lines:
        m = vehicle_name_pat.search(line)
        if m:
            _gscalar_class_cache[key] = "Parameters"
            return "Parameters"

    # Fallback: use "Parameters" since GSCALAR is always in Parameters.cpp files
    _gscalar_class_cache[key] = "Parameters"
    return "Parameters"


# ---------------------------------------------------------------------------
# Main: build tables in DuckDB
# ---------------------------------------------------------------------------

def main():
    print("Extracting message_handlers...")
    handler_rows = extract_message_handlers()
    print(f"  Raw rows extracted: {len(handler_rows)}")

    print("Extracting param_bindings...")
    param_rows = extract_param_bindings()
    print(f"  Raw rows extracted: {len(param_rows)}")

    print(f"Connecting to {DB_PATH}...")
    con = duckdb.connect(DB_PATH)

    # ---- TABLE 1: message_handlers ----
    print("Creating message_handlers table...")
    con.execute("DROP TABLE IF EXISTS message_handlers")
    con.execute("""
        CREATE TABLE message_handlers (
            msg_name  VARCHAR NOT NULL,
            handler   VARCHAR,
            file      VARCHAR NOT NULL,
            line      INTEGER NOT NULL,
            source    VARCHAR NOT NULL
        )
    """)

    if handler_rows:
        con.executemany(
            "INSERT INTO message_handlers VALUES (?, ?, ?, ?, ?)",
            handler_rows
        )

    n_handlers = con.execute("SELECT COUNT(*) FROM message_handlers").fetchone()[0]
    print(f"  Inserted {n_handlers} rows into message_handlers")

    # ---- TABLE 2: param_bindings ----
    print("Creating param_bindings table...")
    con.execute("DROP TABLE IF EXISTS param_bindings")
    con.execute("""
        CREATE TABLE param_bindings (
            full_name  VARCHAR,
            leaf       VARCHAR NOT NULL,
            class_name VARCHAR NOT NULL,
            member     VARCHAR NOT NULL,
            file       VARCHAR NOT NULL,
            line       INTEGER NOT NULL
        )
    """)

    # Join to params table to get full_name
    # params.name == leaf AND params.file == file
    # Insert with NULL full_name first, then update via join
    if param_rows:
        con.executemany(
            "INSERT INTO param_bindings (full_name, leaf, class_name, member, file, line) VALUES (NULL, ?, ?, ?, ?, ?)",
            param_rows
        )

    # Update full_name via join on (params.name == leaf AND params.file == file)
    con.execute("""
        UPDATE param_bindings pb
        SET full_name = (
            SELECT p.full_name
            FROM params p
            WHERE p.name = pb.leaf
              AND p.file = pb.file
            LIMIT 1
        )
    """)

    n_params = con.execute("SELECT COUNT(*) FROM param_bindings").fetchone()[0]
    n_matched = con.execute("SELECT COUNT(*) FROM param_bindings WHERE full_name IS NOT NULL").fetchone()[0]
    print(f"  Inserted {n_params} rows into param_bindings")
    print(f"  Rows with full_name matched to params table: {n_matched}")
    print(f"  Rows without match (full_name NULL): {n_params - n_matched}")

    # ---- Verification ----
    print("\n=== VERIFICATION: message_handlers ===")

    for msg in ["HEARTBEAT", "COMMAND_LONG", "MISSION_ITEM_INT"]:
        rows = con.execute(
            "SELECT msg_name, handler, file, line, source FROM message_handlers WHERE msg_name = ? ORDER BY source, line LIMIT 5",
            [msg]
        ).fetchall()
        print(f"\n  {msg}: {len(rows)} rows")
        for r in rows:
            print(f"    {r}")

    print("\n  Top 10 messages by handler count (case source only):")
    rows = con.execute("""
        SELECT msg_name, COUNT(*) as cnt
        FROM message_handlers
        WHERE source = 'case'
        GROUP BY msg_name
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"    {r}")

    print("\n=== VERIFICATION: param_bindings ===")

    # ATC_ANGLE_MAX verification
    rows = con.execute("""
        SELECT full_name, leaf, class_name, member, file, line
        FROM param_bindings
        WHERE leaf = 'ANGLE_MAX' AND class_name = 'AC_AttitudeControl'
        LIMIT 3
    """).fetchall()
    print(f"\n  ATC_ANGLE_MAX (leaf=ANGLE_MAX, class=AC_AttitudeControl): {len(rows)} rows")
    for r in rows:
        print(f"    {r}")

    # EK3_HGT_DELAY verification
    rows = con.execute("""
        SELECT full_name, leaf, class_name, member, file, line
        FROM param_bindings
        WHERE leaf = 'HGT_DELAY'
        LIMIT 3
    """).fetchall()
    print(f"\n  EK3_HGT_DELAY (leaf=HGT_DELAY): {len(rows)} rows")
    for r in rows:
        print(f"    {r}")

    # Sample rows
    print("\n  Sample param_bindings rows (first 5):")
    rows = con.execute("""
        SELECT full_name, leaf, class_name, member, file, line
        FROM param_bindings
        WHERE full_name IS NOT NULL
        LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"    {r}")

    print("\n=== SUMMARY ===")
    print(f"  message_handlers total rows: {n_handlers}")
    n_case = con.execute("SELECT COUNT(*) FROM message_handlers WHERE source='case'").fetchone()[0]
    n_hfn = con.execute("SELECT COUNT(*) FROM message_handlers WHERE source='handler_fn'").fetchone()[0]
    n_distinct_msgs = con.execute("SELECT COUNT(DISTINCT msg_name) FROM message_handlers WHERE source='case'").fetchone()[0]
    print(f"    source='case': {n_case}, source='handler_fn': {n_hfn}")
    print(f"    distinct MAVLink messages handled (case): {n_distinct_msgs}")
    print(f"  param_bindings total rows: {n_params}")
    print(f"    matched to params.full_name: {n_matched} / {n_params} ({100*n_matched//n_params if n_params else 0}%)")

    con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
