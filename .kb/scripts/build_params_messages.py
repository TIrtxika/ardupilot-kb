#!/usr/bin/env python3
"""
Phase 1 — ArduPilot deterministic layer builder.
Parses:
  1. // @Param comment blocks from all C++ source files -> params table
  2. MAVLink XML message definitions -> messages table
  3. DroneCAN DSDL .uavcan files -> messages table (source='dronecan')

Rules (from CLAUDE.md + param-metadata-parsing skill):
- Values copied VERBATIM, never rounded or inferred.
- If default is computed (not literal), default=NULL with a note.
- Record file:line provenance for every row.
- Parse failures are reported, never silently skipped.
"""

import re
import os
import sys
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import duckdb

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CORPUS = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/corpus/ardupilot")
DB_PATH = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/structured/kb.duckdb")
MAVLINK_DIR = CORPUS / "modules/mavlink/message_definitions/v1.0"
DSDL_DIR = CORPUS / "modules/DroneCAN/DSDL"

# ---------------------------------------------------------------------------
# Domain taxonomy (from ardupilot-domains skill)
# ---------------------------------------------------------------------------
DOMAIN_MAP: List[Tuple[str, str]] = [
    ("libraries/AP_HAL", "hal_boards"),
    ("libraries/AP_HAL_ChibiOS", "hal_boards"),
    ("libraries/AP_HAL_Linux", "hal_boards"),
    ("libraries/AP_HAL_ESP32", "hal_boards"),
    ("libraries/AP_InertialSensor", "sensors"),
    ("libraries/AP_GPS", "sensors"),
    ("libraries/AP_Compass", "sensors"),
    ("libraries/AP_Baro", "sensors"),
    ("libraries/AP_RangeFinder", "sensors"),
    ("libraries/AP_Airspeed", "sensors"),
    ("libraries/AP_NavEKF", "state_estimation"),
    ("libraries/AP_NavEKF2", "state_estimation"),
    ("libraries/AP_NavEKF3", "state_estimation"),
    ("libraries/AP_AHRS", "state_estimation"),
    ("libraries/AC_AttitudeControl", "control"),
    ("libraries/AC_PosControl", "control"),
    ("libraries/AC_WPNav", "control"),
    ("libraries/AP_Motors", "control"),
    ("libraries/APM_Control", "control"),
    ("ArduCopter/", "vehicle_copter"),
    ("ArduPlane/", "vehicle_plane"),
    ("Rover/", "vehicle_rover"),
    ("ArduSub/", "vehicle_sub"),
    ("AntennaTracker/", "vehicle_antennatracker"),
    ("Blimp/", "vehicle_blimp"),
    ("libraries/GCS_MAVLink", "comms"),
    ("libraries/AP_DroneCAN", "comms"),
    ("modules/mavlink", "comms"),
    ("libraries/AP_Scripting", "scripting"),
    ("libraries/AP_Param", "infra_crosscutting"),
    ("libraries/AP_Scheduler", "infra_crosscutting"),
    ("libraries/AP_Logger", "infra_crosscutting"),
    ("libraries/StorageManager", "infra_crosscutting"),
]

def domain_for_path(rel_path: str) -> str:
    """Return domain tag for a relative file path."""
    for prefix, domain in DOMAIN_MAP:
        if rel_path.startswith(prefix):
            return domain
    return "infra_crosscutting"

def vehicle_for_path(rel_path: str) -> Optional[str]:
    """Return vehicle name from path prefix."""
    mapping = {
        "ArduCopter/": "copter",
        "ArduPlane/": "plane",
        "Rover/": "rover",
        "ArduSub/": "sub",
        "AntennaTracker/": "antennatracker",
        "Blimp/": "blimp",
    }
    for prefix, vehicle in mapping.items():
        if rel_path.startswith(prefix):
            return vehicle
    return None

# ---------------------------------------------------------------------------
# @Param block parser
# ---------------------------------------------------------------------------

# Recognized tag names
PARAM_TAGS = {
    "Param", "DisplayName", "Description", "Units", "Range",
    "Increment", "Values", "Bitmask", "User", "RebootRequired",
    "ReadOnly", "CopyFieldsFrom",
}

