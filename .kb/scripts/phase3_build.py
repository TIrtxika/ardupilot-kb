#!/usr/bin/env python3
"""
Phase 3 — Build per-domain LanceDB partitions and per-domain BM25 indices.

Strategy:
  - Re-use ALL embeddings already in the global LanceDB table (NO re-embedding).
  - Partition by normalised domain (canonical 11 domains from ardupilot-domains skill).
  - Chunks in 'other' domain are mapped to infra_crosscutting (safe fallback —
    they include many internal libraries that serve all domains).
  - lib_* chunks are mapped via prefix rules that mirror the ardupilot-domains taxonomy.
  - infra_crosscutting is also always added to every per-domain search (cross-cutting rule).
  - Write one LanceDB table per domain: e.g. 'domain_control', 'domain_comms', etc.
  - Write one BM25 pickle per domain under .kb/index/bm25/domain_<name>.pkl
"""

import re, sys, json, pickle, time
from pathlib import Path
from collections import defaultdict

KB_ROOT = Path('/home/o0rt/Projects/homek/ArduPilot/.kb')
sys.path.insert(0, str(KB_ROOT / 'venv' / 'lib' / 'python3.14' / 'site-packages'))

import lancedb
import pyarrow as pa

INDEX_DIR = KB_ROOT / 'index'
BM25_DIR  = INDEX_DIR / 'bm25'

# ──────────────────────────────────────────────────────────────────────────────
# Domain normalisation map
# Canonical domains per ardupilot-domains skill.
# lib_* prefixes that fall outside the skill's primary dirs get mapped to the
# closest canonical domain or to infra_crosscutting.
# ──────────────────────────────────────────────────────────────────────────────

CANONICAL_DOMAINS = {
    'hal_boards',
    'sensors',
    'state_estimation',
    'control',
    'vehicle_copter',
    'vehicle_plane',
    'vehicle_rover',
    'vehicle_sub',
    'vehicle_blimp',
    'vehicle_antennatracker',
    'comms',
    'scripting',
    'infra_crosscutting',
}

# Explicit lib_* -> canonical domain mapping
LIB_DOMAIN_MAP = {
    # Control-adjacent libs
    'lib_AC_AutoTune':       'control',
    'lib_AC_Autorotation':   'control',
    'lib_AC_InputManager':   'control',
    'lib_AC_PID':            'control',
    'lib_AC_PrecLand':       'control',
    'lib_AC_Fence':          'control',
    'lib_AP_L1_Control':     'control',
    'lib_AP_TECS':           'control',
    'lib_AR_Motors':         'control',
    'lib_AR_WPNav':          'control',
    'lib_PID':               'control',
    'lib_AP_Landing':        'control',
    'lib_AP_Soaring':        'control',
    'lib_AP_InertialNav':    'control',

    # Sensors-adjacent libs
    'lib_AP_ADC':            'sensors',
    'lib_AP_AccelCal':       'sensors',
    'lib_AP_ExternalAHRS':   'sensors',
    'lib_AP_Declination':    'sensors',
    'lib_AP_OpticalFlow':    'sensors',
    'lib_AP_GyroFFT':        'sensors',
    'lib_AP_RPM':            'sensors',
    'lib_AP_RCProtocol':     'sensors',
    'lib_AP_WheelEncoder':   'sensors',
    'lib_AP_LeakDetector':   'sensors',
    'lib_AP_Proximity':      'sensors',
    'lib_AP_Beacon':         'sensors',
    'lib_AP_ADSB':           'sensors',
    'lib_AP_Airspeed':       'sensors',
    'lib_AP_SurfaceDistance': 'sensors',
    'lib_AP_TemperatureSensor': 'sensors',
    'lib_AP_EFI':            'sensors',
    'lib_AP_GSOF':           'sensors',
    'lib_AP_RCTelemetry':    'sensors',
    'lib_AP_Radio':          'sensors',
    'lib_Filter':            'sensors',

    # Comms-adjacent libs
    'lib_AP_DDS':            'comms',
    'lib_AP_Frsky_Telem':    'comms',
    'lib_AP_NMEA_Output':    'comms',
    'lib_AP_CANManager':     'comms',
    'lib_AP_Torqeedo':       'comms',
    'lib_AP_ONVIF':          'comms',

    # Infra/cross-cutting
    'lib_AP_Param':          'infra_crosscutting',
    'lib_AP_Logger':         'infra_crosscutting',
    'lib_AP_Scheduler':      'infra_crosscutting',
    'lib_StorageManager':    'infra_crosscutting',
    'lib_AP_Stats':          'infra_crosscutting',
    'lib_AP_RTC':            'infra_crosscutting',
    'lib_AP_BoardConfig':    'infra_crosscutting',
    'lib_AP_Common':         'infra_crosscutting',
    'lib_AP_Math':           'infra_crosscutting',
    'lib_AP_FlashStorage':   'infra_crosscutting',
    'lib_AP_FlashIface':     'infra_crosscutting',
    'lib_AP_RAMTRON':        'infra_crosscutting',
    'lib_AP_MultiHeap':      'infra_crosscutting',
    'lib_AP_CheckFirmware':  'infra_crosscutting',
    'lib_AP_ROMFS':          'infra_crosscutting',
    'lib_AP_JSON':           'infra_crosscutting',
    'lib_AP_CSVReader':      'infra_crosscutting',
    'lib_AP_Filesystem':     'infra_crosscutting',
    'lib_AP_Menu':           'infra_crosscutting',
    'lib_AP_Notify':         'infra_crosscutting',
    'lib_AP_Mission':        'infra_crosscutting',
    'lib_AP_SmartRTL':       'infra_crosscutting',
    'lib_AP_DAL':            'infra_crosscutting',
    'lib_AP_Vehicle':        'infra_crosscutting',
    'lib_AP_Arming':         'infra_crosscutting',
    'lib_AP_Navigation':     'infra_crosscutting',
    'lib_AP_OLC':            'infra_crosscutting',
    'lib_SRV_Channel':       'infra_crosscutting',
    'lib_RC_Channel':        'infra_crosscutting',
    'lib_AP_Mount':          'infra_crosscutting',
    'lib_AP_Camera':         'infra_crosscutting',
    'lib_AP_Relay':          'infra_crosscutting',
    'lib_AP_BattMonitor':    'infra_crosscutting',
    'lib_AP_Parachute':      'infra_crosscutting',
    'lib_AP_JSButton':       'infra_crosscutting',
    'lib_AP_Module':         'infra_crosscutting',
    'lib_AP_IOMCU':          'hal_boards',
    'lib_AP_OSD':            'infra_crosscutting',
    'lib_AP_Networking':     'infra_crosscutting',
    'lib_AP_DAC':            'hal_boards',

    # SITL is infrastructure
    'lib_SITL':              'infra_crosscutting',
}


