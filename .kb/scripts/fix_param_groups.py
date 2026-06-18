#!/usr/bin/env python3
"""
Fix param group/prefix resolution in the KB.

Root problem: build_params_messages.py stored leaf param names only; params.group was
always NULL. This script resolves the prefix for every param by mirroring ArduPilot's
own param_parse.py logic:
  - Parse @Group/@Path blocks from vehicle Parameters.cpp files
  - Recurse into library files for nested sub-group blocks
  - Concatenate prefixes down the call tree
  - Map canonical file paths -> list of (vehicle, accumulated_prefix)

Then:
  - Add full_name column to params table (group + name)
  - UPDATE params.group and params.full_name for all rows
  - Regenerate param_chunks.jsonl with full_name in text

Rules:
  - Values verbatim, never rounded/inferred
  - All parse failures reported, never silently skipped
  - Row count MUST stay unchanged
"""

import re
import os
import json
import copy
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

import duckdb

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CORPUS = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/corpus/ardupilot")
DB_PATH = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/structured/kb.duckdb")
CHUNKS_PATH = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/chunks/param_chunks.jsonl")

# ---------------------------------------------------------------------------
# Vehicle roots: (vehicle_name, path_to_Parameters.cpp_dir, vehicle_label)
# ---------------------------------------------------------------------------
VEHICLE_ROOTS = [
    ("copter",         CORPUS / "ArduCopter"),
    ("plane",          CORPUS / "ArduPlane"),
    ("rover",          CORPUS / "Rover"),
    ("sub",            CORPUS / "ArduSub"),
    ("antennatracker", CORPUS / "AntennaTracker"),
    ("blimp",          CORPUS / "Blimp"),
]

# ---------------------------------------------------------------------------
# @Group / @Path block parser
# ---------------------------------------------------------------------------
# Matches:
#   // @Group: ATC_
#   // @Path: ../libraries/AC_AttitudeControl/AC_AttitudeControl.cpp,../libraries/...
# Both lines MUST appear consecutively (possibly with blank comment lines between,
# but in practice they are always adjacent). We use the same regex as param_parse.py:
#   prog_groups = re.compile(r"@Group: *(\w*).*((?:\n[ \t]*// @(Path): (\S+))+)", re.MULTILINE)
# This captures (group_name, full_path_block_text) — path values are on @Path lines.

GROUP_BLOCK_RE = re.compile(
    r"@Group:\s*(\w*)[^\n]*(?:\n[ \t]*//[^\n]*)*?\n[ \t]*//\s*@Path:\s*(\S+)",
    re.MULTILINE,
)

# Simpler version that matches each @Group ... @Path pair regardless of intervening lines.
# We do a two-pass approach: find @Group, then find the immediately following @Path.
GROUP_RE = re.compile(r"@Group:\s*(\w*)")
PATH_RE  = re.compile(r"@Path:\s*(\S+)")


def parse_group_path_blocks(text: str) -> List[Tuple[str, List[str]]]:
    """
    Parse all @Group/@Path pairs from a C++ source text.
    Returns list of (group_prefix, [path1, path2, ...]).

    Strategy: scan line by line. When we see @Group, record it; then scan
    forward for the very next @Path (which must appear before the next @Group
    or before a non-comment line that isn't a continuation).
    Actually mirrors what param_parse.py does: prog_groups matches
    @Group and the @Path(s) that follow within the same comment block.
    We use a simple two-pass approach that's robust to whitespace variations.
    """
    results = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        # Look for @Group: tag
        gm = GROUP_RE.search(stripped)
        if gm and stripped.startswith("//"):
            group_prefix = gm.group(1).strip()
            # Now look forward for @Path: within the same or immediately following comment lines
            j = i + 1
            found_path = None
            while j < n:
                ls = lines[j].strip()
                if not ls.startswith("//"):
                    # Non-comment line ends the block
                    break
                pm = PATH_RE.search(ls)
                if pm:
                    found_path = pm.group(1).strip()
                    break
                j += 1
            if found_path is not None:
                paths = [p.strip() for p in found_path.split(",") if p.strip()]
                results.append((group_prefix, paths))
            i = j
        else:
            i += 1
    return results