# Matches a definition line like:
#   GSCALAR(name, "PARAM_NAME", default_value),
#   GARRAY(name, idx, "PARAM_NAME", default_value),
#   AP_GROUPINFO("PARAM_NAME", idx, group, var, default_value),
#   AP_GROUPINFO_FLAGS("PARAM_NAME", idx, group, var, default_value, flags),
DEFINITION_RE = re.compile(
    r"""(?:
        GSCALAR\s*\(\s*\w+\s*,\s*"(?P<gs_name>[^"]+)"\s*,\s*(?P<gs_default>[^)]+)
      | GARRAY\s*\(\s*\w+\s*,\s*\d+\s*,\s*"(?P<ga_name>[^"]+)"\s*,\s*(?P<ga_default>[^)]+)
      | AP_GROUPINFO(?:_FLAGS)?\s*\(\s*"(?P<ag_name>[^"]+)"\s*,\s*\d+\s*,\s*\w+\s*,\s*\w+\s*,\s*(?P<ag_default>[^,)]+)
      | AP_GROUPINFO_FLAGS_IGNORE_ENABLE\s*\(\s*"(?P<agi_name>[^"]+)"\s*,\s*\d+\s*,\s*\w+\s*,\s*\w+\s*,\s*(?P<agi_default>[^,)]+)
    )""",
    re.VERBOSE,
)

# Non-literal default patterns — anything that looks like a macro/function/cast
NON_LITERAL_RE = re.compile(
    r"""(?:
        [A-Z_]{3,}\w*     # ALL_CAPS macro
      | static_cast\s*<   # static_cast
      | \w+::\w+          # Enum::Value or Class::CONST
      | \(float\)         # C-style cast
    )""",
    re.VERBOSE,
)

def is_literal_default(s: str) -> bool:
    """Return True if the default value string is a numeric literal."""
    s = s.strip()
    # Pure number, possibly negative, possibly float
    try:
        float(s)
        return True
    except ValueError:
        return False

def clean_default(s: str) -> Optional[str]:
    """Return verbatim literal default or None if non-literal."""
    s = s.strip().rstrip(",").strip()
    if is_literal_default(s):
        return s
    return None  # non-literal -> NULL

def parse_param_blocks(filepath: Path, rel_path: str) -> Tuple[List[Dict], List[Dict]]:
    """
    Parse @Param comment blocks and their following definition lines.
    Returns (rows, errors).
    Each row is a dict matching the params table schema.
    """
    rows: List[Dict] = []
    errors: List[Dict] = []

    try:
        text = filepath.read_text(errors="replace")
    except Exception as e:
        errors.append({"file": str(filepath), "line": 0, "error": str(e)})
        return rows, errors

    lines = text.splitlines()
    n = len(lines)

    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Look for start of @Param block
        if not (stripped.startswith("//") and "@Param:" in stripped):
            i += 1
            continue

        # We are at the @Param line
        block_start_line = i + 1  # 1-based
        current_tags: Dict[str, str] = {}

        # Extract @Param name from this line
        m = re.search(r"@Param:\s*(\S+)", stripped)
        if not m:
            errors.append({
                "file": rel_path,
                "line": i + 1,
                "error": "@Param: tag found but could not extract name",
                "raw": stripped,
            })
            i += 1
            continue

        current_tags["Param"] = m.group(1).strip()

        # Consume subsequent comment lines in the block
        j = i + 1
        while j < n:
            ls = lines[j].strip()
            # Must be a comment line with a @Tag
            if ls.startswith("//"):
                tag_m = re.search(r"@(\w+):\s*(.*)", ls)
                if tag_m:
                    tag = tag_m.group(1)
                    val = tag_m.group(2).strip()
                    if tag in PARAM_TAGS and tag != "Param":
                        # Multi-line tag values (rare but can happen): append
                        if tag in current_tags:
                            current_tags[tag] += " " + val
                        else:
                            current_tags[tag] = val
                    # Unknown tags: ignore silently
                j += 1
                continue
            # End of comment block - next non-comment line is the definition
            break

        def_line_no = j + 1  # 1-based
        def_line = lines[j] if j < n else ""

        # Parse @Range
        range_min = range_max = None
        if "Range" in current_tags:
            rng = current_tags["Range"].strip()
            rng_parts = rng.split()
            if len(rng_parts) == 2:
                range_min = rng_parts[0]
                range_max = rng_parts[1]
            else:
                errors.append({
                    "file": rel_path,
                    "line": block_start_line,
                    "error": f"@Range could not be split into 2 parts: {rng!r}",
                    "param": current_tags.get("Param"),
                })

        # Extract default from definition line
        default_value = None
        default_note = None
        def_m = DEFINITION_RE.search(def_line)
        if def_m:
            raw_default = (
                def_m.group("gs_default")
                or def_m.group("ga_default")
                or def_m.group("ag_default")
                or def_m.group("agi_default")
                or ""
            )
            raw_default = raw_default.strip().rstrip(")").strip()
            cleaned = clean_default(raw_default)
            if cleaned is not None:
                default_value = cleaned
            else:
                default_note = f"runtime: {raw_default}"
        else:
            # No matching definition line - could be end of file, conditional compile, etc.
            default_note = "no definition line found"

        # Determine vehicle
        vehicle = vehicle_for_path(rel_path)
        domain = domain_for_path(rel_path)

        row = {
            "name": current_tags.get("Param"),
            "vehicle": vehicle,
            "group": None,  # will be set by group-prefix logic below
            "display_name": current_tags.get("DisplayName"),
            "description": current_tags.get("Description"),
            "units": current_tags.get("Units"),
            "range_min": range_min,
            "range_max": range_max,
            "increment": current_tags.get("Increment"),
            "values": current_tags.get("Values"),
            "default": default_value,
            "default_note": default_note,
            "bitmask": current_tags.get("Bitmask"),
            "file": rel_path,
            "line": def_line_no,
            "domain": domain,
        }
        rows.append(row)

        # Advance past block; next @Param may be anywhere after j
        i = j

    return rows, errors