def normalize_domain(raw: str) -> str:
    """Map raw domain label to one of the 13 canonical domains."""
    if raw in CANONICAL_DOMAINS:
        return raw
    if raw in LIB_DOMAIN_MAP:
        return LIB_DOMAIN_MAP[raw]
    # 'other' and anything else -> infra_crosscutting (safe catch-all)
    return 'infra_crosscutting'


def main():
    t0 = time.time()
    print("=" * 64)
    print("Phase 3 — Building per-domain indices (partition, no re-embed)")
    print("=" * 64)

    # ── 1. Load global LanceDB table ──────────────────────────────────────────
    db = lancedb.connect(str(INDEX_DIR / 'lancedb'))
    global_tbl = db.open_table('chunks')
    print(f"Global table: {global_tbl.count_rows()} rows")

    # Load entire table as Arrow batch (all data is already local)
    print("Loading Arrow data from global table…")
    arrow_tbl = global_tbl.to_arrow()
    total_rows = len(arrow_tbl)
    print(f"Loaded {total_rows} rows")

    # ── 2. Build normalized domain column ─────────────────────────────────────
    raw_domains = arrow_tbl.column('domain').to_pylist()
    norm_domains = [normalize_domain(d) for d in raw_domains]

    # Count distribution
    dist = defaultdict(int)
    for d in norm_domains:
        dist[d] += 1
    print("\nNormalised domain distribution:")
    for d, c in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {d}: {c:,}")

    # ── 3. Partition into per-domain LanceDB tables ───────────────────────────
    print("\nBuilding per-domain LanceDB partitions…")

    # Build lookup: norm_domain -> list of row indices
    domain_indices = defaultdict(list)
    for i, nd in enumerate(norm_domains):
        domain_indices[nd].append(i)

    created_tables = {}
    for domain, indices in sorted(domain_indices.items()):
        table_name = f"domain_{domain}"
        print(f"  {table_name}: {len(indices):,} rows", end='', flush=True)

        # Slice arrow table
        import pyarrow.compute as pc
        mask = pa.array([False] * total_rows)
        # Build boolean mask
        mask_list = [False] * total_rows
        for i in indices:
            mask_list[i] = True
        mask = pa.chunked_array([pa.array(mask_list)])
        domain_arrow = arrow_tbl.filter(mask)

        # Drop existing domain table if it exists, then create
        try:
            db.drop_table(table_name)
        except Exception:
            pass

        db.create_table(table_name, data=domain_arrow)
        created_tables[domain] = {'table': table_name, 'row_count': len(indices)}
        print(f" → created")

    # ── 4. Build per-domain BM25 indices ──────────────────────────────────────
    print("\nBuilding per-domain BM25 indices…")

    # Load global BM25 to get the corpus texts and chunk_ids
    global_bm25_path = BM25_DIR / 'bm25.pkl'
    print(f"  Loading global BM25 from {global_bm25_path}…")
    with open(global_bm25_path, 'rb') as f:
        global_bm25_data = pickle.load(f)

    global_chunk_ids = global_bm25_data['chunk_ids']

    # Build chunk_id -> index mapping
    cid_to_idx = {cid: i for i, cid in enumerate(global_chunk_ids)}

    # We need the tokenised corpus. Extract it from the BM25 object.
    # rank_bm25.BM25Okapi stores: corpus_size, avgdl, doc_freqs, idf, doc_len, corpus
    # We can rebuild per-domain BM25 from the tokenised corpus.
    bm25_obj = global_bm25_data['bm25']

    # Get the tokenised corpus from the BM25 object
    if hasattr(bm25_obj, 'corpus'):
        tokenised_corpus = bm25_obj.corpus
        print(f"  Corpus available in BM25 object: {len(tokenised_corpus)} docs")
    else:
        # We need to rebuild from the raw JSONL chunks
        print("  BM25 corpus not in object, rebuilding from JSONL chunks…")
        tokenised_corpus = None

    from rank_bm25 import BM25Okapi

    # Build mapping: chunk_id -> norm_domain
    all_chunk_ids_set = set(global_chunk_ids)
    # From LanceDB arrow
    lance_cids = arrow_tbl.column('chunk_id').to_pylist()
    cid_to_domain = {cid: norm_domains[i] for i, cid in enumerate(lance_cids)}

    if tokenised_corpus is not None:
        # We have the tokenised corpus; build per-domain BM25 from slices
        for domain in sorted(domain_indices.keys()):
            # Get chunk_ids for this domain
            domain_cids = [lance_cids[i] for i in domain_indices[domain]]
            # Find their indices in global_chunk_ids
            valid = [(cid, cid_to_idx[cid]) for cid in domain_cids if cid in cid_to_idx]
            if not valid:
                print(f"  [BM25] {domain}: no matching chunk_ids in BM25 corpus, skipping")
                continue

            d_cids = [v[0] for v in valid]
            d_corpus = [tokenised_corpus[v[1]] for v in valid]

            print(f"  [BM25] {domain}: {len(d_cids):,} docs", end='', flush=True)
            bm25_d = BM25Okapi(d_corpus)

            out_path = BM25_DIR / f"domain_{domain}.pkl"
            with open(out_path, 'wb') as f:
                pickle.dump({'bm25': bm25_d, 'chunk_ids': d_cids}, f)
            print(f" → {out_path.name}")
    else:
        # Rebuild from JSONL chunks
        print("  Rebuilding from JSONL chunks…")
        # Build chunk_id -> text mapping
        chunks_dir = KB_ROOT / 'chunks'
        cid_to_text = {}
        for fname in ['cpp_chunks.jsonl', 'param_chunks.jsonl', 'message_chunks.jsonl', 'rst_chunks.jsonl']:
            path = chunks_dir / fname
            if not path.exists():
                continue
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    cid_to_text[obj['chunk_id']] = obj.get('text', '')

        for domain in sorted(domain_indices.keys()):
            domain_cids = [lance_cids[i] for i in domain_indices[domain]]
            valid_cids = [cid for cid in domain_cids if cid in cid_to_text]
            if not valid_cids:
                print(f"  [BM25] {domain}: no text found, skipping")
                continue

            d_corpus = [re.findall(r'[a-z0-9]+', cid_to_text[cid].lower()) for cid in valid_cids]
            print(f"  [BM25] {domain}: {len(valid_cids):,} docs", end='', flush=True)
            bm25_d = BM25Okapi(d_corpus)

            out_path = BM25_DIR / f"domain_{domain}.pkl"
            with open(out_path, 'wb') as f:
                pickle.dump({'bm25': bm25_d, 'chunk_ids': valid_cids}, f)
            print(f" → {out_path.name}")

    # ── 5. Save normalised domain map for router ──────────────────────────────
    # Save chunk_id -> normalised domain for the router's lookup
    domain_map = {lance_cids[i]: norm_domains[i] for i in range(len(lance_cids))}
    out_domain_map = INDEX_DIR / 'chunk_domain_map.json'
    with open(out_domain_map, 'w') as f:
        json.dump(domain_map, f)
    print(f"\nChunk domain map saved: {out_domain_map} ({len(domain_map):,} entries)")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")

    return created_tables, dist


if __name__ == '__main__':
    tables, dist = main()
    print("\nSummary:")
    for d, info in sorted(tables.items()):
        print(f"  {info['table']}: {info['row_count']:,} rows")