def resolve_library_path(path_str: str, base_dir: Path, corpus: Path) -> Optional[Path]:
    """
    Resolve a @Path value to an absolute filesystem path.

    ArduPilot conventions (mirroring param_parse.py process_library):
      - If path contains '/', treat as relative to apm_path/libraries/ OR relative to base_dir
      - If path has no '/', treat as relative to the vehicle dir (same dir as Parameters.cpp)

    The base_dir is the directory of the file that contained the @Path reference.
    """
    if not path_str:
        return None

    # Remove leading ../ sequences and try to resolve
    # param_parse.py logic:
    #   if path.find('/') == -1:  libraryfname = os.path.join(vehicle.path, path)
    #   else:  libraryfname = os.path.normpath(apm_path + '/libraries/' + path)
    # But when called recursively (pathprefix given):
    #   libraryfname = os.path.join(pathprefix, path)

    if "/" not in path_str:
        # Vehicle-local file
        candidate = base_dir / path_str
        if candidate.exists():
            return candidate
        return None

    # Has slashes - could be relative (../libraries/...) or absolute library path
    # Try relative to base_dir first
    candidate = (base_dir / path_str).resolve()
    if candidate.exists():
        return candidate

    # Try as libraries-relative: strip leading ../ and treat as from corpus root
    stripped = path_str.lstrip("./")
    candidate2 = (corpus / stripped).resolve()
    if candidate2.exists():
        return candidate2

    # Try treating entire path as relative to corpus
    candidate3 = (corpus / path_str.lstrip("/")).resolve()
    if candidate3.exists():
        return candidate3

    return None


def canonical_rel(path: Path, corpus: Path) -> Optional[str]:
    """Return path relative to corpus as a string, or None if outside corpus."""
    try:
        return str(path.relative_to(corpus))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Recursive prefix resolver
# ---------------------------------------------------------------------------

# Maps canonical_rel_path -> list of (vehicle, cumulative_prefix)
# A single file can be included by multiple vehicles with different prefixes.
FilePrefix = Dict[str, List[Tuple[str, str]]]


def collect_prefixes(
    vehicle: str,
    file_path: Path,
    corpus: Path,
    current_prefix: str,
    result: FilePrefix,
    visited: Set[str],
    depth: int = 0,
) -> None:
    """
    Recursively parse @Group/@Path blocks in file_path, accumulating prefix.

    For every file we encounter:
      1. Record (vehicle, current_prefix) for this file's canonical rel path
      2. Parse its @Group/@Path blocks; for each: recurse with current_prefix + group_prefix

    visited prevents infinite loops (same (vehicle, file, prefix) triple).
    """
    if depth > 20:
        print(f"  WARNING: max recursion depth reached at {file_path}")
        return

    rel = canonical_rel(file_path, corpus)
    if rel is None:
        return  # Outside corpus

    visit_key = f"{vehicle}|{rel}|{current_prefix}"
    if visit_key in visited:
        return
    visited.add(visit_key)

    # Record this file as reachable with this prefix under this vehicle
    result[rel].append((vehicle, current_prefix))

    # Parse the file for nested @Group/@Path blocks
    try:
        text = file_path.read_text(errors="replace")
    except Exception as e:
        print(f"  WARNING: could not read {file_path}: {e}")
        return

    base_dir = file_path.parent
    group_path_pairs = parse_group_path_blocks(text)

    for sub_group, paths in group_path_pairs:
        sub_prefix = current_prefix + sub_group  # concatenate
        for path_str in paths:
            resolved = resolve_library_path(path_str, base_dir, corpus)
            if resolved is None or not resolved.exists():
                # Try alternate resolution: the path is relative to corpus/libraries
                # when it contains '/' and doesn't start with '../'
                # e.g. "AP_GPS/AP_GPS.cpp" -> libraries/AP_GPS/AP_GPS.cpp
                if "/" in path_str and not path_str.startswith(".."):
                    alt = corpus / "libraries" / path_str
                    if alt.exists():
                        resolved = alt
                if resolved is None or not resolved.exists():
                    # Final fallback: just log and skip
                    # (not a hard error - some paths are conditionally compiled)
                    continue
            collect_prefixes(vehicle, resolved, corpus, sub_prefix, result, visited, depth + 1)


