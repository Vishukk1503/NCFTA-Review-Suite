"""Embed the tested Python handler into the deployable Snowflake procedure."""
from pathlib import Path

HERE = Path(__file__).resolve().parent

def render():
    handler = (HERE / 'raw_loader.py').read_text(encoding='utf-8')
    assert '$$' not in handler
    return '''-- RAW loader v1. Prepared locally; requires Snowflake integration validation.
-- Run after 01_staging_and_raw.sql and 02_audit.sql, outside a transaction.
-- CALL only with a completed package created to the contract in README.md.
-- One writer at a time. Requires caller SELECT/INSERT on RAW, audit INSERT/UPDATE,
-- stage READ, file-format USAGE, schema/database USAGE and warehouse USAGE.
-- This replaces only the named procedure, not tables or data.
-- Reference: https://docs.snowflake.com/en/developer-guide/stored-procedure/python/procedure-python-examples
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
CREATE OR REPLACE PROCEDURE RAW.SP_LOAD_NCFTA_RAW(
    BATCH VARIANT, FILES ARRAY, LOAD_RUN_ID VARCHAR
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.12'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
''' + handler + '\n$$;\n'

if __name__ == '__main__':
    (HERE / '05_load_raw.sql').write_text(render(), encoding='utf-8')
    print('Generated 05_load_raw.sql; no database connection or execution.')
