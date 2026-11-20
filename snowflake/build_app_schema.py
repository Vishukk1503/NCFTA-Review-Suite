"""Generate app-table DDL from the preserved app mapping; no database access."""
import json
from pathlib import Path
from build_refined_schema import TECHNICAL, col, literal, verification

HERE = Path(__file__).resolve().parent
MAPPING = HERE / 'app-fields-v2.json'
NAMES = ['ADDRESS', 'REPORTED_AMOUNT', 'BANK_NAME', 'BANK_ADDRESS',
         'CRIME_DATETIME', 'SUBMITTED_BY', 'SWIFT_CODE', 'COMMENTS',
         'BANK_ACCOUNT_NUMBER', 'ROUTING_NUMBER', 'REPORTED_ACTOR_NAME',
         'PHONE_NUMBER', 'SOURCE_IP', 'EMAIL_ADDRESS', 'DATE_OF_BIRTH',
         'URL', 'MERCHANT_CATEGORY', 'COMPANY_NAME', 'ALIAS_NAME', 'AGE',
         'PLACE_OF_BIRTH', 'OCCUPATION', 'EMPLOYER_NAME', 'TARGET_ORGANIZATION',
         'MERCHANT_NAME', 'PREVIOUS_ADDRESS', 'ACCOUNT_TYPE', 'TOTAL_AMOUNT',
         'REPORTED_CURRENCY']


def schema():
    fields = json.loads(MAPPING.read_text(encoding='utf-8-sig'))['fields']
    if len(fields) != 29 or [f['number'] for f in fields] != list(range(1, 30)):
        raise ValueError('Expected the approved 29 ordered app fields.')
    excluded = {'ACCEPTED_SPAN_COUNT', 'REJECTED_SPAN_COUNT', 'MULTIVALUE_FIELDS',
                'CREATED_BY_LOAD_RUN_ID', 'CURATED_AT'}
    technical = [c for c in TECHNICAL if c['name'] not in excluded]
    technical += [
        col('APP_ROW_ID', 'VARCHAR(36)', True, comment='Stable publication identity for curated row and mapping version'),
        col('APP_MAPPING_VERSION', 'VARCHAR', True),
        col('FIELD_PROVENANCE', 'VARIANT', True, comment='Per field and array index: source key, value kind and evidence offsets; preserves URL versus domain and category text versus MCC'),
        col('PUBLISHED_BY_LOAD_RUN_ID', 'VARCHAR(36)', True),
        col('PUBLISHED_AT', 'TIMESTAMP_TZ', True, 'CURRENT_TIMESTAMP()', 'Publication time, not source version order'),
    ]
    business = [col(name, 'ARRAY', comment=f['label'] + '. Source: ' + f['source'] + '. ' + f['rule'])
                for name, f in zip(NAMES, fields)]
    columns = technical + business
    assert len({c['name'] for c in columns}) == len(columns)
    return columns


def render(columns):
    definitions = []
    for c in columns:
        line = '  ' + c['name'] + ' ' + c['data_type']
        if c['required']:
            line += ' NOT NULL'
        if c['default'] is not None:
            line += ' DEFAULT ' + c['default']
        if c['comment']:
            line += ' COMMENT ' + literal(c['comment'])
        definitions.append(line)
    return f'''-- NCFTA layer 4: application history. Prepared locally; not server-validated.
-- 29 business columns + {len(columns)-29} technical columns = {len(columns)} total.
-- Logical key: (CURATED_ROW_ID, APP_MAPPING_VERSION), enforced by future procedure.
-- One attribute observation/version per row. Never combine unrelated attributes.
-- Repeated observations remain traceable; no latest/current view is created here.
-- Arrays contain literal strings, not inferred person/account relationships.
-- FIELD_PROVENANCE records field + array index + source key/kind/offsets.
-- Deleted or unevaluated outcomes have NULL business arrays; retain their IDs/flags.
-- Evaluated fields without matching values use []; do not flatten arrays into joins.
-- Original source text and full JSON remain in RAW, linked by RESULT_ID.
-- No uploads, inserts, procedures, grants, destructive changes or model execution.
-- CREATE IF NOT EXISTS does not migrate existing definitions; run verification.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;

CREATE TABLE IF NOT EXISTS REPORTING.NCFTA_APP (
''' + ',\n'.join(definitions) + "\n) COMMENT = 'NCFTA app field history with attribute lineage and deletion evidence';\n"


def verify(columns):
    return (verification(columns).replace('layer-3', 'layer-4')
            .replace('REFINED.NCFTA_FIELDS', 'REPORTING.NCFTA_APP')
            .replace("TABLE_SCHEMA = 'REFINED' AND TABLE_NAME = 'NCFTA_FIELDS'",
                     "TABLE_SCHEMA = 'REPORTING' AND TABLE_NAME = 'NCFTA_APP'"))


if __name__ == '__main__':
    columns = schema()
    (HERE / '04_app.sql').write_text(render(columns), encoding='utf-8')
    (HERE / '04_verify_app.sql').write_text(verify(columns), encoding='utf-8')
    print(f'Generated app DDL and verification: 29 business + {len(columns)-29} technical columns.')
