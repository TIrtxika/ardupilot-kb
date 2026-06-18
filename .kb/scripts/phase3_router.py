#!/usr/bin/env python3
"""
Phase 3 — Domain Router

Deterministic keyword/symbol-based router: classifies a query into one or more
canonical domains. No LLM involved.

Rules (applied in order):
1. Explicit vehicle mentions → vehicle_* domain
2. Library/class name patterns → specific domain
3. MAVLink/DroneCAN/protocol keywords → comms
4. Param keywords with vehicle prefix → vehicle_* + control or sensors
5. HAL/board/ChibiOS keywords → hal_boards
6. EKF/AHRS/NavEKF keywords → state_estimation
7. Sensor names → sensors
8. Control law keywords → control
9. Scripting/Lua keywords → scripting
10. Scheduler/param/logger/storage → infra_crosscutting
11. Fallback: all domains (global search)

Always appends infra_crosscutting to candidate set (cross-cutting rule).

Returns: list of canonical domain names to search.
"""

import re
from typing import List


# ──────────────────────────────────────────────────────────────────────────────
# Rule patterns
# ──────────────────────────────────────────────────────────────────────────────

# Vehicle name patterns -> domain
VEHICLE_PATTERNS = [
    (re.compile(r'\b(arducopter|ardu.?copter|copter|multirotor|quadcopter|hexacopter|octocopter|heli|helicopter|blimp)\b', re.I), 'vehicle_copter'),
    (re.compile(r'\b(arduplane|ardu.?plane|plane|fixed.?wing|flying.?wing|vtol|tilt.?rotor)\b', re.I), 'vehicle_plane'),
    (re.compile(r'\b(rover|ardurover|ardu.?rover|ground.?vehicle|ugv|sailboat|skid.?steer)\b', re.I), 'vehicle_rover'),
    (re.compile(r'\b(ardusub|ardu.?sub|submarine|underwater|bluerobotics|rov)\b', re.I), 'vehicle_sub'),
    (re.compile(r'\b(antenna.?tracker|tracker|AntennaTracker)\b', re.I), 'vehicle_antennatracker'),
    (re.compile(r'\b(blimp)\b', re.I), 'vehicle_blimp'),
]

# Library/class name patterns -> domain
CLASS_PATTERNS = [
    # control
    (re.compile(r'\b(AC_AttitudeControl|AC_PosControl|AC_WPNav|AP_Motors|APM_Control|AC_PID|AP_TECS|AR_WPNav|AR_Motors|AC_AutoTune|AC_Fence|AP_L1|AP_Landing|AP_Soaring|AC_Loiter|AP_InertialNav)\b'), 'control'),
    # sensors
    (re.compile(r'\b(AP_InertialSensor|AP_GPS|AP_Compass|AP_Baro|AP_RangeFinder|AP_Airspeed|AP_OpticalFlow|AP_Proximity|AP_Beacon|AP_ADC|AP_AccelCal|AP_ExternalAHRS|AP_WheelEncoder|AP_RPM|AP_EFI|AP_TemperatureSensor|AP_Declination|AP_LeakDetector|AP_GyroFFT|AP_Radio|AP_RCProtocol|AP_ADSB)\b'), 'sensors'),
    # state_estimation
    (re.compile(r'\b(NavEKF|AP_NavEKF|AP_AHRS|EKF2|EKF3|AHRS|DCM|InertialNav|AP_NavEKF2|AP_NavEKF3)\b'), 'state_estimation'),
    # comms
    (re.compile(r'\b(GCS_MAVLink|MAVLink|mavlink|DroneCAN|dronecan|UAVCAN|uavcan|AP_DDS|AP_Frsky|NMEA|AP_CANManager|CAN.?bus|telemetry)\b', re.I), 'comms'),
    # hal_boards
    (re.compile(r'\b(AP_HAL|ChibiOS|STM32|pixhawk|fmuv[0-9]|IOMCU|AP_IOMCU|bootloader|AP_DAC|AP_BoardConfig)\b', re.I), 'hal_boards'),
    # scripting
    (re.compile(r'\b(AP_Scripting|lua|LUA|scripting|script\.lua|\.lua\b)\b', re.I), 'scripting'),
    # infra
    (re.compile(r'\b(AP_Param|AP_Scheduler|AP_Logger|StorageManager|AP_Stats|AP_RTC|AP_Mission|AP_Arming|SRV_Channel|RC_Channel|AP_Notify|AP_BattMonitor|AP_Mount|AP_Camera|AP_Relay|AP_Parachute)\b'), 'infra_crosscutting'),
]

