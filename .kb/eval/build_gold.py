#!/usr/bin/env python3
"""Build the gold eval set for the ArduPilot KB.

Every gold answer is QUERIED from .kb/structured/kb.duckdb (deterministic layer) so it can
be auto-graded with no human. The git SHA the answers are valid for is recorded in each row's
notes and at the top of this file.

gold.jsonl schema (per kb-eval skill):
  { id, question, domain, type, gold_answer, gold_support, notes }
  type in {param_fact, localization, relationship, concept}
  gold_support is a DuckDB query (exact-fact types) or file:line locator.
"""
import duckdb, json, os

DB = os.path.join(os.path.dirname(__file__), "..", "structured", "kb.duckdb")
con = duckdb.connect(os.path.abspath(DB), read_only=True)
SHA = con.execute("select value from build_info where key='git_sha'").fetchone()[0]

rows = []  # collected gold rows
HELDOUT = set()  # ids that go into the held-out split


def one(q, params=None):
    r = con.execute(q, params or []).fetchone()
    return r


def add(id, question, domain, typ, gold_answer, gold_support, notes="", heldout=False):
    rows.append({
        "id": id,
        "question": question,
        "domain": domain,
        "type": typ,
        "gold_answer": gold_answer,
        "gold_support": gold_support,
        "notes": (notes + (" " if notes else "") + f"git_sha={SHA}").strip(),
    })
    if heldout:
        HELDOUT.add(id)


# ---------------------------------------------------------------------------
# 1. PARAM_FACT  (default / range / units), drawn directly from params table
# ---------------------------------------------------------------------------
# Each entry: (id, human question, domain, sql, formatter, [heldout])
def pf_default_range_units(id, question, domain, name, vehsql, filescope=None, heldout=False):
    where = f"name='{name}'"
    if vehsql is not None:
        where += f" and vehicle='{vehsql}'"
    else:
        where += " and vehicle is null"
    if filescope:
        where += f" and file='{filescope}'"
    sql = (f"SELECT default_val, range_min, range_max, units FROM params WHERE {where}")
    d, rmin, rmax, u = one(sql)
    ans = f"default={d}, range={rmin}..{rmax}, units={u}"
    add(id, question, domain, "param_fact", ans, sql, heldout=heldout)


# control
pf_default_range_units("param-acro-roll-rate-plane",
    "For ArduPlane, what are the default value, range, and units of the ACRO_ROLL_RATE parameter?",
    "control", "ACRO_ROLL_RATE", "plane")
pf_default_range_units("param-gov-droop",
    "What are the default, range, and units of the GOV_DROOP (Governor Droop Compensator) parameter defined in AP_MotorsHeli_RSC.cpp?",
    "control", "GOV_DROOP", None, "libraries/AP_Motors/AP_MotorsHeli_RSC.cpp")
pf_default_range_units("param-angle-max-attctrl",
    "What are the range and units of the ANGLE_MAX parameter defined in AC_AttitudeControl.cpp? (note: it has no compiled-in default value)",
    "control", "ANGLE_MAX", None, "libraries/AC_AttitudeControl/AC_AttitudeControl.cpp")

# sensors
pf_default_range_units("param-tmax-tempcal",
    "What are the default, range, and units of the TMAX (temperature calibration max) parameter in AP_InertialSensor_tempcal.cpp?",
    "sensors", "TMAX", None, "libraries/AP_InertialSensor/AP_InertialSensor_tempcal.cpp")
pf_default_range_units("param-accoffs-z",
    "What are the default, range, and units of the ACCOFFS_Z accelerometer offset parameter?",
    "sensors", "ACCOFFS_Z", None, "libraries/AP_InertialSensor/AP_InertialSensor_Params.cpp")
pf_default_range_units("param-compass-dec",
    "What are the default, range, and units of the compass declination (DEC) parameter?",
    "sensors", "DEC", None, "libraries/AP_Compass/AP_Compass.cpp", heldout=True)
