# Revised Snowflake hotfix (v2)

1. Close the suite and copy the ZIP contents into the existing suite folder, replacing matching files. Keep .env, models, runs and connection settings.
2. Run all of snowflake/08_json_transport_hotfix.sql in Snowflake. This revision replaces only the three procedures. It does not ALTER TABLE, change grants, or create file formats.
3. Restart and choose option 8; select the completed extraction. No extraction rerun.

Both original RAW tables and tables partially upgraded by the first patch are accepted. Unused nullable companion columns can remain. Exact JSON is stored under reserved __ncfta_exact_json_v1 metadata inside existing VARIANT payload objects; original source keys remain queryable. Validation removes this metadata and reconstructs the exact original evidence. Source files are untouched. Collisions with that reserved key are rejected.

JSONL upload records are JSON strings containing canonical JSON, so the existing JSON file format preserves numeric representations. New _text filenames keep earlier staged files intact. Rich activity indicators and per-layer row counts remain included.

This eliminates the table-alter requirement. Actual database access denials cannot be fixed or bypassed by Python. Procedure replacement and normal table/stage access still must be allowed. No live Snowflake connection was available for testing; do not interpret local tests as a verified work-PC load.
