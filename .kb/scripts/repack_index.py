#!/usr/bin/env python3
"""
Repack per-domain LanceDB tables into small fragments so no committed file is large.

phase3_build.py creates each domain table as ONE fragment; a big domain (infra_crosscutting,
21k chunks) becomes a single ~95 MB .lance file that trips GitHub's 50 MB warning / 100 MB limit.
This rewrites every domain_* table with max_rows_per_file=6000 (~25 MB/fragment) and deletes the
old versions. Vectors are unchanged, so retrieval is identical (verify with serve_eval.py).

Run after phase3_build.py. Requires pylance (`pip install pylance`).
"""
import sys, glob, os
from datetime import timedelta
from pathlib import Path
KB = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB / 'venv' / 'lib' / 'python3.14' / 'site-packages'))
import lance, lancedb

MAX_ROWS_PER_FILE = 6000  # ~25 MB per fragment at 1024-dim float32


def main():
    db = lancedb.connect(str(KB / 'index' / 'lancedb'))
    for name in sorted(n for n in db.table_names() if n.startswith('domain_')):
        path = str(KB / 'index' / 'lancedb' / f'{name}.lance')
        tbl = lance.dataset(path).to_table()
        lance.write_dataset(tbl, path, mode='overwrite', max_rows_per_file=MAX_ROWS_PER_FILE)
        lance.dataset(path).cleanup_old_versions(older_than=timedelta(0), delete_unverified=True)
        print(f"  repacked {name}: {tbl.num_rows} rows")
    biggest = max(glob.glob(str(KB / 'index/lancedb/**/*.lance'), recursive=True),
                  key=os.path.getsize)
    print(f"largest fragment now: {os.path.getsize(biggest) / 1048576:.1f} MB")


if __name__ == '__main__':
    main()