pf_default_range_units("param-rangefinder-pwrrng",
    "What are the default, range, and units of the PWRRNG (Powersave range) RangeFinder parameter?",
    "sensors", "PWRRNG", None, "libraries/AP_RangeFinder/AP_RangeFinder_Params.cpp")

# state_estimation (scoped to EKF3 file to disambiguate from EKF2)
pf_default_range_units("param-ek3-hgt-delay",
    "In AP_NavEKF3, what are the default, range, and units of the HGT_DELAY parameter?",
    "state_estimation", "HGT_DELAY", None, "libraries/AP_NavEKF3/AP_NavEKF3.cpp")
pf_default_range_units("param-ek3-mag-ef-lim",
    "In AP_NavEKF3, what are the default, range, and units of the MAG_EF_LIM parameter?",
    "state_estimation", "MAG_EF_LIM", None, "libraries/AP_NavEKF3/AP_NavEKF3.cpp", heldout=True)
pf_default_range_units("param-ahrs-trim-z",
    "What are the default, range, and units of the AHRS TRIM_Z (AHRS Trim Yaw) parameter?",
    "state_estimation", "TRIM_Z", None, "libraries/AP_AHRS/AP_AHRS.cpp")

# vehicle_copter
pf_default_range_units("param-copter-guid-timeout",
    "For ArduCopter, what are the default, range, and units of the GUID_TIMEOUT (Guided mode timeout) parameter?",
    "vehicle_copter", "GUID_TIMEOUT", "copter")
pf_default_range_units("param-copter-alt-low-m",
    "For ArduCopter, what are the default, range, and units of the ALT_LOW_M (Land alt low) parameter?",
    "vehicle_copter", "ALT_LOW_M", "copter", heldout=True)
pf_default_range_units("param-copter-fs-gcs-timeout",
    "For ArduCopter, what are the default, range, and units of the FS_GCS_TIMEOUT parameter?",
    "vehicle_copter", "FS_GCS_TIMEOUT", "copter")

# vehicle_plane
pf_default_range_units("param-plane-alt-slope-min",
    "For ArduPlane, what are the default, range, and units of the ALT_SLOPE_MIN parameter?",
    "vehicle_plane", "ALT_SLOPE_MIN", "plane")
pf_default_range_units("param-plane-alt-takeoff",
    "For ArduPlane, what are the default, range, and units of the ALT parameter defined in mode_takeoff.cpp?",
    "vehicle_plane", "ALT", "plane", "ArduPlane/mode_takeoff.cpp", heldout=True)

# vehicle_rover
pf_default_range_units("param-rover-cruise-throttle",
    "For Rover, what are the default, range, and units of the CRUISE_THROTTLE parameter?",
    "vehicle_rover", "CRUISE_THROTTLE", "rover")
pf_default_range_units("param-rover-sailboat-angle-max",
    "For Rover, what are the default, range, and units of the sailboat ANGLE_MAX parameter (sailboat.cpp)?",
    "vehicle_rover", "ANGLE_MAX", "rover", "Rover/sailboat.cpp")

# vehicle_sub
pf_default_range_units("param-sub-pilot-speed-dn",
    "For ArduSub, what are the default, range, and units of the PILOT_SPEED_DN parameter?",
    "vehicle_sub", "PILOT_SPEED_DN", "sub")
pf_default_range_units("param-sub-js-lights-steps",
    "For ArduSub, what are the default, range, and units of the JS_LIGHTS_STEPS parameter?",
    "vehicle_sub", "JS_LIGHTS_STEPS", "sub", heldout=True)

# antennatracker / blimp (vehicle coverage)
pf_default_range_units("param-tracker-mav-update-rate",
    "For AntennaTracker, what are the default, range, and units of the MAV_UPDATE_RATE parameter?",
    "vehicle_antennatracker", "MAV_UPDATE_RATE", "antennatracker")
pf_default_range_units("param-blimp-wp-radius",
    "For Blimp, what are the default, range, and units of the WP_RADIUS (Waypoint Radius) parameter?",
    "vehicle_blimp", "WP_RADIUS", "blimp")

