-- Read-only definition inspection. Does not publish or expose source data.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
DESCRIBE PROCEDURE REPORTING.SP_POPULATE_NCFTA_APP(VARCHAR, VARCHAR);
-- Expected: Python 3.12, handler run, caller rights and pinned app-v1 mapping.
-- After successful RAW and REFINED processing, replace placeholders to call:
-- CALL REPORTING.SP_POPULATE_NCFTA_APP('<source_batch_id>', '<load_run_id>');
-- Inspect returned status: SUCCESS, NO_OP or FAILED; CALL completion is not enough.
-- SQL compilation, permissions and actual data-path behavior require live validation.
