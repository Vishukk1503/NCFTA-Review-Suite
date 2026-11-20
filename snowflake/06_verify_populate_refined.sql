-- Read-only definition check; no transformation or source data access.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
DESCRIBE PROCEDURE REFINED.SP_POPULATE_NCFTA_REFINED(VARCHAR, VARCHAR);
-- Verify Python 3.12, caller rights, handler run and embedded catalog/schema.
-- Invocation, after a successful RAW load (replace placeholders; do not run as-is):
-- CALL REFINED.SP_POPULATE_NCFTA_REFINED('<source_batch_id>', '<load_run_id>');
-- Inspect the returned status: SUCCESS / NO_OP / FAILED.
-- The procedure does not upload files or fetch events.
