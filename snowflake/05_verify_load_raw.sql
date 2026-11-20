-- Read-only inspection. This does not call the loader or load data.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
DESCRIBE PROCEDURE RAW.SP_LOAD_NCFTA_RAW(VARIANT, ARRAY, VARCHAR);
-- Verify Python 3.12, handler run, caller execution and expected source body.
-- Actual stage access, permissions, casts and transactional behavior still need
-- a synthetic integration run in the intended Snowflake account before real data.