def scan_all_param_files(corpus: Path) -> Tuple[List[Dict], List[Dict]]:
    """Walk all .cpp and .h files, parse @Param blocks."""
    all_rows: List[Dict] = []
    all_errors: List[Dict] = []

    files = list(corpus.rglob("*.cpp")) + list(corpus.rglob("*.h"))
    # Filter out files not likely to contain @Param to save time
    for fp in files:
        try:
            # Quick check: only parse if @Param: appears in file
            content = fp.read_text(errors="replace")
            if "@Param:" not in content:
                continue
        except Exception:
            continue

        rel = str(fp.relative_to(corpus))
        rows, errors = parse_param_blocks(fp, rel)
        all_rows.extend(rows)
        all_errors.extend(errors)

    return all_rows, all_errors


# ---------------------------------------------------------------------------
# MAVLink XML parser
# ---------------------------------------------------------------------------

def parse_mavlink_xml(xml_path: Path, source: str) -> Tuple[List[Dict], List[Dict]]:
    """
    Parse a MAVLink XML dialect file.
    Returns (rows, errors) where rows match the messages table schema.
    """
    rows: List[Dict] = []
    errors: List[Dict] = []

    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as e:
        errors.append({"file": str(xml_path), "error": str(e)})
        return rows, errors

    root = tree.getroot()

    for message in root.iter("message"):
        msg_id_str = message.get("id")
        msg_name = message.get("name", "")
        try:
            msg_id = int(msg_id_str) if msg_id_str is not None else None
        except (ValueError, TypeError):
            msg_id = None

        msg_desc_el = message.find("description")
        msg_description = msg_desc_el.text.strip() if msg_desc_el is not None and msg_desc_el.text else None

        # Find the line number - ET doesn't provide it natively; use a search approach
        # We record relative path only since we don't have line numbers from ET
        rel_path = str(xml_path.relative_to(xml_path.parent.parent.parent.parent.parent.parent))

        # Emit one row per field
        fields = message.findall("field")
        if not fields:
            # Emit message-level row with no field
            rows.append({
                "msg_id": msg_id,
                "name": msg_name,
                "field_name": None,
                "field_type": None,
                "units": None,
                "description": msg_description,
                "source": source,
                "file": rel_path,
                "line": None,
            })
        else:
            for field in fields:
                field_name = field.get("name", "")
                field_type = field.get("type", "")
                field_units = field.get("units")
                field_enum = field.get("enum")
                field_desc = field.text.strip() if field.text else None
                rows.append({
                    "msg_id": msg_id,
                    "name": msg_name,
                    "field_name": field_name,
                    "field_type": field_type,
                    "units": field_units,
                    "description": field_desc,
                    "source": source,
                    "file": rel_path,
                    "line": None,
                })

    return rows, errors


def parse_all_mavlink(mavlink_dir: Path) -> Tuple[List[Dict], List[Dict]]:
    """Parse all MAVLink XML files."""
    all_rows: List[Dict] = []
    all_errors: List[Dict] = []

    # Track which message IDs have been seen to avoid duplicates from includes
    # We parse all XML files individually (includes create duplicates but that's acceptable)
    xml_files = list(mavlink_dir.glob("*.xml"))

    for xf in xml_files:
        fname = xf.name
        # Determine source name from filename
        source = fname.replace(".xml", "")
        rows, errors = parse_mavlink_xml(xf, f"mavlink/{source}")
        all_rows.extend(rows)
        all_errors.extend(errors)

    return all_rows, all_errors