# comms
pf_default_range_units("param-dronecan-ntf-rt",
    "What are the default, range, and units of the NTF_RT (Notify State rate) DroneCAN parameter?",
    "comms", "NTF_RT", None, "libraries/AP_DroneCAN/AP_DroneCAN.cpp")
pf_default_range_units("param-gcs-telem-delay",
    "What are the default, range, and units of the _TELEM_DELAY (Telemetry startup delay) GCS parameter?",
    "comms", "_TELEM_DELAY", None, "libraries/GCS_MAVLink/GCS.cpp", heldout=True)

# scripting (range only; has no units in table)
def pf_default_range_only(id, question, domain, name, filescope):
    sql = (f"SELECT default_val, range_min, range_max FROM params WHERE name='{name}' "
           f"and file='{filescope}'")
    d, rmin, rmax = one(sql)
    ans = f"default={d}, range={rmin}..{rmax}"
    add(id, question, domain, "param_fact", ans, sql)

pf_default_range_only("param-scripting-vm-i-count",
    "What are the default value and range of the AP_Scripting VM_I_COUNT parameter?",
    "scripting", "VM_I_COUNT", "libraries/AP_Scripting/AP_Scripting.cpp")

# infra_crosscutting (default-only style)
def pf_default_only(id, question, domain, name, filescope):
    sql = f"SELECT default_val, units FROM params WHERE name='{name}' and file='{filescope}'"
    d, u = one(sql)
    ans = f"default={d}, units={u}"
    add(id, question, domain, "param_fact", ans, sql)

pf_default_range_units("param-landing-abort-deg",
    "What are the default, range, and units of the AP_Landing ABORT_DEG parameter?",
    "infra_crosscutting", "ABORT_DEG", None, "libraries/AP_Landing/AP_Landing.cpp")
pf_default_range_units("param-tecs-appr-smax",
    "What are the default, range, and units of the AP_TECS APPR_SMAX parameter?",
    "infra_crosscutting", "APPR_SMAX", None, "libraries/AP_TECS/AP_TECS.cpp", heldout=True)


# ---------------------------------------------------------------------------
# 2. LOCALIZATION  (which file/line/class defines X)
# ---------------------------------------------------------------------------
def loc(id, question, domain, name, heldout=False):
    # canonical definition = class symbol with a real body (end_line>start_line)
    sql = (f"SELECT file, start_line FROM symbols WHERE name='{name}' AND kind='class' "
           f"AND end_line>start_line ORDER BY (end_line-start_line) DESC LIMIT 1")
    f, ln = one(sql)
    add(id, question, domain, "localization", f"{f}:{ln}", sql, heldout=heldout)


loc("loc-ac-poscontrol", "Which file defines the AC_PosControl class?", "control", "AC_PosControl")
loc("loc-ac-wpnav", "Which file defines the AC_WPNav class?", "control", "AC_WPNav")
loc("loc-ac-attitudecontrol", "Which file defines the AC_AttitudeControl base class?", "control", "AC_AttitudeControl", heldout=True)
loc("loc-ap-motorsmulticopter", "Which file defines the AP_MotorsMulticopter class?", "control", "AP_MotorsMulticopter")
loc("loc-ap-motorsheli", "Which file defines the AP_MotorsHeli class?", "control", "AP_MotorsHeli")
loc("loc-navekf3", "Which file defines the NavEKF3 class?", "state_estimation", "NavEKF3")
loc("loc-navekf2", "Which file defines the NavEKF2 class?", "state_estimation", "NavEKF2", heldout=True)
loc("loc-ap-inertialsensor", "Which file defines the AP_InertialSensor class?", "sensors", "AP_InertialSensor")
loc("loc-ap-baro", "Which file defines the AP_Baro class?", "sensors", "AP_Baro")
loc("loc-ap-param", "Which file defines the AP_Param class?", "infra_crosscutting", "AP_Param")
loc("loc-storagemanager", "Which file defines the StorageManager class?", "infra_crosscutting", "StorageManager")
loc("loc-ap-mission", "Which file defines the AP_Mission class?", "infra_crosscutting", "AP_Mission", heldout=True)
loc("loc-copter", "Which file defines the Copter vehicle class?", "vehicle_copter", "Copter")
loc("loc-plane", "Which file defines the Plane vehicle class?", "vehicle_plane", "Plane")
loc("loc-rover", "Which file defines the Rover vehicle class?", "vehicle_rover", "Rover")
loc("loc-sub", "Which file defines the Sub vehicle class?", "vehicle_sub", "Sub", heldout=True)
loc("loc-ap-tecs", "Which file defines the AP_TECS class?", "control", "AP_TECS")
loc("loc-ar-wpnav", "Which file defines the AR_WPNav (Rover waypoint navigation) class?", "control", "AR_WPNav")


