"""Embed pinned mapping, schemas and validation helpers into the app procedure."""
import hashlib
import json
from pathlib import Path
from build_app_schema import MAPPING, NAMES, schema
from build_refined_procedure import artifacts as refined_artifacts

HERE=Path(__file__).resolve().parent
MAPPING_SHA256='c1fe944df6a28bf3226435ab7f992d2db1da17c2cdb9a401c139b6843373a980'

def artifacts():
    data=MAPPING.read_bytes()
    if hashlib.sha256(data).hexdigest()!=MAPPING_SHA256:
        raise ValueError('Mapping checksum changed; review and version the mapping before regenerating.')
    fields=json.loads(data.decode('utf-8-sig'))['fields']
    direct={name:field['source'] for name,field in zip(NAMES,fields) if field['number'] not in (8,16,17)}
    contract=(f'MAPPING_SHA256 = {MAPPING_SHA256!r}\nAPP_SCHEMA = {schema()!r}\n'
              f'BUSINESS_NAMES = {NAMES!r}\nDIRECT_MAP = {direct!r}\n')
    refined_contract,_=refined_artifacts()
    helpers=(HERE/'raw_loader.py').read_text(encoding='utf-8').split('\ndef run(session,')[0]
    refined=(HERE/'refined_loader.py').read_text(encoding='utf-8').split('\ndef run(session,')[0]
    refined='\n'.join(line for line in refined.splitlines() if not line.startswith(('from raw_loader import','from refined_contract import')))
    app=(HERE/'app_loader.py').read_text(encoding='utf-8')
    app='\n'.join(line for line in app.splitlines() if not line.startswith(('from raw_loader import','from refined_loader import','from app_contract import')))
    body='\n'.join([helpers,refined_contract,refined,contract,app])
    assert '$$' not in body
    ddl='''-- App publisher v1. Prepared locally; not Snowflake-server validated.
-- Requires provisioned RAW/REFINED/REPORTING/audit tables and a complete refined-v1 batch.
-- Single writer, caller rights, outside a transaction. No model or network access.
-- Reference: https://docs.snowflake.com/en/developer-guide/stored-procedure/python/procedure-python-examples
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
CREATE OR REPLACE PROCEDURE REPORTING.SP_POPULATE_NCFTA_APP(
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
    (HERE/'app_contract.py').write_text(contract,encoding='utf-8')
    (HERE/'07_populate_app.sql').write_text(ddl,encoding='utf-8')
    print('Generated app procedure: 29 mapped business fields; no database execution.')
