---
name: ardupilot-domains
description: The canonical domain taxonomy for the ArduPilot knowledge base, mapping source directories to knowledge domains. Use whenever code, params, docs, or queries must be assigned to a domain, or when deciding which per-domain index to build or search.
---

# ArduPilot Domain Taxonomy

Domains map to ArduPilot's real architecture, not invented categories. ArduPilot has 6 vehicle
types (Copter, Plane, Rover, Sub, Blimp, AntennaTracker) over shared libraries, with a HAL that
abstracts boards. Use this exact mapping when tagging symbols/chunks and routing queries.

| Domain | Primary dirs (under corpus/ardupilot) |
|---|---|
| hal_boards | `libraries/AP_HAL`, `libraries/AP_HAL_ChibiOS`, `libraries/AP_HAL_Linux`, `libraries/AP_HAL_ESP32` |
| sensors | `libraries/AP_InertialSensor`, `AP_GPS`, `AP_Compass`, `AP_Baro`, `AP_RangeFinder`, `AP_Airspeed` |
| state_estimation | `libraries/AP_NavEKF`, `AP_NavEKF2`, `AP_NavEKF3`, `AP_AHRS` |
| control | `libraries/AC_AttitudeControl`, `AC_PosControl`, `AC_WPNav`, `AP_Motors`, `APM_Control` |
| vehicle_copter | `ArduCopter/` |
| vehicle_plane | `ArduPlane/` |
| vehicle_rover | `Rover/` |
| vehicle_sub | `ArduSub/` |
| comms | `libraries/GCS_MAVLink`, `AP_DroneCAN`, `modules/mavlink` |
| scripting | `libraries/AP_Scripting` (Lua bindings) |
| infra_crosscutting | `libraries/AP_Param`, `AP_Scheduler`, `AP_Logger`, `StorageManager` |

## Cross-cutting rule

`infra_crosscutting` (params, scheduler, logger, storage) and `hal_boards` leak into every other
domain by design. Treat them as a shared index that is always a candidate in routing, and rely on
the symbol/param graph — not the domain split — to answer cross-domain questions. A clean domain
partition is partly fiction; the graph is what makes cross-domain answers correct.

## Assignment procedure

1. Match a file's path prefix to the table above; first match wins.
2. A symbol inherits its file's domain unless it lives in a shared header pulled by many domains
   (e.g. `AP_Vehicle.h`), in which case tag it `infra_crosscutting`.
3. Wiki/RST docs: assign by topic to the same domains; a doc may carry up to 2 domains.
