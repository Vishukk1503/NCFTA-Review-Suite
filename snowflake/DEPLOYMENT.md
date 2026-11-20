# NCFTA Snowflake setup

[Download all SQL scripts as a ZIP](NCFTA-Snowflake-Scripts.zip?raw=true)

These are **Snowflake SQL scripts**, run in a Snowflake SQL worksheet. They create
the storage tables, stage, JSON format and three stored procedures. They do not
fetch NCFTA data, run the model or upload local files.

Status: development/PoC. Local tests passed; these scripts have **not been executed
or validated in a live Snowflake account**. This upload does not update the Windows
suite release ZIP or install menu option 8 on your PC.

## 1. Open Snowflake

1. Download the ZIP and extract it.
2. Sign into Snowflake and open a SQL worksheet.
3. Select the intended account, an authorized role and an active warehouse.
4. Use database `COMPLIANCE_SCREENING_SANDBOX`.
5. The existing schemas must be present: `INTERNAL_STAGE`, `RAW`, `AUDIT_LAYER`,
   `REFINED` and `REPORTING`. These scripts do not create schemas or grant access.

The database name is embedded in the procedure handlers as well as the DDL.
Do not change only the worksheet database to deploy to a different database.

## 2. Run in this order

Open each creation file, paste its **entire contents** into the worksheet and run
the whole script. Then run its verification file. Do not execute individual lines
from inside a Python procedure's `AS $$ ... $$` body.

| Step | Run this script | What it creates | Then verify with |
| --- | --- | --- | --- |
| 1 | `01_staging_and_raw.sql` | JSON file format, internal stage, 3 RAW tables | `01_verify_staging_and_raw.sql` |
| 2 | `02_audit.sql` | One 40-column audit table | `02_verify_audit.sql` |
| 3 | `03_refined.sql` | REFINED table: 118 fields + 33 tracking columns | `03_verify_refined.sql` |
| 4 | `04_app.sql` | App table: 29 business fields + 33 tracking columns | `04_verify_app.sql` |
| 5 | `05_load_raw.sql` | `RAW.SP_LOAD_NCFTA_RAW` | `05_verify_load_raw.sql` |
| 6 | `06_populate_refined.sql` | `REFINED.SP_POPULATE_NCFTA_REFINED` | `06_verify_populate_refined.sql` |
| 7 | `07_populate_app.sql` | `REPORTING.SP_POPULATE_NCFTA_APP` | `07_verify_populate_app.sql` |

Stop at the first error and resolve it before continuing. Do not wrap deployment
in `BEGIN/COMMIT`; DDL commits independently.

## 3. What successful verification looks like

- **01:** DESCRIBE shows the stage/file format/tables, and the final SOURCE_VALUE
  check returns `PASS`. RAW column counts are 22, 14 and 32 respectively.
- **02:** DESCRIBE and the column listing show 40 audit columns.
- **03 and 04:** the final mismatch query returns **zero rows**. Review DESCRIBE
  for defaults. Expected totals are 151 REFINED and 62 app columns.
- **05–07:** DESCRIBE finds each procedure with Python 3.12, caller execution and
  handler `run`. This confirms its definition, not end-to-end execution.

`CREATE TABLE IF NOT EXISTS` preserves existing tables; it does not repair a wrong
definition. Layer 1 adds SOURCE_VALUE when missing, and layer 2 adds the documented
audit metadata columns when missing. Investigate other mismatches rather than
dropping tables. Existing `ALERT_*` objects are not modified. Steps 5–7 replace
only the three named NCFTA procedures.

## 4. Permissions and runtime

The deployment role needs USAGE on the database/schemas and privileges to create
the relevant stage, file format, tables and procedures. Existing objects may need
their owner to apply the additive changes or replace the procedures.

The runtime role needs warehouse/database/schema USAGE, READ/WRITE on the internal
stage for uploads, file-format USAGE, SELECT/INSERT on the NCFTA data tables and
SELECT/INSERT/UPDATE on the audit table. Procedures run with caller privileges;
they do not bypass access controls. Python 3.12 and `snowflake-snowpark-python`
must be available under the account's package policies.

## 5. After deployment

The tables should still be empty unless you already loaded data. Creating the
procedures does not call them.

The separate suite loader prepares a completed extraction run's saved JSON,
waits for browser SSO, uploads it and calls **RAW → REFINED → APP**. The published
Windows release has not been updated with that local loader yet; installing these
SQL scripts alone does not add the menu option. Do not manually call the RAW
procedure with Excel or arbitrary JSON: it expects the loader's validated package.

For the first live test, use one small completed run after installing the loader.
Check each returned status (`SUCCESS`, `NO_OP`, or `FAILED`), the audit row counts,
and the original text against the extracted values. A successful SQL CALL can still
return `FAILED`; inspect the returned object.

PoC constraints: one writer at a time, bounded batches, row-wise inserts and no
active/latest or cross-run delta view yet. Identical retries are skipped; different
observations retain history. Deletion flags do not physically delete saved rows.
If a request is interrupted, check the audit before retrying because server work
may still be running.

## Files in this download

14 SQL files (7 creation + 7 verification), this guide and `SHA256SUMS.txt`.
No credentials, source events, extracted records, model files or local settings
are included.
