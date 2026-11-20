# Snowflake JSON transport hotfix

1. Close the suite. Copy the contents of NCFTA-Snowflake-Hotfix.zip into your existing suite folder, replacing matching files. Keep .env, runs, models and your connection configuration.
2. In Snowflake, using the role that owns your NCFTA procedures, run snowflake/08_json_transport_hotfix.sql once. It adds four exact-JSON text columns and a text file format, and replaces the three loader procedures. It does not drop tables or delete records.
3. Start the suite, choose option 8 (Snowflake loader), and select the completed extraction. No model rerun is needed.

The loader shows Rich activity indicators during validation, browser sign-in, upload and each layer, followed by inserted/unchanged counts. Failure details remain in the local load report and audit table.

Why: Snowflake numeric normalization can change canonical JSON hashes. Batch arguments now travel as JSON text, staged JSONL is read as text, and RAW stores exact JSON alongside queryable VARIANT values. Hash and path checks remain enforced.

Validation: 108 local tests passed, including numeric normalization, retry NO_OP, downstream validation, tampered evidence rejection and Rich rendering. No live Snowflake execution was available. Previously successful legacy batches without exact-JSON columns may need a separately reviewed backfill; this patch will not silently overwrite their evidence.