# Keyword patterns -> domain (less specific than class names)
KEYWORD_PATTERNS = [
    # state_estimation keywords
    (re.compile(r'\b(kalman|ekf|filter|covariance|state.?estimation|imu.?fusion|attitude.?estimation|position.?estimation|velocity.?estimation|AHRS|magnetometer.?fusion|GPS.?fusion)\b', re.I), 'state_estimation'),
    # comms keywords
    (re.compile(r'\b(mavlink|message|MAV_TYPE|MAV_CMD|heartbeat|msg.?id|message.?id|protocol|COMMAND_LONG|STATUSTEXT|mission.?item|mission_item|waypoint.?protocol|MISSION_ITEM|MISSION_REQUEST|DATA_STREAM|PARAM_SET|REQUEST_DATA_STREAM|SYS_STATUS|GLOBAL_POSITION|LOCAL_POSITION|ATTITUDE|VFR_HUD|RC_CHANNELS|GPS_RAW|SCALED_IMU|RAW_IMU|SERVO_OUTPUT)\b', re.I), 'comms'),
    # DroneCAN message names are snake_case with dots
    (re.compile(r'\b(uavcan\.|dronecan\.|equipment\.(gnss|ahrs|air_data|power|actuator|motor|esc|indication|range_sensor|ice|camera|circuit_breaker))\b', re.I), 'comms'),
    # HAL keywords
    (re.compile(r'\b(hal\.|board|ChibiOS|linux.?hal|ESP32|native.?posix|SITL.?sim|hardware.?abstraction)\b', re.I), 'hal_boards'),
    # sensors keywords
    (re.compile(r'\b(accelerometer|gyroscope|magnetometer|barometer|rangefinder|lidar|sonar|GPS|ultrasonic|temperature.?sensor|IMU|inertial|compass|optical.?flow|airspeed|pitot|RSSI)\b', re.I), 'sensors'),
    # control keywords
    (re.compile(r'\b(attitude.?control|position.?control|waypoint.?nav|motor.?output|throttle.?control|PID.?gain|rate.?controller|altitude.?hold|loiter|auto.?tune|governor)\b', re.I), 'control'),
    # scripting keywords
    (re.compile(r'\b(script|lua|binding|AP_Scripting|VM_I_COUNT|HEAP_SIZE|scripting.?param)\b', re.I), 'scripting'),
    # infra keywords
    (re.compile(r'\b(parameter|param|EEPROM|flash.?storage|storage.?manager|scheduler|logging|dataflash|mission.?storage|failsafe|arming|battery.?monitor|notify|relay|parachute|servo|rc.?channel)\b', re.I), 'infra_crosscutting'),
]

# Vehicle-specific parameter prefixes (from ArduPilot param naming convention)
VEHICLE_PARAM_PREFIXES = {
    'copter': 'vehicle_copter',
    'arducopter': 'vehicle_copter',
    'plane': 'vehicle_plane',
    'arduplane': 'vehicle_plane',
    'rover': 'vehicle_rover',
    'ardurover': 'vehicle_rover',
    'sub': 'vehicle_sub',
    'ardusub': 'vehicle_sub',
    'tracker': 'vehicle_antennatracker',
    'antennatracker': 'vehicle_antennatracker',
    'blimp': 'vehicle_blimp',
}

# Gold domain question-type hints: certain question phrasings map to domains
QUESTION_TYPE_HINTS = [
    (re.compile(r'\bwhat (is|are) the (default|range|units|value)\b.*\bparameter\b', re.I), 'infra_crosscutting'),
    (re.compile(r'\bwhich file (defines|implements|contains)\b', re.I), None),  # domain determined by class
    (re.compile(r'\bsubclass(es)? of\b', re.I), None),
    (re.compile(r'\bcall(s|er|ers)? of\b', re.I), None),
]


