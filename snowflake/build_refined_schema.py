"""Build layer-3 DDL from the pinned catalog. No network/database/data access."""
from pathlib import Path
import hashlib
import json
import re

HERE = Path(__file__).resolve().parent
CATALOG = HERE.parent / 'assets' / 'field-catalog.json'
CATALOG_SHA256 = '58bf1531ceb3c2957b34ae8ddf8c14c37ec076db2ad2e166878c1fb9c2244cda'
METADATA = {'observation_first_seen', 'observation_last_seen'}


def col(name, data_type, required=False, default=None, comment=''):
    return dict(name=name, data_type=data_type, required=required, default=default, comment=comment)


TECHNICAL = [
    col('CURATED_ROW_ID', 'VARCHAR(36)', True, comment='Stable loader UUID for RESULT_ID and TRANSFORM_VERSION'),
    col('RESULT_ID', 'VARCHAR(36)', True, comment='Links to RAW.NCFTA_MODEL_RESULTS'),
    col('SOURCE_BATCH_ID', 'VARCHAR(36)', True, comment='Links to RAW.NCFTA_BATCHES'),
    col('EVENT_SNAPSHOT_ID', 'VARCHAR(36)', True, comment='Links to RAW.NCFTA_EVENTS'),
    col('SOURCE_INSTANCE_ID', 'VARCHAR', True, comment='Source namespace from the batch; no credentials'),
    col('EVENT_UUID', 'VARCHAR(36)'),
    col('OBJECT_UUID', 'VARCHAR(36)'),
    col('ATTRIBUTE_UUID', 'VARCHAR(36)'),
    col('SOURCE_EVENT_ID', 'VARCHAR'),
    col('SOURCE_OBJECT_ID', 'VARCHAR'),
    col('SOURCE_ATTRIBUTE_ID', 'VARCHAR'),
    col('SOURCE_REVIEW_ID', 'VARCHAR', True, comment='Saved internal join key, not a source UUID'),
    col('EVENT_SHARING_GROUP_ID', 'VARCHAR'),
    col('ATTRIBUTE_SHARING_GROUP_ID', 'VARCHAR'),
    col('ATTRIBUTE_DISTRIBUTION', 'VARCHAR'),
    col('SOURCE_TYPE', 'VARCHAR'),
    col('SOURCE_CATEGORY', 'VARCHAR'),
    col('SOURCE_ATTRIBUTE_SHA256', 'VARCHAR(64)', True, comment='Source attribute hash copied from RAW'),
    col('MODEL_SHA256', 'VARCHAR(64)', True),
    col('CATALOG_SHA256', 'VARCHAR(64)', True),
    col('PREDICTION_IDENTITY', 'VARCHAR(64)', True, comment='Saved inference identity; not a source version timestamp'),
    col('TRANSFORM_VERSION', 'VARCHAR', True),
    col('OUTCOME_STATUS', 'VARCHAR', True, comment='predicted, deleted, invalid_source, empty_value or input_error'),
    col('SOURCE_ATTRIBUTE_DELETED', 'BOOLEAN', comment='Original attribute deletion signal; absent stays NULL'),
    col('SOURCE_OBJECT_DELETED', 'BOOLEAN', comment='Original parent object deletion signal; absent stays NULL'),
    col('IS_DELETED', 'BOOLEAN', True, comment='True when either source signal is true; absence is not a deletion'),
    col('REVIEW_STATUS', 'VARCHAR', True, "'NOT_REVIEWED'", 'No automatic analyst approval'),
    col('ACCEPTED_SPAN_COUNT', 'NUMBER(38,0)', True),
    col('REJECTED_SPAN_COUNT', 'NUMBER(38,0)', True),
    col('MULTIVALUE_FIELDS', 'ARRAY', True, comment='Catalog keys with more than one accepted occurrence'),
    col('WARNINGS', 'ARRAY', True, comment='Saved extraction and validation warnings'),
    col('CREATED_BY_LOAD_RUN_ID', 'VARCHAR(36)', True, comment='LOAD_RUN_ID in AUDIT_LAYER.NCFTA_LOAD_AUDIT'),
    col('CURATED_AT', 'TIMESTAMP_TZ', True, 'CURRENT_TIMESTAMP()', 'Ingestion time, never source version order'),
]


def schema(document):
    fields = document['fields']
    keys = [field['key'] for field in fields]
    if len(keys) != 118 or len(set(keys)) != 118:
        raise ValueError('Expected exactly 118 unique catalog fields.')
    if not all(re.fullmatch(r'[a-z][a-z0-9_]*', key) for key in keys):
        raise ValueError('Invalid catalog identifier.')
    if not METADATA <= set(keys):
        raise ValueError('Missing observation metadata fields.')
    if {key.upper() for key in keys} & {c['name'] for c in TECHNICAL}:
        raise ValueError('Catalog field collides with a technical column.')
    result = list(TECHNICAL)
    for field in fields:
        metadata = field['key'] in METADATA
        comment = ('Source metadata: preserve exact first_seen/last_seen text; no inferred timezone.' if metadata else
                   'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. ' + field['definition'])
        result.append(col(field['key'].upper(), 'VARCHAR' if metadata else 'ARRAY', comment=comment))
    return result


def literal(text):
    return "'" + text.replace("'", "''").replace('\r', ' ').replace('\n', ' ') + "'"