def loc_q(id, question, domain, qname, heldout=False):
    # localization for a method/class with a unique qualified_name and real body
    sql = (f"SELECT file, start_line FROM symbols WHERE qualified_name='{qname}' "
           f"AND end_line>start_line ORDER BY (end_line-start_line) DESC LIMIT 1")
    f, ln = one(sql)
    add(id, question, domain, "localization", f"{f}:{ln}", sql, heldout=heldout)

loc_q("loc-modeauto-copter", "Which file defines the ArduCopter ModeAuto class?", "vehicle_copter",
      "ModeAuto")  # note disambig below

# ModeAuto is ambiguous across vehicles; scope to copter file explicitly
rows.pop()  # remove the imprecise one just added
sql = ("SELECT file, start_line FROM symbols WHERE name='ModeAuto' AND kind='class' "
       "AND file LIKE 'ArduCopter%' AND end_line>start_line LIMIT 1")
f, ln = one(sql)
add("loc-modeauto-copter", "Which file defines the ArduCopter ModeAuto flight-mode class?",
    "vehicle_copter", "localization", f"{f}:{ln}", sql)

# HAL localization (scoped, hal_boards)
def loc_hal(id, question, qname, heldout=False):
    sql = (f"SELECT file, start_line FROM symbols WHERE qualified_name='{qname}' "
           f"AND kind='class' AND end_line>start_line LIMIT 1")
    f, ln = one(sql)
    add(id, question, "hal_boards", "localization", f"{f}:{ln}", sql, heldout=heldout)

loc_hal("loc-hal-spidevice", "Which file defines the AP_HAL::SPIDevice abstract class?", "AP_HAL::SPIDevice")
loc_hal("loc-hal-linux-uart", "Which file defines the Linux::UARTDriver class?", "Linux::UARTDriver", heldout=True)
loc_hal("loc-hal-linux-scheduler", "Which file defines the Linux::Scheduler class?", "Linux::Scheduler")

# scripting localization
sql = ("SELECT file, start_line FROM symbols WHERE qualified_name='AP_Scripting_SerialAccess' "
       "AND kind='class' AND end_line>start_line LIMIT 1")
f, ln = one(sql)
add("loc-scripting-serialaccess", "Which file defines the AP_Scripting_SerialAccess class?",
    "scripting", "localization", f"{f}:{ln}", sql)


# ---------------------------------------------------------------------------
# 3. RELATIONSHIP  (subclass / caller / callee), from edges + helper views
# ---------------------------------------------------------------------------
# 3a. subclass: how many / which direct subclasses of a base class
def rel_subclasses(id, question, domain, base, heldout=False):
    sql = (f"SELECT subclass_name FROM subclasses_of WHERE base_name='{base}' "
           f"ORDER BY subclass_name")
    subs = [r[0] for r in con.execute(sql).fetchall()]
    ans = f"{len(subs)} subclasses: " + ", ".join(subs)
    add(id, question, domain, "relationship", ans, sql, heldout=heldout)

rel_subclasses("rel-ins-backend-subclasses",
    "Which classes are direct subclasses of AP_InertialSensor_Backend (i.e. the IMU driver backends)?",
    "sensors", "AP_InertialSensor_Backend")
rel_subclasses("rel-motorsmulti-subclasses",
    "Which classes directly inherit from AP_MotorsMulticopter?",
    "control", "AP_MotorsMulticopter")