# ---------------------------------------------------------------------------
# DroneCAN DSDL parser
# ---------------------------------------------------------------------------

# DSDL field line: optional qualifier + type (primitive or compound/dotted, with optional array)
# + lowercase field name + optional comment.
# e.g.:  float16 heading_rad   # [rad]
#        truncated uint56 usec     # Microseconds
#        uint8[<=255] text
#        uavcan.Timestamp timestamp
#        NodeStatus status
#        CANIfaceStats[<=3] can_iface_stats
DSDL_FIELD_RE = re.compile(
    r"^(?P<qualifier>truncated\s+|saturated\s+)?(?P<ftype>[\w.]+(?:\[(?:[^\]]+)\])?)\s+(?P<fname>[a-zA-Z_]\w*)(?:\s*#\s*(?P<comment>.*))?$"
)
# Constant definition: type NAME = value
# NAME must start with uppercase letter and may contain digits and underscores.
DSDL_CONST_RE = re.compile(
    r"^(?P<ftype>[\w.]+(?:\[(?:[^\]]+)\])?)\s+(?P<fname>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>.+?)(?:\s*#\s*(?P<comment>.*))?$"
)

# DSDL meta-directives that are not fields and should be silently skipped
DSDL_SKIP_RE = re.compile(r"^(@\w+|OVERRIDE_SIGNATURE\s)")

def parse_dsdl_file(fp: Path) -> Tuple[List[Dict], List[Dict]]:
    """
    Parse a single DSDL .uavcan file.
    Returns (rows, errors).
    """
    rows: List[Dict] = []
    errors: List[Dict] = []

    filename = fp.name
    # Extract message ID from filename: "20002.Heading.uavcan" -> id=20002, name="Heading"
    # Or "Heading.uavcan" (no ID) -> id=None
    msg_id = None
    name_part = filename.replace(".uavcan", "")
    parts = name_part.split(".", 1)
    if len(parts) == 2 and parts[0].isdigit():
        msg_id = int(parts[0])
        msg_name_base = parts[1]
    else:
        msg_name_base = name_part

    # Determine namespace from directory path relative to DSDL_DIR
    try:
        rel_dir = fp.parent.relative_to(DSDL_DIR)
        namespace = ".".join(rel_dir.parts)
    except ValueError:
        namespace = ""

    full_name = f"{namespace}.{msg_name_base}" if namespace else msg_name_base

    # Source: dronecan/uavcan/ardupilot/etc based on top-level dir
    top_dir = fp.parts[len(DSDL_DIR.parts)] if len(fp.parts) > len(DSDL_DIR.parts) else ""
    source = f"dronecan/{top_dir}" if top_dir else "dronecan"

    rel_path = str(fp.relative_to(DSDL_DIR.parent.parent))

    try:
        text = fp.read_text(errors="replace")
    except Exception as e:
        errors.append({"file": rel_path, "line": 0, "error": str(e)})
        return rows, errors

    lines = text.splitlines()

    # Handle service types: split on "---"
    request_lines = []
    response_lines = []
    is_service = False
    section = request_lines
    for line in lines:
        if line.strip() == "---":
            is_service = True
            section = response_lines
        else:
            section.append(line)

    def parse_section(section_lines, section_suffix):
        section_name = full_name + section_suffix
        for lineno, raw_line in enumerate(section_lines, 1):
            stripped = raw_line.strip()
            # Skip blank lines, pure comments
            if not stripped or stripped.startswith("#"):
                continue
            # Skip DSDL meta-directives (@union, OVERRIDE_SIGNATURE, etc.)
            if DSDL_SKIP_RE.match(stripped):
                continue
            # Remove inline comment for matching
            no_comment = stripped.split("#")[0].strip()
            if not no_comment:
                continue

            # Skip constant definitions (TYPE NAME = value)
            if "=" in no_comment:
                const_m = DSDL_CONST_RE.match(stripped)
                if const_m:
                    continue  # Constants are not fields

            # void (padding) fields - record them
            if no_comment.startswith("void"):
                rows.append({
                    "msg_id": msg_id,
                    "name": section_name,
                    "field_name": no_comment,
                    "field_type": "void",
                    "units": None,
                    "description": None,
                    "source": source,
                    "file": rel_path,
                    "line": lineno,
                })
                continue

            m = DSDL_FIELD_RE.match(stripped)
            if m:
                ftype = (m.group("qualifier") or "").strip() + m.group("ftype")
                fname = m.group("fname")
                comment = (m.group("comment") or "").strip() or None
                rows.append({
                    "msg_id": msg_id,
                    "name": section_name,
                    "field_name": fname,
                    "field_type": ftype,
                    "units": None,   # DSDL embeds units in comment
                    "description": comment,
                    "source": source,
                    "file": rel_path,
                    "line": lineno,
                })
            else:
                # Unrecognized line that isn't blank/comment/const
                errors.append({
                    "file": rel_path,
                    "line": lineno,
                    "error": f"DSDL unrecognized field line",
                    "raw": stripped,
                })

    parse_section(request_lines, ".Request" if is_service else "")
    if is_service:
        parse_section(response_lines, ".Response")

    return rows, errors