def render(columns):
    definitions = []
    for column in columns:
        definition = '  ' + column['name'] + ' ' + column['data_type']
        if column['required']:
            definition += ' NOT NULL'
        if column['default'] is not None:
            definition += ' DEFAULT ' + column['default']
        if column['comment']:
            definition += ' COMMENT ' + literal(column['comment'])
        definitions.append(definition)
    return f"""-- NCFTA layer 3: REFINED | prepared locally, not deployed/server-validated.
-- Catalog v2.0.0 SHA-256: {CATALOG_SHA256}
-- Exactly 118 catalog columns + 33 technical columns = 151 columns.
-- Row grain: one saved RAW attribute outcome per transformation version.
-- Logical unique key: (RESULT_ID, TRANSFORM_VERSION); enforced by future procedure.
-- A retry reuses its row; a new RAW observation or transform retains its history.
-- This is an observation/history table, not an active-only or person/account table.
-- Arrays contain strings; the transformation must validate element types/spans.
-- Two observation metadata fields preserve source text rather than model output.
-- Deleted/unevaluated rows retain lineage; model field arrays remain SQL NULL.
-- No event values, source files, model files or mapping workbooks are changed.
-- Run as an authorized role, separately from load transactions.
-- IF NOT EXISTS does NOT migrate an earlier table; run the verification script.
-- No rows are loaded and no procedures/views/permissions are created here.
-- Syntax references:
-- https://docs.snowflake.com/en/sql-reference/sql/create-table
-- https://docs.snowflake.com/en/sql-reference/data-types-semistructured

USE DATABASE COMPLIANCE_SCREENING_SANDBOX;

CREATE TABLE IF NOT EXISTS REFINED.NCFTA_FIELDS (
""" + ',\n'.join(definitions) + "\n) COMMENT = '118 catalog fields plus source, model, transformation and deletion lineage; historical attribute observations';\n"


def verification(columns):
    values = []
    for column in columns:
        kind = column['data_type'].split('(')[0]
        length = re.fullmatch(r'VARCHAR\((\d+)\)', column['data_type'])
        values.append('    (' + ', '.join((literal(column['name']), literal(kind),
                      literal('NO' if column['required'] else 'YES'), length[1] if length else 'NULL')) + ')')
    return """-- Read-only layer-3 definition check. No source rows are accessed.
-- The last query returns mismatches only. Zero rows = names/types/nullability
-- and bounded VARCHAR lengths match. Inspect DESCRIBE for default expressions.
-- An absent/inaccessible table returns all expected columns as MISSING.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;
SELECT CURRENT_ACCOUNT() AS ACCOUNT_NAME, CURRENT_ROLE() AS ROLE_NAME,
       CURRENT_DATABASE() AS DATABASE_NAME;
DESCRIBE TABLE REFINED.NCFTA_FIELDS;

WITH expected (COLUMN_NAME, EXPECTED_TYPE, EXPECTED_NULLABLE, EXPECTED_LENGTH) AS (
  SELECT * FROM VALUES
""" + ',\n'.join(values) + """
), actual AS (
  SELECT COLUMN_NAME,
         CASE WHEN DATA_TYPE IN ('TEXT', 'VARCHAR') THEN 'VARCHAR' ELSE DATA_TYPE END AS ACTUAL_TYPE,
         IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = 'REFINED' AND TABLE_NAME = 'NCFTA_FIELDS'
)
SELECT COALESCE(e.COLUMN_NAME, a.COLUMN_NAME) AS COLUMN_NAME,
       e.EXPECTED_TYPE, a.ACTUAL_TYPE,
       e.EXPECTED_NULLABLE, a.IS_NULLABLE AS ACTUAL_NULLABLE,
       e.EXPECTED_LENGTH, a.CHARACTER_MAXIMUM_LENGTH AS ACTUAL_LENGTH,
       CASE WHEN a.COLUMN_NAME IS NULL THEN 'MISSING'
            WHEN e.COLUMN_NAME IS NULL THEN 'UNEXPECTED'
            WHEN e.EXPECTED_TYPE <> a.ACTUAL_TYPE THEN 'TYPE_MISMATCH'
            WHEN e.EXPECTED_NULLABLE <> a.IS_NULLABLE THEN 'NULLABILITY_MISMATCH'
            WHEN e.EXPECTED_LENGTH IS NOT NULL
                 AND e.EXPECTED_LENGTH <> a.CHARACTER_MAXIMUM_LENGTH THEN 'LENGTH_MISMATCH'
            WHEN e.EXPECTED_TYPE = 'NUMBER'
                 AND (a.NUMERIC_PRECISION <> 38 OR a.NUMERIC_SCALE <> 0) THEN 'NUMBER_MISMATCH'
            ELSE 'MATCH' END AS CHECK_RESULT
FROM expected e FULL OUTER JOIN actual a ON e.COLUMN_NAME = a.COLUMN_NAME
WHERE a.COLUMN_NAME IS NULL OR e.COLUMN_NAME IS NULL
   OR e.EXPECTED_TYPE <> a.ACTUAL_TYPE OR e.EXPECTED_NULLABLE <> a.IS_NULLABLE
   OR (e.EXPECTED_LENGTH IS NOT NULL AND e.EXPECTED_LENGTH <> a.CHARACTER_MAXIMUM_LENGTH)
   OR (e.EXPECTED_TYPE = 'NUMBER' AND (a.NUMERIC_PRECISION <> 38 OR a.NUMERIC_SCALE <> 0))
ORDER BY COLUMN_NAME;
"""


def build():
    data = CATALOG.read_bytes()
    if hashlib.sha256(data).hexdigest() != CATALOG_SHA256:
        raise ValueError('Catalog checksum changed. Review the catalog before regenerating this schema.')
    columns = schema(json.loads(data.decode('utf-8-sig')))
    (HERE / '03_refined.sql').write_text(render(columns), encoding='utf-8')
    (HERE / '03_verify_refined.sql').write_text(verification(columns), encoding='utf-8')
    print('Generated 03_refined.sql and 03_verify_refined.sql: 118 catalog + 33 technical columns.')


if __name__ == '__main__':
    build()