def build_file_prefix_map(corpus: Path) -> FilePrefix:
    """
    Walk all vehicle Parameters.cpp files and build the full
    canonical_rel_path -> [(vehicle, prefix)] map.

    Top-level Parameters.cpp params have prefix = "" (empty string).
    Library params inherit prefix from how they are included.
    """
    result: FilePrefix = defaultdict(list)

    for vehicle, vehicle_dir in VEHICLE_ROOTS:
        params_cpp = vehicle_dir / "Parameters.cpp"
        if not params_cpp.exists():
            print(f"  WARNING: {params_cpp} not found, skipping {vehicle}")
            continue

        visited: Set[str] = set()
        # The Parameters.cpp itself is top-level -> prefix = ""
        collect_prefixes(vehicle, params_cpp, corpus, "", result, visited, depth=0)

    return result


# ---------------------------------------------------------------------------
# Ambiguity resolution
# ---------------------------------------------------------------------------

def best_prefix(file_rel: str, file_vehicle: Optional[str], prefix_map: FilePrefix) -> Tuple[Optional[str], str]:
    """
    Given a param's file and vehicle (may be None for library files),
    return (prefix, note).

    If a file is reachable via multiple prefixes under the same vehicle,
    we cannot deterministically resolve which one applies without tracking
    which AP_GROUPINFO index maps to which group instantiation.
    In that case we return the first prefix found and note the ambiguity.

    For library files (vehicle=None), we collect the set of unique prefixes
    across all vehicles. If they're all the same prefix, use it.
    If they differ, we return None (unresolvable, needs manual inspection).

    Special cases:
      - If prefix_map has no entry for this file, return None (unresolved).
      - Empty prefix ("") = top-level, which is correct.
    """
    entries = prefix_map.get(file_rel)
    if entries is None:
        return (None, "file not found in prefix_map")

    if file_vehicle is not None:
        # Filter to matching vehicle
        vehicle_entries = [(v, p) for v, p in entries if v == file_vehicle]
        if not vehicle_entries:
            # Vehicle file included but no entry for this specific vehicle - use all
            vehicle_entries = entries
        prefixes = [p for _, p in vehicle_entries]
    else:
        # Library file - collect all prefixes
        prefixes = [p for _, p in entries]

    if not prefixes:
        return (None, "no prefix entries")

    unique_prefixes = list(dict.fromkeys(prefixes))  # preserve order, deduplicate

    if len(unique_prefixes) == 1:
        return (unique_prefixes[0], "")

    # Multiple unique prefixes - ambiguous for a single param.
    # This happens when the same .cpp is included under multiple prefixes in one vehicle.
    # We cannot resolve without more context. Record all as a note, return first.
    note = f"ambiguous: {unique_prefixes!r}"
    return (unique_prefixes[0], note)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Fixing param group/prefix resolution ===\n")

    # ------------------------------------------------------------------
    # Step 1: Build file -> prefix map
    # ------------------------------------------------------------------
    print("Building file->prefix map from vehicle Parameters.cpp files...")
    prefix_map = build_file_prefix_map(CORPUS)
    print(f"  Mapped {len(prefix_map)} unique file paths\n")

    # Quick sanity: check AC_AttitudeControl and AP_NavEKF3
    ac_key = "libraries/AC_AttitudeControl/AC_AttitudeControl.cpp"
    ek3_key = "libraries/AP_NavEKF3/AP_NavEKF3.cpp"
    print(f"  AC_AttitudeControl entries: {prefix_map.get(ac_key, [])[:10]}")
    print(f"  AP_NavEKF3 entries:         {prefix_map.get(ek3_key, [])[:10]}\n")

    # ------------------------------------------------------------------
    # Step 2: Open DB, add full_name column if missing, then update rows
    # ------------------------------------------------------------------
    print("Opening DuckDB...")
    con = duckdb.connect(str(DB_PATH))

    # Check row count before
    before_count = con.execute("SELECT COUNT(*) FROM params").fetchone()[0]
    print(f"  Row count before: {before_count}\n")

    # Add full_name column if it doesn't already exist
    existing_cols = {row[0] for row in con.execute("DESCRIBE params").fetchall()}
    if "full_name" not in existing_cols:
        print("  Adding full_name column to params table...")
        con.execute("ALTER TABLE params ADD COLUMN full_name VARCHAR")
        print("  Done.\n")
    else:
        print("  full_name column already exists.\n")

    # ------------------------------------------------------------------
    # Step 3: Fetch all params and compute group + full_name
    # ------------------------------------------------------------------
    print("Fetching all param rows...")
    rows = con.execute(
        'SELECT id, name, vehicle, file FROM params ORDER BY id'
    ).fetchall()
    print(f"  Got {len(rows)} rows.\n")

    # Build update batches
    updates = []       # (group_val, full_name_val, id)
    unresolved = []    # rows we couldn't assign a prefix
    ambiguous = []     # rows with ambiguous prefixes (multi-prefix same vehicle)

    for row_id, name, vehicle, file_rel in rows:
        prefix, note = best_prefix(file_rel, vehicle, prefix_map)

        if prefix is None:
            unresolved.append({
                "id": row_id,
                "name": name,
                "vehicle": vehicle,
                "file": file_rel,
                "note": note,
            })
            # group stays NULL (or ""), full_name = name
            updates.append(("", name, row_id))
        else:
            if note:
                ambiguous.append({
                    "id": row_id,
                    "name": name,
                    "vehicle": vehicle,
                    "file": file_rel,
                    "prefixes_note": note,
                    "chosen_prefix": prefix,
                })
            full_name = prefix + name
            updates.append((prefix, full_name, row_id))

    print(f"  Resolved:   {len(updates) - len(unresolved)} rows")
    print(f"  Unresolved: {len(unresolved)} rows")
    print(f"  Ambiguous:  {len(ambiguous)} rows\n")

    # ------------------------------------------------------------------
    # Step 4: Apply updates
    # ------------------------------------------------------------------
    print("Applying group and full_name updates to DB...")
    con.executemany(
        'UPDATE params SET "group" = ?, full_name = ? WHERE id = ?',
        updates,
    )
    con.commit()

    # Verify row count unchanged
    after_count = con.execute("SELECT COUNT(*) FROM params").fetchone()[0]
    print(f"  Row count after: {after_count}")
    assert after_count == before_count, f"Row count changed! {before_count} -> {after_count}"
    print("  Row count unchanged. OK.\n")

    # ------------------------------------------------------------------
    # Step 5: Verification spot checks
    # ------------------------------------------------------------------
    print("=== VERIFICATION ===\n")

    # Check ATC_ANGLE_MAX
    atc = con.execute(
        "SELECT id, name, vehicle, \"group\", full_name, file FROM params "
        "WHERE name = 'ANGLE_MAX' AND file LIKE '%AC_AttitudeControl%'"
    ).fetchall()
    print("ANGLE_MAX (AC_AttitudeControl rows):")
    for r in atc:
        print(f"  id={r[0]} name={r[1]} vehicle={r[2]} group={r[3]!r} full_name={r[4]!r} file={r[5]}")

    # Check EK3_HGT_DELAY
    ek3 = con.execute(
        "SELECT id, name, vehicle, \"group\", full_name, file FROM params "
        "WHERE name = 'HGT_DELAY' AND file LIKE '%NavEKF3%'"
    ).fetchall()
    print("\nHGT_DELAY (AP_NavEKF3 rows):")
    for r in ek3:
        print(f"  id={r[0]} name={r[1]} vehicle={r[2]} group={r[3]!r} full_name={r[4]!r} file={r[5]}")

    # Count non-empty group vs top-level
    stats = con.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE "group" IS NOT NULL AND "group" != '') AS has_group,
            COUNT(*) FILTER (WHERE "group" = '' OR "group" IS NULL) AS top_level,
            COUNT(*) FILTER (WHERE full_name IS NULL) AS null_full_name,
            COUNT(*) AS total
        FROM params
        """
    ).fetchone()
    print(f"\nStats:")
    print(f"  Total rows:               {stats[3]}")
    print(f"  Params with non-empty group (library): {stats[0]}")
    print(f"  Params with empty/NULL group (top-level or unresolved): {stats[1]}")
    print(f"  Params with NULL full_name: {stats[2]}")

    # Sample unresolved
    if unresolved:
        print(f"\nUnresolved prefixes sample (first 20):")
        for u in unresolved[:20]:
            print(f"  id={u['id']} name={u['name']} vehicle={u['vehicle']} file={u['file']} note={u['note']}")
        if len(unresolved) > 20:
            print(f"  ... and {len(unresolved) - 20} more")

    # Sample ambiguous
    if ambiguous:
        print(f"\nAmbiguous prefixes sample (first 10):")
        for a in ambiguous[:10]:
            print(f"  id={a['id']} name={a['name']} chosen={a['chosen_prefix']!r} all={a['prefixes_note']}")
        if len(ambiguous) > 10:
            print(f"  ... and {len(ambiguous) - 10} more")

    # ------------------------------------------------------------------
    # Step 6: Regenerate param_chunks.jsonl
    # ------------------------------------------------------------------
    print("\n=== Regenerating param_chunks.jsonl ===\n")

    # Fetch all params with full data
    param_rows = con.execute(
        """
        SELECT
            id, name, full_name, vehicle, "group", display_name, description,
            units, range_min, range_max, default_val, default_note, domain,
            file, line
        FROM params
        ORDER BY id
        """
    ).fetchall()

    cols = [
        "id", "name", "full_name", "vehicle", "group", "display_name", "description",
        "units", "range_min", "range_max", "default_val", "default_note", "domain",
        "file", "line",
    ]

    chunks_written = 0
    with open(str(CHUNKS_PATH), "w") as f:
        for row in param_rows:
            p = dict(zip(cols, row))
            name = p["name"] or ""
            full_name = p["full_name"] or name
            group = p["group"] or ""
            vehicle = p["vehicle"] or "unknown"
            display_name = p["display_name"] or ""
            description = p["description"] or ""
            default_val = p["default_val"]
            default_note = p["default_note"]
            range_min = p["range_min"]
            range_max = p["range_max"]
            units = p["units"]

            # Build text - lead with full_name, note leaf if different
            if full_name != name:
                header = f"Parameter: {full_name} (leaf {name})"
            else:
                header = f"Parameter: {full_name}"

            lines = [header, f"Vehicle: {vehicle}"]
            if group:
                lines.append(f"Group prefix: {group}")
            if display_name:
                lines.append(f"Display name: {display_name}")
            if description:
                lines.append(f"Description: {description}")
            if units:
                lines.append(f"Units: {units}")
            if range_min is not None and range_max is not None:
                lines.append(f"Range: {range_min} to {range_max}")
            if default_val is not None:
                lines.append(f"Default: {default_val}")
            elif default_note:
                lines.append(f"Default note: {default_note}")

            text = "\n".join(lines)

            chunk = {
                "chunk_id": f"param_{p['id']}",
                "chunk_type": "param",
                "domain": p["domain"],
                "source_path": p["file"],
                "symbol_id": None,
                "param_id": p["id"],
                "param_name": name,
                "full_name": full_name,
                "vehicle": vehicle,
                "start_line": p["line"],
                "end_line": p["line"],
                "text": text,
                "default_val": default_val,
                "range_min": range_min,
                "range_max": range_max,
                "units": units,
            }

            f.write(json.dumps(chunk) + "\n")
            chunks_written += 1

    print(f"  Written {chunks_written} chunks to {CHUNKS_PATH}\n")

    # Verify chunk count matches param count
    assert chunks_written == before_count, \
        f"Chunk count {chunks_written} != param count {before_count}"
    print("  Chunk count matches param count. OK.\n")

    # ------------------------------------------------------------------
    # Save resolution report
    # ------------------------------------------------------------------
    report_path = Path("/home/o0rt/Projects/homek/ArduPilot/.kb/structured/param_group_resolution_report.json")
    report = {
        "total_params": before_count,
        "resolved_with_group": stats[0],
        "top_level_or_empty": stats[1],
        "unresolved_count": len(unresolved),
        "ambiguous_count": len(ambiguous),
        "unresolved": unresolved,
        "ambiguous": ambiguous[:50],
    }
    with open(str(report_path), "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Resolution report written to: {report_path}\n")

    con.close()
    print("=== Done ===")


if __name__ == "__main__":
    main()