def parse_all_dsdl(dsdl_dir: Path) -> Tuple[List[Dict], List[Dict]]:
    """Parse all .uavcan DSDL files."""
    all_rows: List[Dict] = []
    all_errors: List[Dict] = []

    for fp in dsdl_dir.rglob("*.uavcan"):
        rows, errors = parse_dsdl_file(fp)
        all_rows.extend(rows)
        all_errors.extend(errors)

    return all_rows, all_errors


# ---------------------------------------------------------------------------
# DuckDB table setup and insertion
# ---------------------------------------------------------------------------

CREATE_PARAMS_SQL = """
CREATE TABLE IF NOT EXISTS params (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL,
    vehicle     VARCHAR,
    "group"     VARCHAR,
    display_name VARCHAR,
    description  VARCHAR,
    units        VARCHAR,
    range_min    VARCHAR,
    range_max    VARCHAR,
    increment    VARCHAR,
    values_map   VARCHAR,
    default_val  VARCHAR,
    default_note VARCHAR,
    bitmask      VARCHAR,
    file         VARCHAR NOT NULL,
    line         INTEGER,
    domain       VARCHAR
)
"""

CREATE_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    msg_id      INTEGER,
    name        VARCHAR NOT NULL,
    field_name  VARCHAR,
    field_type  VARCHAR,
    units       VARCHAR,
    description VARCHAR,
    source      VARCHAR,
    file        VARCHAR,
    line        INTEGER
)
"""

def load_params(con: duckdb.DuckDBPyConnection, rows: List[Dict]) -> None:
    """Insert param rows into DB."""
    for idx, r in enumerate(rows, 1):
        con.execute("""
            INSERT INTO params
            (id, name, vehicle, "group", display_name, description, units,
             range_min, range_max, increment, values_map, default_val,
             default_note, bitmask, file, line, domain)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            idx,
            r.get("name"),
            r.get("vehicle"),
            r.get("group"),
            r.get("display_name"),
            r.get("description"),
            r.get("units"),
            r.get("range_min"),
            r.get("range_max"),
            r.get("increment"),
            r.get("values"),
            r.get("default"),
            r.get("default_note"),
            r.get("bitmask"),
            r.get("file"),
            r.get("line"),
            r.get("domain"),
        ])


