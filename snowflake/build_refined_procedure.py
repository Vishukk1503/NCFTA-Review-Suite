"""Generate a self-contained procedure with the pinned catalog embedded."""
import hashlib
import json
from pathlib import Path
from build_refined_schema import CATALOG, CATALOG_SHA256, schema

HERE=Path(__file__).resolve().parent

def artifacts():
    data=CATALOG.read_bytes()
    if hashlib.sha256(data).hexdigest()!=CATALOG_SHA256:
        raise ValueError('Catalog checksum changed; review before generating.')
    contract='CATALOG_SHA256 = '+repr(CATALOG_SHA256)+'\nSCHEMA = '+repr(schema(json.loads(data)))+'\n'
    helpers=(HERE/'raw_loader.py').read_text(encoding='utf-8').split('\ndef run(session,')[0]
    handler=(HERE/'refined_loader.py').read_text(encoding='utf-8')
    handler='\n'.join(line for line in handler.splitlines() if not line.startswith(('from raw_loader import','from refined_contract import')))
    body=helpers+'\n'+contract+'\n'+handler
    assert '$$' not in body
    ddl='''-- REFINED transformation v1. Prepared locally, not server-validated.
-- Requires layers 1, 2, 3 and a successfully committed RAW batch.
-- Single writer; call outside a transaction. No model execution.
-- Reference: https://docs.snowflake.com/en/developer-guide/stored-procedure/python/procedure-python-examples
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
CREATE OR REPLACE PROCEDURE REFINED.SP_POPULATE_NCFTA_REFINED(
    SOURCE_BATCH_ID VARCHAR, LOAD_RUN_ID VARCHAR
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.12'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
EXECUTE AS CALLER
AS $$
'''+body+'\n$$;\n'
    return contract,ddl

if __name__=='__main__':
    contract,ddl=artifacts()
    (HERE/'refined_contract.py').write_text(contract,encoding='utf-8')
    (HERE/'06_populate_refined.sql').write_text(ddl,encoding='utf-8')
    print('Generated REFINED procedure and embedded 118-field contract; no database execution.')