rel_subclasses("rel-motorsheli-subclasses",
    "Which classes directly inherit from AP_MotorsHeli?",
    "control", "AP_MotorsHeli", heldout=True)
rel_subclasses("rel-hal-rcinput-subclasses",
    "Which classes directly inherit from AP_HAL::RCInput?",
    "hal_boards", "AP_HAL::RCInput")
rel_subclasses("rel-loggermsgwriter-subclasses",
    "Which classes directly inherit from LoggerMessageWriter?",
    "infra_crosscutting", "LoggerMessageWriter")

# 3b. is-a (single subclass -> base)
def rel_base_of(id, question, domain, subclass, heldout=False):
    sql = (f"SELECT base_name FROM subclasses_of WHERE subclass_name='{subclass}'")
    bases = sorted({r[0] for r in con.execute(sql).fetchall()})
    ans = ", ".join(bases)
    add(id, question, domain, "relationship", ans, sql, heldout=heldout)

rel_base_of("rel-bmi088-base",
    "What base class does AP_InertialSensor_BMI088 inherit from?",
    "sensors", "AP_InertialSensor_BMI088")
rel_base_of("rel-motorsmatrix-base",
    "What base class does AP_MotorsMatrix inherit from?",
    "control", "AP_MotorsMatrix", heldout=True)

# 3c. caller relationship: which symbols call a distinctive callee (scoped, small set)
def rel_callers(id, question, domain, callee, heldout=False):
    sql = (f"SELECT DISTINCT caller_name FROM callers_of WHERE callee_name='{callee}' "
           f"ORDER BY caller_name")
    callers = [r[0] for r in con.execute(sql).fetchall()]
    ans = f"{len(callers)} callers: " + ", ".join(callers)
    add(id, question, domain, "relationship", ans, sql, heldout=heldout)

rel_callers("rel-callers-have-aligned-yaw",
    "Which functions call NavEKF2_core::have_aligned_yaw?",
    "state_estimation", "NavEKF2_core::have_aligned_yaw")

# 3d. callee relationship: what a specific symbol id calls (deterministic by id)
def rel_callees_of_id(id, question, domain, sid, heldout=False):
    sql = (f"SELECT DISTINCT callee_name FROM callees_of WHERE caller_id={sid} "
           f"ORDER BY callee_name")
    callees = [r[0] for r in con.execute(sql).fetchall()]
    ans = f"{len(callees)} callees: " + ", ".join(callees)
    add(id, question, domain, "relationship", ans, sql, heldout=heldout)

# NavEKF3::UpdateFilter (id 15995) verified above
rel_callees_of_id("rel-callees-navekf3-updatefilter",
    "What functions/methods does NavEKF3::UpdateFilter (defined in AP_NavEKF3.cpp) call?",
    "state_estimation", 15995, heldout=True)
# mkdir_p (id 9840)
rel_callees_of_id("rel-callees-mkdir-p",
    "What functions does the Linux HAL Storage helper mkdir_p (Storage.cpp) call?",
    "hal_boards", 9840)


# ---------------------------------------------------------------------------
# 4. CONCEPT / MESSAGE  (MAVLink / DroneCAN message field facts, from messages)
#    These are concept-type per schema but auto-graded against the messages table.
# ---------------------------------------------------------------------------
def msg_id_fact(id, question, domain, name, heldout=False):
    sql = f"SELECT DISTINCT msg_id FROM messages WHERE name='{name}'"
    mid = one(sql)[0]
    add(id, question, domain, "concept", f"msg_id={mid}", sql, heldout=heldout)

def msg_field_type_units(id, question, domain, name, field, heldout=False):
    sql = (f"SELECT field_type, units FROM messages WHERE name='{name}' "
           f"AND field_name='{field}'")
    ft, u = one(sql)
    add(id, question, domain, "concept", f"field_type={ft}, units={u}", sql, heldout=heldout)