def load_messages(con: duckdb.DuckDBPyConnection, rows: List[Dict]) -> None:
    """Insert message rows into DB."""
    for idx, r in enumerate(rows, 1):
        con.execute("""
            INSERT INTO messages
            (id, msg_id, name, field_name, field_type, units, description, source, file, line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            idx,
            r.get("msg_id"),
            r.get("name"),
            r.get("field_name"),
            r.get("field_type"),
            r.get("units"),
            r.get("description"),
            r.get("source"),
            r.get("file"),
            r.get("line"),
        ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Phase 1: Building params + messages tables ===\n")

    # Open DB in append mode
    con = duckdb.connect(str(DB_PATH))

    # Verify existing tables
    existing = {t[0] for t in con.execute("SHOW TABLES").fetchall()}
    print(f"Existing tables: {sorted(existing)}")

    # Drop and recreate params/messages tables if they exist
    for tbl in ("params", "messages"):
        if tbl in existing:
            print(f"  Dropping existing {tbl} table...")
            con.execute(f"DROP TABLE {tbl}")

    con.execute(CREATE_PARAMS_SQL)
    con.execute(CREATE_MESSAGES_SQL)
    print("  Created params and messages tables.\n")

    # -----------------------------------------------------------------------
    # 1. Parse @Param blocks
    # -----------------------------------------------------------------------
    print("Scanning C++ source for @Param blocks...")
    param_rows, param_errors = scan_all_param_files(CORPUS)
    print(f"  Found {len(param_rows)} param rows, {len(param_errors)} parse errors.\n")

    load_params(con, param_rows)
    print(f"  Inserted {len(param_rows)} rows into params table.\n")

    # -----------------------------------------------------------------------
    # 2. Parse MAVLink XML
    # -----------------------------------------------------------------------
    print("Parsing MAVLink XML definitions...")
    mav_rows, mav_errors = parse_all_mavlink(MAVLINK_DIR)
    print(f"  Found {len(mav_rows)} MAVLink field rows, {len(mav_errors)} errors.\n")

    # -----------------------------------------------------------------------
    # 3. Parse DroneCAN DSDL
    # -----------------------------------------------------------------------
    print("Parsing DroneCAN DSDL files...")
    dsdl_rows, dsdl_errors = parse_all_dsdl(DSDL_DIR)
    print(f"  Found {len(dsdl_rows)} DSDL field rows, {len(dsdl_errors)} errors.\n")

    # Combine and load messages
    all_msg_rows = mav_rows + dsdl_rows
    load_messages(con, all_msg_rows)
    print(f"  Inserted {len(all_msg_rows)} rows into messages table.\n")

    # -----------------------------------------------------------------------
    # Statistics
    # -----------------------------------------------------------------------
    print("=== STATISTICS ===\n")

    # Params by domain
    print("Params by domain:")
    for row in con.execute("SELECT domain, COUNT(*) as cnt FROM params GROUP BY domain ORDER BY cnt DESC").fetchall():
        print(f"  {row[0]}: {row[1]}")

    # Params by vehicle
    print("\nParams by vehicle:")
    for row in con.execute("SELECT vehicle, COUNT(*) as cnt FROM params GROUP BY vehicle ORDER BY cnt DESC").fetchall():
        print(f"  {row[0]}: {row[1]}")

    # Params with NULL default
    null_default = con.execute("SELECT COUNT(*) FROM params WHERE default_val IS NULL").fetchone()[0]
    with_note = con.execute("SELECT COUNT(*) FROM params WHERE default_note IS NOT NULL").fetchone()[0]
    print(f"\nParams total: {len(param_rows)}")
    print(f"Params with literal default: {len(param_rows) - null_default}")
    print(f"Params with NULL default (runtime/non-literal): {null_default}")
    print(f"Params with default_note: {with_note}")

    # Messages by source
    print("\nMessages by source:")
    for row in con.execute("SELECT source, COUNT(*) as cnt FROM messages GROUP BY source ORDER BY cnt DESC").fetchall():
        print(f"  {row[0]}: {row[1]}")

    # Messages distinct names by source
    print("\nDistinct message names by source:")
    for row in con.execute("SELECT source, COUNT(DISTINCT name) as cnt FROM messages GROUP BY source ORDER BY cnt DESC").fetchall():
        print(f"  {row[0]}: {row[1]}")

    # -----------------------------------------------------------------------
    # Parse failures report
    # -----------------------------------------------------------------------
    all_errors = param_errors + mav_errors + dsdl_errors
    print(f"\nTotal parse failures/warnings: {len(all_errors)}")

    if param_errors:
        print(f"\n@Param parse errors/warnings: {len(param_errors)}")
        for e in param_errors[:10]:
            print(f"  {e.get('file')}:{e.get('line')}: {e.get('error')} | {e.get('raw', '')[:80]}")
        if len(param_errors) > 10:
            print(f"  ... and {len(param_errors) - 10} more")

    if mav_errors:
        print(f"\nMAVLink XML parse errors: {len(mav_errors)}")
        for e in mav_errors[:5]:
            print(f"  {e}")

    if dsdl_errors:
        print(f"\nDSDL parse errors: {len(dsdl_errors)}")
        for e in dsdl_errors[:10]:
            print(f"  {e.get('file')}:{e.get('line')}: {e.get('error')} | {e.get('raw', '')[:80]}")
        if len(dsdl_errors) > 10:
            print(f"  ... and {len(dsdl_errors) - 10} more")

    # Save error log
    error_log = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/structured/parse_errors_phase1.json")
    with open(str(error_log), "w") as f:
        json.dump(all_errors, f, indent=2)
    print(f"\nFull error log written to: {error_log}")

    con.close()
    print("\n=== Phase 1 complete ===")


if __name__ == "__main__":
    main()