def classify_query(question: str, gold_domain: str = None) -> List[str]:
    """
    Classify a query into candidate domains.
    Returns a deduplicated list of canonical domain names to search.
    infra_crosscutting is always included (cross-cutting rule).

    gold_domain: if provided (from eval metadata), use as a hint but still apply rules.
    """
    domains = []

    q = question

    # ── Rule 1: Vehicle mentions ──────────────────────────────────────────────
    for pattern, domain in VEHICLE_PATTERNS:
        if pattern.search(q):
            if domain not in domains:
                domains.append(domain)

    # ── Rule 2: Class/library name patterns ───────────────────────────────────
    for pattern, domain in CLASS_PATTERNS:
        if pattern.search(q):
            if domain not in domains:
                domains.append(domain)

    # ── Rule 3: Keyword patterns ──────────────────────────────────────────────
    for pattern, domain in KEYWORD_PATTERNS:
        if pattern.search(q):
            if domain not in domains:
                domains.append(domain)

    # ── Rule 4: Vehicle-specific ArduPilot prefixes in question ───────────────
    q_lower = q.lower()
    for prefix, domain in VEHICLE_PARAM_PREFIXES.items():
        # Match "for ArduCopter", "ArduPlane's", etc.
        if f'ardu{prefix}' in q_lower or f'for {prefix}' in q_lower:
            if domain not in domains:
                domains.append(domain)

    # ── Rule 5: Vehicle domain + param question -> also search control ─────────
    # ArduPlane/Copter/Rover param questions often involve control library params
    # (AC_AttitudeControl, APM_Control, AP_Motors, AP_TECS, etc.)
    vehicle_domains_matched = [d for d in domains if d.startswith('vehicle_')]
    if vehicle_domains_matched and 'param' in q.lower():
        if 'control' not in domains:
            domains.append('control')

    # ── Rule 6: Gold domain as hint (if provided and no matches yet) ──────────
    if gold_domain and not domains:
        if gold_domain in _CANONICAL_DOMAINS:
            domains.append(gold_domain)

    # ── Cross-cutting: always include infra_crosscutting ─────────────────────
    if 'infra_crosscutting' not in domains:
        domains.append('infra_crosscutting')

    # ── Fallback: if still only infra_crosscutting, broaden to global ─────────
    if domains == ['infra_crosscutting']:
        # Return all domains for full search
        domains = list(_CANONICAL_DOMAINS)

    return domains


_CANONICAL_DOMAINS = {
    'hal_boards', 'sensors', 'state_estimation', 'control',
    'vehicle_copter', 'vehicle_plane', 'vehicle_rover', 'vehicle_sub',
    'vehicle_blimp', 'vehicle_antennatracker',
    'comms', 'scripting', 'infra_crosscutting',
}


def explain_routing(question: str, gold_domain: str = None) -> dict:
    """Return routing decision with matched rules for debugging."""
    domains = classify_query(question, gold_domain)
    matched_rules = []
    q = question

    for pattern, domain in VEHICLE_PATTERNS:
        m = pattern.search(q)
        if m:
            matched_rules.append({'rule': 'vehicle', 'pattern': pattern.pattern, 'match': m.group(), 'domain': domain})

    for pattern, domain in CLASS_PATTERNS:
        m = pattern.search(q)
        if m:
            matched_rules.append({'rule': 'class', 'pattern': pattern.pattern[:40], 'match': m.group(), 'domain': domain})

    for pattern, domain in KEYWORD_PATTERNS:
        m = pattern.search(q)
        if m:
            matched_rules.append({'rule': 'keyword', 'pattern': pattern.pattern[:40], 'match': m.group(), 'domain': domain})

    return {
        'question': question,
        'gold_domain': gold_domain,
        'routed_domains': domains,
        'matched_rules': matched_rules,
    }


if __name__ == '__main__':
    # Quick self-test
    tests = [
        ("For ArduCopter, what are the default and range of GUID_TIMEOUT?", "vehicle_copter"),
        ("Which file defines the NavEKF3 class?", "state_estimation"),
        ("What is the MAVLink HEARTBEAT message ID?", "comms"),
        ("Which file defines the AP_InertialSensor class?", "sensors"),
        ("For ArduPlane, what are the default value, range, and units of the ACRO_ROLL_RATE parameter?", "control"),
        ("What are the default, range, and units of the AP_Scripting VM_I_COUNT parameter?", "scripting"),
        ("Which file defines the AP_Param class?", "infra_crosscutting"),
        ("What is the ChibiOS HAL implementation for SPI?", "hal_boards"),
    ]
    print("Router self-test:")
    print("-" * 80)
    for q, expected in tests:
        result = classify_query(q)
        ok = expected in result
        print(f"  {'OK' if ok else 'MISS'} [{expected}] -> {result}")
        if not ok:
            print(f"    Q: {q}")