# MAVLink message IDs
msg_id_fact("msg-heartbeat-id", "What is the MAVLink message ID of HEARTBEAT?", "comms", "HEARTBEAT")
msg_id_fact("msg-attitude-id", "What is the MAVLink message ID of ATTITUDE?", "comms", "ATTITUDE")
msg_id_fact("msg-global-position-int-id", "What is the MAVLink message ID of GLOBAL_POSITION_INT?", "comms", "GLOBAL_POSITION_INT", heldout=True)
msg_id_fact("msg-gps-raw-int-id", "What is the MAVLink message ID of GPS_RAW_INT?", "comms", "GPS_RAW_INT")
msg_id_fact("msg-battery-status-id", "What is the MAVLink message ID of BATTERY_STATUS?", "comms", "BATTERY_STATUS")
msg_id_fact("msg-statustext-id", "What is the MAVLink message ID of STATUSTEXT?", "comms", "STATUSTEXT", heldout=True)

# MAVLink field type / units
msg_field_type_units("msg-attitude-roll", "In the MAVLink ATTITUDE message, what are the type and units of the 'roll' field?", "comms", "ATTITUDE", "roll")
msg_field_type_units("msg-global-pos-lat", "In the MAVLink GLOBAL_POSITION_INT message, what are the type and units of the 'lat' field?", "comms", "GLOBAL_POSITION_INT", "lat")
msg_field_type_units("msg-global-pos-vx", "In the MAVLink GLOBAL_POSITION_INT message, what are the type and units of the 'vx' field?", "comms", "GLOBAL_POSITION_INT", "vx", heldout=True)
msg_field_type_units("msg-vfr-hud-airspeed", "In the MAVLink VFR_HUD message, what are the type and units of the 'airspeed' field?", "comms", "VFR_HUD", "airspeed")
msg_field_type_units("msg-battery-temperature", "In the MAVLink BATTERY_STATUS message, what are the type and units of the 'temperature' field?", "comms", "BATTERY_STATUS", "temperature")
msg_field_type_units("msg-gps-raw-vel", "In the MAVLink GPS_RAW_INT message, what are the type and units of the 'vel' field?", "comms", "GPS_RAW_INT", "vel", heldout=True)

# DroneCAN field facts
msg_field_type_units("msg-dronecan-batteryinfo-voltage",
    "In the DroneCAN uavcan.equipment.power.BatteryInfo message, what is the type of the 'voltage' field?",
    "comms", "uavcan.equipment.power.BatteryInfo", "voltage")
msg_field_type_units("msg-dronecan-batteryinfo-soc",
    "In the DroneCAN uavcan.equipment.power.BatteryInfo message, what is the type of the 'state_of_charge_pct' field?",
    "comms", "uavcan.equipment.power.BatteryInfo", "state_of_charge_pct", heldout=True)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------
out = os.path.join(os.path.dirname(__file__), "gold.jsonl")
heldout_out = os.path.join(os.path.dirname(__file__), "gold_heldout.jsonl")

# sanity: unique ids, non-empty answers
ids = [r["id"] for r in rows]
assert len(ids) == len(set(ids)), "duplicate ids: " + str([i for i in ids if ids.count(i) > 1])
for r in rows:
    assert r["gold_answer"] and "None" not in r["gold_answer"].split("=")[-1] or "None" in r["gold_answer"], r
    assert r["gold_support"], r

main = [r for r in rows if r["id"] not in HELDOUT]
held = [r for r in rows if r["id"] in HELDOUT]

with open(out, "w") as fh:
    for r in main:
        fh.write(json.dumps(r) + "\n")
with open(heldout_out, "w") as fh:
    for r in held:
        fh.write(json.dumps(r) + "\n")

# report
import collections
def report(label, items):
    print(f"\n== {label}: {len(items)} ==")
    bd = collections.Counter(r["domain"] for r in items)
    bt = collections.Counter(r["type"] for r in items)
    print("  by domain:", dict(sorted(bd.items())))
    print("  by type  :", dict(sorted(bt.items())))

print("git_sha =", SHA)
report("ALL", rows)
report("MAIN (gold.jsonl)", main)
report("HELD-OUT (gold_heldout.jsonl)", held)
print("\nwrote", out)
print("wrote", heldout_out)
