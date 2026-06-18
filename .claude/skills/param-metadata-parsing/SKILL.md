---
name: param-metadata-parsing
description: How to parse ArduPilot parameter metadata exactly. ArduPilot documents parameters via structured // @Param comment blocks in C++ and a generated apm.pdef.xml / Parameters.rst. Use when populating the params table. Values must be copied verbatim.
---

# Parsing ArduPilot Parameter Metadata

ArduPilot parameters are documented in-source with structured comment blocks immediately above
the parameter definition, and are also published as generated artifacts. Prefer the generated
`apm.pdef.xml` when present (it is the project's own canonical export); fall back to parsing the
`// @...` comment blocks directly.

## Comment block fields (parse these tags)

Recognized tags include: `@Param`, `@DisplayName`, `@Description`, `@Units`, `@Range`,
`@Increment`, `@Values`, `@Bitmask`, `@User`, `@RebootRequired`, `@ReadOnly`.

Example shape (do not assume spacing; tokenize by tag):

```
// @Param: ANGLE_MAX
// @DisplayName: Angle Max
// @Description: Maximum lean angle in all flight modes
// @Units: cdeg
// @Range: 1000 8000
// @User: Advanced
```

## Rules

1. `@Range: <min> <max>` -> range_min, range_max as numbers, verbatim. Never round.
2. `@Values: 0:Disabled,1:Enabled,...` -> store the full mapping; do not collapse it.
3. `@Bitmask` is distinct from `@Values`; keep separately.
4. The parameter's literal default comes from the `AP_GROUPINFO`/definition in source, not the
   comment. If the default is computed at runtime (not a literal), set default=NULL + a note.
5. Vehicle scoping matters: the same short name can differ per vehicle. Always store `vehicle`
   and the fully-qualified group prefix.
6. Record `file` and `line` of the definition so the param links into the symbol graph.

## Validation

- A param missing `@Range` is fine; a param whose stored range/default disagrees with source is
  a bug — fail the parse loudly rather than emitting a wrong number.
