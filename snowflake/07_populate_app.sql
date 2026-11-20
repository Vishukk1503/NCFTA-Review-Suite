-- App publisher v1. Prepared locally; not Snowflake-server validated.
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
"""Snowflake RAW handler. No network, model execution, or local source mutation."""
import hashlib
import json
import re
import uuid
from datetime import date

DB = 'COMPLIANCE_SCREENING_SANDBOX'
STAGE = DB + '.INTERNAL_STAGE.NCFTA_STAGE'
AUDIT = DB + '.AUDIT_LAYER.NCFTA_LOAD_AUDIT'
MAX_ROWS = 50000
MAX_BYTES = 64 * 1024 * 1024


class Invalid(Exception):
    pass


def require(ok, code):
    if not ok:
        raise Invalid(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def uid(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'ncfta:' + value))


def flag(value):
    if value is None:
        return None
    require(type(value) in (bool, int, str), 'INVALID_DELETE_FLAG')
    require(value in (True, False, 0, 1, '0', '1', 'true', 'false'), 'INVALID_DELETE_FLAG')
    return value in (True, 1, '1', 'true')


def text(value):
    return None if value is None or value == '' else str(value)


def metadata(batch):
    required = {'SOURCE_RUN_NAME', 'SOURCE_INSTANCE_ID', 'SOURCE_MANIFEST',
                'SOURCE_EVENTS_SHA256', 'SOURCE_RECORDS_SHA256', 'SOURCE_PREDICTIONS_SHA256',
                'CATALOG_SHA256', 'MODEL_SHA256', 'ASSET_HASH', 'PREDICTION_IDENTITY',
                'THRESHOLD', 'SOURCE_GROUP_IDS', 'SOURCE_DATE_FROM', 'SOURCE_DATE_TO',
                'EXPECTED_EVENTS', 'EXPECTED_OBJECTS', 'EXPECTED_ATTRIBUTES', 'EXPECTED_PREDICTION_RECORDS'}
    require(isinstance(batch, dict) and set(batch) == required, 'BATCH_CONTRACT')
    for k in required:
        if k.endswith('SHA256') or k in ('ASSET_HASH', 'PREDICTION_IDENTITY'):
            require(isinstance(batch[k], str) and re.fullmatch('[0-9a-f]{64}', batch[k]), 'INVALID_HASH')
        if k.startswith('EXPECTED_'):
            require(type(batch[k]) is int and 0 <= batch[k] <= MAX_ROWS, 'INVALID_COUNT')
    require(all(isinstance(batch[k], str) and batch[k] for k in ('SOURCE_RUN_NAME', 'SOURCE_INSTANCE_ID')), 'SOURCE_NAMESPACE')
    require(isinstance(batch['SOURCE_GROUP_IDS'], list) and all(type(g) is int for g in batch['SOURCE_GROUP_IDS']), 'GROUPS')
    require(type(batch['THRESHOLD']) in (float, int) and 0 <= batch['THRESHOLD'] <= 1, 'THRESHOLD')
    for k in ('SOURCE_DATE_FROM', 'SOURCE_DATE_TO'):
        if batch[k] is not None:
            require(isinstance(batch[k], str) and date.fromisoformat(batch[k]).isoformat() == batch[k], 'DATE')
    if batch['SOURCE_DATE_FROM'] and batch['SOURCE_DATE_TO']:
        require(batch['SOURCE_DATE_FROM'] <= batch['SOURCE_DATE_TO'], 'DATE_ORDER')
    m = batch['SOURCE_MANIFEST']
    require(isinstance(m, dict) and m.get('fetch_complete') is True and m.get('inference_complete') is True and m.get('phase') == 'complete', 'RUN_NOT_COMPLETE')
    identity = digest({'assets': batch['ASSET_HASH'], 'threshold': batch['THRESHOLD'], 'records': batch['SOURCE_RECORDS_SHA256']})
    require(identity == batch['PREDICTION_IDENTITY'], 'PREDICTION_IDENTITY')
    fingerprint = digest(batch)
    return dict(batch, BATCH_FINGERPRINT=fingerprint, SOURCE_BATCH_ID=uid(fingerprint))


def validate_files(files, batch_id):
    require(isinstance(files, list) and len(files) <= 100, 'FILE_LIST')
    seen = set()
    for f in files:
        require(isinstance(f, dict) and set(f) == {'path', 'kind', 'rows', 'canonical_sha256'}, 'FILE_CONTRACT')
        require(f['kind'] in ('events', 'results'), 'FILE_KIND')
        pattern = re.escape(batch_id + '/' + f['kind'] + '/') + r'[A-Za-z0-9_-]+\.jsonl(?:\.gz)?'
        require(isinstance(f['path'], str) and re.fullmatch(pattern, f['path']), 'FILE_PATH')
        require(f['path'] not in seen, 'DUPLICATE_FILE')
        seen.add(f['path'])
        require(type(f['rows']) is int and 0 < f['rows'] <= MAX_ROWS, 'FILE_ROWS')
        require(isinstance(f['canonical_sha256'], str) and re.fullmatch('[0-9a-f]{64}', f['canonical_sha256']), 'FILE_HASH')


def prepare(batch, events, results):
    """Validate saved evidence and derive all identity/deletion/source-value columns."""
    b = metadata(batch)
    require(len(events) == b['EXPECTED_EVENTS'] and len(results) == b['EXPECTED_PREDICTION_RECORDS'], 'ROW_COUNTS')
    event_rows, result_rows, attrs, event_keys = [], [], {}, set()
    objects = 0
    for e, location in events:
        require(isinstance(e, dict) and (e.get('uuid') or e.get('id')), 'EVENT_ID')
        key = str(e.get('uuid') or e['id'])
        require(key not in event_keys, 'DUPLICATE_EVENT')
        event_keys.add(key)
        sid = uid(b['SOURCE_BATCH_ID'] + ':event:' + key)
        row = dict(EVENT_SNAPSHOT_ID=sid, SOURCE_BATCH_ID=b['SOURCE_BATCH_ID'], EVENT_KEY=key,
                   EVENT_UUID=text(e.get('uuid')), SOURCE_EVENT_ID=text(e.get('id')), EVENT_DATE=text(e.get('date')),
                   EVENT_SHARING_GROUP_ID=text(e.get('sharing_group_id')), EVENT_PAYLOAD=e, PAYLOAD_SHA256=digest(e), **location)
        event_rows.append(row)
        objlist = e.get('Object') or []
        require(isinstance(objlist, list), 'OBJECT_LIST')
        groups = [(None, e.get('Attribute') or [])]
        for obj in objlist:
            require(isinstance(obj, dict), 'OBJECT')
            objects += 1
            groups.append((obj, obj.get('Attribute') or []))
        for obj, alist in groups:
            require(isinstance(alist, list), 'ATTRIBUTE_LIST')
            for pos, a in enumerate(alist):
                require(isinstance(a, dict), 'ATTRIBUTE')
                objkey = str((obj or {}).get('uuid') or (obj or {}).get('id') or 'direct')
                attrkey = str(a.get('uuid') or a.get('id') or f'position-{pos}')
                rid = 'R' + digest([key, objkey, attrkey])[:20]
                if rid in attrs:
                    require(attrs[rid] == (row, obj, a), 'CONFLICTING_ATTRIBUTE')
                else:
                    attrs[rid] = (row, obj, a)
    require(objects == b['EXPECTED_OBJECTS'] and len(attrs) == b['EXPECTED_ATTRIBUTES'] == len(results), 'ATTRIBUTE_COUNTS')
    seen = set()
    for envelope, location in results:
        require(isinstance(envelope, dict) and set(envelope) == {'source_record', 'prediction'}, 'RESULT_ENVELOPE')
        r, p = envelope['source_record'], envelope['prediction']
        require(isinstance(r, dict) and isinstance(p, dict), 'RESULT_OBJECT')
        rid = r.get('review_id')
        require(isinstance(rid, str) and rid in attrs and rid not in seen, 'RESULT_JOIN')
        seen.add(rid)
        e, obj, a = attrs[rid]
        obj = obj or {}
        require(r.get('source_hash') == digest(a), 'ATTRIBUTE_HASH')
        require(isinstance(r.get('input'), str) and p.get('input_hash') == digest(r['input']), 'INPUT_HASH')
        require(p.get('review_id') == rid and p.get('identity') == b['PREDICTION_IDENTITY'], 'PREDICTION_JOIN')
        status = p.get('status')
        require(status in ('predicted', 'deleted', 'invalid_source', 'empty_value', 'input_error'), 'OUTCOME')
        require(isinstance(p.get('spans'), list) and isinstance(p.get('warnings'), list), 'PREDICTION_SHAPE')
        normalized = {dst: a.get(src) if isinstance(a.get(src), str) else '' for dst, src in
                      [('source_type','type'), ('source_category','category'), ('source_comment','comment'), ('source_value','value')]}
        require(r.get('source_row') == normalized, 'SOURCE_VALUE_MISMATCH')
        prefix = ('Source type: ' + normalized['source_type'] + '\nSource category: ' + normalized['source_category']
                  + '\nSource comment: ' + normalized['source_comment'] + '\nSource value:\n')
        require(r['input'] == prefix + normalized['source_value'] and r.get('value_offset') == len(prefix), 'INPUT_CONTEXT')
        for record_key, expected in [('event_id',e['SOURCE_EVENT_ID']), ('event_uuid',e['EVENT_UUID']),
                                     ('object_id',text(obj.get('id'))), ('object_uuid',text(obj.get('uuid'))),
                                     ('attribute_id',text(a.get('id'))), ('attribute_uuid',text(a.get('uuid')))]:
            require(text(r.get(record_key)) == expected, 'SOURCE_ID_MISMATCH')
        ad, od = flag(a.get('deleted')), flag(obj.get('deleted'))
        deleted = ad is True or od is True
        require(r.get('deleted') is deleted, 'DELETE_MISMATCH')
        require(not deleted or status in ('deleted', 'invalid_source'), 'DELETED_PREDICTION')
        require(status != 'deleted' or deleted, 'FALSE_DELETION')
        require(status == 'predicted' or not p['spans'], 'UNEVALUATED_SPANS')
        row = dict(RESULT_ID=uid(b['SOURCE_BATCH_ID'] + ':result:' + rid), SOURCE_BATCH_ID=b['SOURCE_BATCH_ID'],
                   EVENT_SNAPSHOT_ID=e['EVENT_SNAPSHOT_ID'], SOURCE_REVIEW_ID=rid,
                   EVENT_UUID=e['EVENT_UUID'], OBJECT_UUID=text(obj.get('uuid')), ATTRIBUTE_UUID=text(a.get('uuid')),
                   SOURCE_EVENT_ID=e['SOURCE_EVENT_ID'], SOURCE_OBJECT_ID=text(obj.get('id')), SOURCE_ATTRIBUTE_ID=text(a.get('id')),
                   EVENT_SHARING_GROUP_ID=e['EVENT_SHARING_GROUP_ID'], ATTRIBUTE_SHARING_GROUP_ID=text(a.get('sharing_group_id')),
                   ATTRIBUTE_DISTRIBUTION=text(a.get('distribution')), SOURCE_TYPE=text(a.get('type')), SOURCE_CATEGORY=text(a.get('category')),
                   SOURCE_VALUE=a.get('value') if isinstance(a.get('value'), str) else None,
                   SOURCE_ATTRIBUTE_SHA256=digest(a), INPUT_SHA256=p['input_hash'], PREDICTION_IDENTITY=p['identity'],
                   MODEL_SHA256=b['MODEL_SHA256'], OUTCOME_STATUS=status, SOURCE_ATTRIBUTE_DELETED=ad,
                   SOURCE_OBJECT_DELETED=od, IS_DELETED=deleted, SOURCE_RECORD=r, PREDICTION=p,
                   RESULT_PAYLOAD_SHA256=digest(envelope), **location)
        result_rows.append(row)
    return b, event_rows, result_rows


CATALOG_SHA256 = '58bf1531ceb3c2957b34ae8ddf8c14c37ec076db2ad2e166878c1fb9c2244cda'
SCHEMA = [{'name': 'CURATED_ROW_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Stable loader UUID for RESULT_ID and TRANSFORM_VERSION'}, {'name': 'RESULT_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_MODEL_RESULTS'}, {'name': 'SOURCE_BATCH_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_BATCHES'}, {'name': 'EVENT_SNAPSHOT_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_EVENTS'}, {'name': 'SOURCE_INSTANCE_ID', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'Source namespace from the batch; no credentials'}, {'name': 'EVENT_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'OBJECT_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_EVENT_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_OBJECT_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_ATTRIBUTE_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_REVIEW_ID', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'Saved internal join key, not a source UUID'}, {'name': 'EVENT_SHARING_GROUP_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_SHARING_GROUP_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_DISTRIBUTION', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_TYPE', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_CATEGORY', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_ATTRIBUTE_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': 'Source attribute hash copied from RAW'}, {'name': 'MODEL_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': ''}, {'name': 'CATALOG_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': ''}, {'name': 'PREDICTION_IDENTITY', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': 'Saved inference identity; not a source version timestamp'}, {'name': 'TRANSFORM_VERSION', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': ''}, {'name': 'OUTCOME_STATUS', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'predicted, deleted, invalid_source, empty_value or input_error'}, {'name': 'SOURCE_ATTRIBUTE_DELETED', 'data_type': 'BOOLEAN', 'required': False, 'default': None, 'comment': 'Original attribute deletion signal; absent stays NULL'}, {'name': 'SOURCE_OBJECT_DELETED', 'data_type': 'BOOLEAN', 'required': False, 'default': None, 'comment': 'Original parent object deletion signal; absent stays NULL'}, {'name': 'IS_DELETED', 'data_type': 'BOOLEAN', 'required': True, 'default': None, 'comment': 'True when either source signal is true; absence is not a deletion'}, {'name': 'REVIEW_STATUS', 'data_type': 'VARCHAR', 'required': True, 'default': "'NOT_REVIEWED'", 'comment': 'No automatic analyst approval'}, {'name': 'ACCEPTED_SPAN_COUNT', 'data_type': 'NUMBER(38,0)', 'required': True, 'default': None, 'comment': ''}, {'name': 'REJECTED_SPAN_COUNT', 'data_type': 'NUMBER(38,0)', 'required': True, 'default': None, 'comment': ''}, {'name': 'MULTIVALUE_FIELDS', 'data_type': 'ARRAY', 'required': True, 'default': None, 'comment': 'Catalog keys with more than one accepted occurrence'}, {'name': 'WARNINGS', 'data_type': 'ARRAY', 'required': True, 'default': None, 'comment': 'Saved extraction and validation warnings'}, {'name': 'CREATED_BY_LOAD_RUN_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'LOAD_RUN_ID in AUDIT_LAYER.NCFTA_LOAD_AUDIT'}, {'name': 'CURATED_AT', 'data_type': 'TIMESTAMP_TZ', 'required': True, 'default': 'CURRENT_TIMESTAMP()', 'comment': 'Ingestion time, never source version order'}, {'name': 'REPORTED_ACTOR_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported threat actor name; no account ownership inferred.'}, {'name': 'COMPANY_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Company reported by the source; not automatically a merchant.'}, {'name': 'DATE_OF_BIRTH', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Accepted calendar dates; no automatic association with a particular name.'}, {'name': 'ALIAS_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported alias or moniker. Link to a person only when supported.'}, {'name': 'AGE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported age, including units. Do not derive without a defined reference date.'}, {'name': 'PLACE_OF_BIRTH', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported birthplace, distinct from address and residence.'}, {'name': 'OCCUPATION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported occupation or job title.'}, {'name': 'EMPLOYER_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Organization explicitly reported as an employer.'}, {'name': 'SOURCE_COC_LABEL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Preserve the COC label and value. The abbreviation meaning has not been confirmed.'}, {'name': 'SOURCE_COR_LABEL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Preserve the COR label and value. The abbreviation meaning has not been confirmed.'}, {'name': 'REGISTERED_AGENT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Person or organization explicitly identified as a registered agent.'}, {'name': 'ALTERNATE_TRADE_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported alternate business or trading name.'}, {'name': 'REPORTED_GENDER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Gender explicitly reported in the source. Do not infer from a name.'}, {'name': 'ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported addresses; ownership and address components not inferred.'}, {'name': 'PHONE_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Normalized phone strings; mobile status and country code not inferred.'}, {'name': 'EMAIL_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Email strings passing the configured basic format check.'}, {'name': 'PREVIOUS_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Address explicitly described as previous or former. Preserve any supplied date context.'}, {'name': 'BENEFICIARY_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Address explicitly attributed to a beneficiary.'}, {'name': 'BANK_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source bank names; aliases have not been consolidated.'}, {'name': 'BANK_ACCOUNT_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Account identifiers preserved as text, including leading zeros.'}, {'name': 'ROUTING_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Nine-digit routing values passing the configured checksum.'}, {'name': 'SWIFT_CODE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source SWIFT values passing a syntax check.'}, {'name': 'BANK_LOCATION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit bank location; no bank/location pairing inferred.'}, {'name': 'BENEFICIARY_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Name explicitly attributed to a beneficiary. Never infer from event co-occurrence.'}, {'name': 'ACCOUNT_HOLDER_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Name explicitly attributed to an account holder. Requires evidence linking the name and account.'}, {'name': 'REMITTER_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Name explicitly identified as the remitter or sender.'}, {'name': 'PAYEE_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Name explicitly identified as the payee or recipient.'}, {'name': 'FOR_BENEFIT_OF', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported FBO instruction. Preserve the full instruction and its relationship evidence.'}, {'name': 'FOR_FURTHER_CREDIT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported FFC instruction. Preserve the full instruction and its relationship evidence.'}, {'name': 'ACCOUNT_TYPE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit account type, such as the source-labeled receiving account type.'}, {'name': 'REPORTED_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Accepted reported amounts. Explicit currency symbols retained; currency code unknown.'}, {'name': 'SHARES', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported share quantities; no transaction or stock pairing inferred.'}, {'name': 'STOCK_SYMBOL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported stock symbols; exchange not inferred.'}, {'name': 'NET_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit net amount, distinct from gross amount and generic reported amount.'}, {'name': 'GROSS_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit gross amount. Empty source labels do not supply an amount.'}, {'name': 'FEDERAL_TAX_WITHHOLDING', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported federal tax or withholding amount. Preserve the original tax label.'}, {'name': 'STATE_TAX_WITHHOLDING', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported state tax or withholding amount. Preserve the original tax label.'}, {'name': 'PRINCIPAL_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit principal amount, separate from fees, taxes and totals.'}, {'name': 'TOTAL_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported total. Preserve what was totaled and its scope.'}, {'name': 'REPORTED_CURRENCY', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit currency code. Do not infer currency from an ambiguous symbol.'}, {'name': 'LOSS_STATUS_TEXT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source wording about loss, including explicit no-loss statements. Not a monetary amount.'}, {'name': 'RECOVERY_STATUS_TEXT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source wording about recovery or return of funds. Not a recovered amount.'}, {'name': 'SSN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Full-format source SSN values passing structural checks; identity unverified.'}, {'name': 'SSN_PARTIAL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reserved final field. Current short SSN values require review and are not populated here.'}, {'name': 'EMPLOYER_TAX_ID', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled EIN/FEIN identifier. Do not treat it as an SSN.'}, {'name': 'TAXPAYER_ID', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled TIN identifier. Do not assume it is an SSN or EIN.'}, {'name': 'DRIVERS_LICENSE_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled license identifier. Preserve letters, masks and leading zeros.'}, {'name': 'DRIVERS_LICENSE_STATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit license-issuing state. Link to a license only when supported.'}, {'name': 'PASSPORT_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled passport identifier. Nationality and ownership are separate facts.'}, {'name': 'DOCUMENT_ISSUE_DATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit issue date. Its document relationship must be supported.'}, {'name': 'CARD_PAN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit card values passing configured length/checksum rules; never inferred from arbitrary numbers.'}, {'name': 'CARD_BIN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit six/eight-digit BIN; not derived from account numbers.'}, {'name': 'MERCHANT_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Requires explicit merchant evidence. Company alone is insufficient.'}, {'name': 'MERCHANT_CATEGORY_CODE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit merchant category code (MCC), stored as a text identifier.'}, {'name': 'SOURCE_IP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source IP values passing syntax validation.'}, {'name': 'DESTINATION_IP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Destination IP values passing syntax validation.'}, {'name': 'DOMAIN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Domain strings passing the configured syntax check.'}, {'name': 'URL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. HTTP/HTTPS URL strings passing the configured syntax check.'}, {'name': 'DEVICE_FINGERPRINT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported device identifiers.'}, {'name': 'BROWSER_USER_AGENT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported user-agent strings.'}, {'name': 'DOMAIN_CREATION_DATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported domain creation date. Link to the domain only when explicit.'}, {'name': 'DOMAIN_REGISTRAR', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Registrar explicitly associated with a domain.'}, {'name': 'REGISTRANT_REGION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported registrant state/province.'}, {'name': 'CRIME_DATETIME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source crime datetimes; timezone is preserved only when supplied.'}, {'name': 'REPORTER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source Reported By values; not assumed to be the actor or victim.'}, {'name': 'INCIDENT_TYPE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported incident classification. Retain the original label and supporting narrative.'}, {'name': 'FRAUD_CHANNEL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported channel through which activity occurred, such as web, phone or ATM.'}, {'name': 'GEOGRAPHIC_AREA', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported geographic scope of activity. It is not a person address.'}, {'name': 'REPORTED_TACTICS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source description of tactics or TTPs. Preserve original wording.'}, {'name': 'FRAUD_INDICATORS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported fraud indicators as narrative evidence.'}, {'name': 'COMPLAINT_REFERENCE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source complaint reference, separate from MISP and CyFin event identifiers.'}, {'name': 'TRANSACTION_TYPE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source description of a transfer, payment or other transaction type.'}, {'name': 'TRANSACTION_STATUS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported status such as sent, blocked or executed. Interpret the status in its transaction context.'}, {'name': 'TRANSACTION_MEMO', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Memo associated with a reported transaction.'}, {'name': 'TRANSACTION_REFERENCE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Trace or payment-system reference. Preserve its source reference type.'}, {'name': 'CHECK_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Check identifier, stored as text including masks and leading zeros.'}, {'name': 'PROCESS_DATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source processing date. Keep separate from event date and crime datetime.'}, {'name': 'TRADE_ACTION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported buy/sell action or side. Generic Action labels need trade context.'}, {'name': 'ORDER_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Order identifier stored as text. A single event can contain multiple orders.'}, {'name': 'SECURITY_CUSIP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled CUSIP security identifier, distinct from the stock symbol.'}, {'name': 'SECURITY_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported security or fund name.'}, {'name': 'PRICE_PER_SHARE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit per-share price. Preserve currency and the associated trade evidence.'}, {'name': 'REPORTED_PRICE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Price with an unspecified unit. Do not assume it means price per share.'}, {'name': 'EXECUTION_TIME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported execution time and supplied timezone. Do not invent a missing date or timezone.'}, {'name': 'REPORTED_TIME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source Time label whose event or transaction meaning needs context.'}, {'name': 'GAIN_LOSS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported gain/loss value. Preserve sign, currency and transaction evidence.'}, {'name': 'BROKER_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled broker identifier. Its issuing system must be retained or reviewed.'}, {'name': 'DTC_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled DTC number stored as an identifier.'}, {'name': 'CONTRA_FIRM', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Reported counterparty firm. Association with an account requires explicit evidence.'}, {'name': 'CONTRA_FIRM_ACCOUNT_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Account explicitly attributed to the counterparty firm.'}, {'name': 'BROKER_CRD_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-labeled CRD identifier, distinct from a generic broker number.'}, {'name': 'USERNAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported username or login identifier. Preserve platform when stated.'}, {'name': 'TELEGRAM_HANDLE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Telegram handle explicitly named in the source.'}, {'name': 'INSTAGRAM_HANDLE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Instagram handle explicitly named in the source.'}, {'name': 'WHATSAPP_ID', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. WhatsApp identifier explicitly named in the source.'}, {'name': 'PAYMENT_TOKEN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported payment token. Its email/phone form does not by itself establish ownership.'}, {'name': 'SKIMMER_TYPE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source description of the skimmer type, such as POS.'}, {'name': 'DEVICE_BRAND_MODEL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Brand/model reported in the skimming-device context.'}, {'name': 'DEVICE_COUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported count in device context. Preserve the item counted.'}, {'name': 'STORE_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit store identifier, separate from the store or merchant name.'}, {'name': 'LICENSE_PLATE_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source-reported vehicle plate identifier, including partial or masked forms.'}, {'name': 'LICENSE_PLATE_STATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. State explicitly associated with a license plate.'}, {'name': 'VEHICLE_VIN', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit vehicle VIN. Preserve its vehicle relationship and validation status.'}, {'name': 'CONTRACT_DATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source contract date. Preserve the contract or property association.'}, {'name': 'CLOSING_DATE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Source closing date. Preserve the transaction or property association.'}, {'name': 'FILE_MD5', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_SHA1', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_SHA256', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_IMPHASH', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_SSDEEP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'FILE_SIZE_BYTES', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'TARGET_ORGANIZATION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'OBSERVATION_FIRST_SEEN', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': 'Source metadata: preserve exact first_seen/last_seen text; no inferred timezone.'}, {'name': 'OBSERVATION_LAST_SEEN', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': 'Source metadata: preserve exact first_seen/last_seen text; no inferred timezone.'}, {'name': 'REPORTED_DATETIME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit MISP source type/label value. Preserve provenance, format validation and permissions; no ownership/attribution inference.'}, {'name': 'SKIMMER_LOCATION_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Name from Store label in an explicit skimming-device record. Not assumed to be a transaction merchant.'}, {'name': 'REPORTED_IP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Literal text values in source-span order; [] means evaluated with no accepted match; NULL means not evaluated. Explicit IP address label without source/destination direction. Do not infer traffic direction.'}]

"""Validate saved evidence and populate REFINED; never run a model."""
import json
import uuid

TRANSFORM_VERSION = 'refined-v1'
TARGET = DB + '.REFINED.NCFTA_FIELDS'
AUDIT = DB + '.AUDIT_LAYER.NCFTA_LOAD_AUDIT'
MODEL_FIELDS = [c['name'].lower() for c in SCHEMA if c['data_type'] == 'ARRAY'
                and c['name'] not in ('WARNINGS', 'MULTIVALUE_FIELDS')]


def transform(batch, row):
    require(batch['CATALOG_SHA256'] == CATALOG_SHA256, 'CATALOG_MISMATCH')
    r, p = row['SOURCE_RECORD'], row['PREDICTION']
    require(p['status'] == row['OUTCOME_STATUS'] and p['identity'] == row['PREDICTION_IDENTITY'], 'RAW_PREDICTION_MISMATCH')
    out = {c['name']:row[c['name']] for c in SCHEMA if c['name'] in row}
    # Remove RAW-only keys by selecting schema names above.
    out.update(CURATED_ROW_ID=uid(row['RESULT_ID'] + ':' + TRANSFORM_VERSION),
               SOURCE_INSTANCE_ID=batch['SOURCE_INSTANCE_ID'], CATALOG_SHA256=CATALOG_SHA256,
               TRANSFORM_VERSION=TRANSFORM_VERSION, REVIEW_STATUS='NOT_REVIEWED')
    warnings = p.get('warnings')
    require(isinstance(warnings, list) and all(isinstance(w,str) for w in warnings), 'WARNINGS_SHAPE')
    out['WARNINGS'] = list(warnings)
    evaluated = row['OUTCOME_STATUS'] == 'predicted' and not row['IS_DELETED']
    for field in MODEL_FIELDS:
        out[field.upper()] = [] if evaluated else None
    spans = p.get('spans')
    require(isinstance(spans,list), 'SPANS_SHAPE')
    require(evaluated or not spans, 'UNEVALUATED_SPANS')
    accepted, rejected = [], 0
    for index, s in enumerate(spans):
        require(isinstance(s,dict) and type(s.get('accepted')) is bool, 'SPAN_ACCEPTANCE')
        if not s['accepted']:
            rejected += 1
            continue
        require(s.get('field') in MODEL_FIELDS, 'UNKNOWN_ACCEPTED_FIELD')
        start, end = s.get('start'), s.get('end')
        require(type(start) is int and type(end) is int and r['value_offset'] <= start < end <= len(r['input']), 'SPAN_BOUNDS')
        require(isinstance(s.get('text'),str) and r['input'][start:end] == s['text'], 'NONLITERAL_SPAN')
        require(type(s.get('value_start')) is int and type(s.get('value_end')) is int
                and s['value_start'] == start-r['value_offset'] and s['value_end'] == end-r['value_offset'], 'VALUE_OFFSETS')
        require(isinstance(row['SOURCE_VALUE'],str)
                and row['SOURCE_VALUE'][s['value_start']:s['value_end']] == s['text'], 'SOURCE_SPAN')
        accepted.append((start,end,index,s))
    for _,_,_,s in sorted(accepted, key=lambda item:item[:3]):
        out[s['field'].upper()].append(s['text'])
    out['ACCEPTED_SPAN_COUNT'] = len(accepted)
    out['REJECTED_SPAN_COUNT'] = rejected
    out['MULTIVALUE_FIELDS'] = [f for f in MODEL_FIELDS if out[f.upper()] is not None and len(out[f.upper()])>1]
    for dest, src in [('OBSERVATION_FIRST_SEEN','first_seen'), ('OBSERVATION_LAST_SEEN','last_seen')]:
        value = r.get(src)
        if value is not None and not isinstance(value,str):
            out['WARNINGS'].append('Non-string source metadata: ' + src)
            value = None
        out[dest] = value
    expected = {c['name'] for c in SCHEMA} - {'CREATED_BY_LOAD_RUN_ID','CURATED_AT'}
    require(set(out) == expected, 'CURATED_SCHEMA_MISMATCH')
    return out


def validate_raw(batch, events, results):
    """Revalidate complete RAW snapshot against source JSON before transformation."""
    info = {k:v for k,v in batch.items() if k not in ('SOURCE_BATCH_ID','BATCH_FINGERPRINT','FIRST_LOAD_RUN_ID','REGISTERED_AT')}
    def location(row):
        return {k:row[k] for k in ('STAGED_FILENAME','FILE_RECORD_NUMBER','FILE_CONTENT_KEY')}
    calculated, ev, rs = prepare(info, [(e['EVENT_PAYLOAD'],location(e)) for e in events],
                               [({'source_record':r['SOURCE_RECORD'],'prediction':r['PREDICTION']},location(r)) for r in results])
    require(all(batch.get(k)==v for k,v in calculated.items()), 'RAW_BATCH_CONFLICT')
    for expected, actual, key in [(ev,events,'EVENT_SNAPSHOT_ID'),(rs,results,'RESULT_ID')]:
        indexed = {r[key]:r for r in actual}
        require(len(indexed)==len(actual), 'RAW_DUPLICATE_ID')
        for r in expected:
            saved = indexed.get(r[key],{})
            require(all(saved.get(k)==v for k,v in r.items()), 'RAW_CONTENT_CONFLICT')
    return [transform(batch,r) for r in results]

MAPPING_SHA256 = 'c1fe944df6a28bf3226435ab7f992d2db1da17c2cdb9a401c139b6843373a980'
APP_SCHEMA = [{'name': 'CURATED_ROW_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Stable loader UUID for RESULT_ID and TRANSFORM_VERSION'}, {'name': 'RESULT_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_MODEL_RESULTS'}, {'name': 'SOURCE_BATCH_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_BATCHES'}, {'name': 'EVENT_SNAPSHOT_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Links to RAW.NCFTA_EVENTS'}, {'name': 'SOURCE_INSTANCE_ID', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'Source namespace from the batch; no credentials'}, {'name': 'EVENT_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'OBJECT_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_UUID', 'data_type': 'VARCHAR(36)', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_EVENT_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_OBJECT_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_ATTRIBUTE_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_REVIEW_ID', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'Saved internal join key, not a source UUID'}, {'name': 'EVENT_SHARING_GROUP_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_SHARING_GROUP_ID', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'ATTRIBUTE_DISTRIBUTION', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_TYPE', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_CATEGORY', 'data_type': 'VARCHAR', 'required': False, 'default': None, 'comment': ''}, {'name': 'SOURCE_ATTRIBUTE_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': 'Source attribute hash copied from RAW'}, {'name': 'MODEL_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': ''}, {'name': 'CATALOG_SHA256', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': ''}, {'name': 'PREDICTION_IDENTITY', 'data_type': 'VARCHAR(64)', 'required': True, 'default': None, 'comment': 'Saved inference identity; not a source version timestamp'}, {'name': 'TRANSFORM_VERSION', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': ''}, {'name': 'OUTCOME_STATUS', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': 'predicted, deleted, invalid_source, empty_value or input_error'}, {'name': 'SOURCE_ATTRIBUTE_DELETED', 'data_type': 'BOOLEAN', 'required': False, 'default': None, 'comment': 'Original attribute deletion signal; absent stays NULL'}, {'name': 'SOURCE_OBJECT_DELETED', 'data_type': 'BOOLEAN', 'required': False, 'default': None, 'comment': 'Original parent object deletion signal; absent stays NULL'}, {'name': 'IS_DELETED', 'data_type': 'BOOLEAN', 'required': True, 'default': None, 'comment': 'True when either source signal is true; absence is not a deletion'}, {'name': 'REVIEW_STATUS', 'data_type': 'VARCHAR', 'required': True, 'default': "'NOT_REVIEWED'", 'comment': 'No automatic analyst approval'}, {'name': 'WARNINGS', 'data_type': 'ARRAY', 'required': True, 'default': None, 'comment': 'Saved extraction and validation warnings'}, {'name': 'APP_ROW_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': 'Stable publication identity for curated row and mapping version'}, {'name': 'APP_MAPPING_VERSION', 'data_type': 'VARCHAR', 'required': True, 'default': None, 'comment': ''}, {'name': 'FIELD_PROVENANCE', 'data_type': 'VARIANT', 'required': True, 'default': None, 'comment': 'Per field and array index: source key, value kind and evidence offsets; preserves URL versus domain and category text versus MCC'}, {'name': 'PUBLISHED_BY_LOAD_RUN_ID', 'data_type': 'VARCHAR(36)', 'required': True, 'default': None, 'comment': ''}, {'name': 'PUBLISHED_AT', 'data_type': 'TIMESTAMP_TZ', 'required': True, 'default': 'CURRENT_TIMESTAMP()', 'comment': 'Publication time, not source version order'}, {'name': 'ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Address. Source: address. Preserve the reported address. Do not infer ownership.'}, {'name': 'REPORTED_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Dollar Amount. Source: reported_amount. Use reported_currency when supplied. Do not assume USD.'}, {'name': 'BANK_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Bank Name. Source: bank_name. Preserve the source bank name.'}, {'name': 'BANK_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Bank Address. Source: bank_location. A bank location may be a city, not a full postal address.'}, {'name': 'CRIME_DATETIME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Date of Crime. Source: crime_datetime. Keep separate from event date. Do not invent a timezone.'}, {'name': 'SUBMITTED_BY', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Submitted By. Source: reporter. Source Reported By value. Not the threat actor or victim.'}, {'name': 'SWIFT_CODE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'SWIFT. Source: swift_code. Preserve the reported SWIFT value as text.'}, {'name': 'COMMENTS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Comments. Source: Raw attribute.value\n(saved source_value). Use the comment-type / Other attribute value, not its metadata comment.'}, {'name': 'BANK_ACCOUNT_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Bank Account #. Source: bank_account_number. Store as text to preserve leading zeros and masking.'}, {'name': 'ROUTING_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Bank Routing #. Source: routing_number. Store as text to preserve leading zeros.'}, {'name': 'REPORTED_ACTOR_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Threat Actor Name. Source: reported_actor_name. Preserve the reported role. Do not infer account ownership.'}, {'name': 'PHONE_NUMBER', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Phone. Source: phone_number. Do not infer mobile status or country code.'}, {'name': 'SOURCE_IP', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'IP. Source: source_ip. Applies to the ip-src rows shown in the mapping sheet.'}, {'name': 'EMAIL_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Email. Source: email_address. Preserve the source email value and its attribute link.'}, {'name': 'DATE_OF_BIRTH', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'DoB. Source: date_of_birth. Do not associate a date with a person without source evidence.'}, {'name': 'URL', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'URL. Source: url\ndomain. Keep domain and full URL distinguishable. Do not invent a protocol.'}, {'name': 'MERCHANT_CATEGORY', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Merchant Category. Source: Raw attribute.value\nmerchant_category_code (if MCC). Use raw category text. Use merchant_category_code only for an actual MCC.'}, {'name': 'COMPANY_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Company Name. Source: company_name. Preserve the reported company. Keep it separate from merchant and target.'}, {'name': 'ALIAS_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Alias Name. Source: alias_name. Preserve the reported alias. Link it to a person only with evidence.'}, {'name': 'AGE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Age. Source: age. Preserve reported age and units. Do not derive it from date of birth.'}, {'name': 'PLACE_OF_BIRTH', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Place of Birth. Source: place_of_birth. Keep birthplace separate from current or previous address.'}, {'name': 'OCCUPATION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Occupation. Source: occupation. Use the occupation or job title explicitly reported by the source.'}, {'name': 'EMPLOYER_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Employer Name. Source: employer_name. Use an organisation explicitly identified as an employer.'}, {'name': 'TARGET_ORGANIZATION', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Target Organisation. Source: target_organization. Preserve the target role. Keep it separate from company and merchant.'}, {'name': 'MERCHANT_NAME', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Merchant Name. Source: merchant_name. Use explicit merchant evidence. Keep company and target separate.'}, {'name': 'PREVIOUS_ADDRESS', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Previous Address. Source: previous_address. Use an address explicitly described as previous or former.'}, {'name': 'ACCOUNT_TYPE', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Account Type. Source: account_type. Use the reported account type. Link it only to the supported account.'}, {'name': 'TOTAL_AMOUNT', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Total Amount. Source: total_amount. Preserve the reported total and its scope. Do not calculate a new total.'}, {'name': 'REPORTED_CURRENCY', 'data_type': 'ARRAY', 'required': False, 'default': None, 'comment': 'Reported Currency. Source: reported_currency. Use an explicit currency code. Do not infer from an ambiguous symbol.'}]
BUSINESS_NAMES = ['ADDRESS', 'REPORTED_AMOUNT', 'BANK_NAME', 'BANK_ADDRESS', 'CRIME_DATETIME', 'SUBMITTED_BY', 'SWIFT_CODE', 'COMMENTS', 'BANK_ACCOUNT_NUMBER', 'ROUTING_NUMBER', 'REPORTED_ACTOR_NAME', 'PHONE_NUMBER', 'SOURCE_IP', 'EMAIL_ADDRESS', 'DATE_OF_BIRTH', 'URL', 'MERCHANT_CATEGORY', 'COMPANY_NAME', 'ALIAS_NAME', 'AGE', 'PLACE_OF_BIRTH', 'OCCUPATION', 'EMPLOYER_NAME', 'TARGET_ORGANIZATION', 'MERCHANT_NAME', 'PREVIOUS_ADDRESS', 'ACCOUNT_TYPE', 'TOTAL_AMOUNT', 'REPORTED_CURRENCY']
DIRECT_MAP = {'ADDRESS': 'address', 'REPORTED_AMOUNT': 'reported_amount', 'BANK_NAME': 'bank_name', 'BANK_ADDRESS': 'bank_location', 'CRIME_DATETIME': 'crime_datetime', 'SUBMITTED_BY': 'reporter', 'SWIFT_CODE': 'swift_code', 'BANK_ACCOUNT_NUMBER': 'bank_account_number', 'ROUTING_NUMBER': 'routing_number', 'REPORTED_ACTOR_NAME': 'reported_actor_name', 'PHONE_NUMBER': 'phone_number', 'SOURCE_IP': 'source_ip', 'EMAIL_ADDRESS': 'email_address', 'DATE_OF_BIRTH': 'date_of_birth', 'COMPANY_NAME': 'company_name', 'ALIAS_NAME': 'alias_name', 'AGE': 'age', 'PLACE_OF_BIRTH': 'place_of_birth', 'OCCUPATION': 'occupation', 'EMPLOYER_NAME': 'employer_name', 'TARGET_ORGANIZATION': 'target_organization', 'MERCHANT_NAME': 'merchant_name', 'PREVIOUS_ADDRESS': 'previous_address', 'ACCOUNT_TYPE': 'account_type', 'TOTAL_AMOUNT': 'total_amount', 'REPORTED_CURRENCY': 'reported_currency'}

"""Publish the approved app mapping without reclassifying or merging attributes."""
import json
import re
import uuid

APP_MAPPING_VERSION = 'app-v1'
TARGET = DB + '.REPORTING.NCFTA_APP'
AUDIT = DB + '.AUDIT_LAYER.NCFTA_LOAD_AUDIT'


def publish(curated, raw):
    """Inputs must first pass validate_publication; all values keep source evidence."""
    require(curated['TRANSFORM_VERSION'] == TRANSFORM_VERSION, 'TRANSFORM_VERSION_MISMATCH')
    require(curated['RESULT_ID'] == raw['RESULT_ID'], 'APP_RAW_JOIN')
    require(isinstance(curated['REVIEW_STATUS'],str) and bool(curated['REVIEW_STATUS']), 'REVIEW_STATUS')
    technical = {c['name'] for c in APP_SCHEMA} - set(BUSINESS_NAMES)
    out = {k:curated[k] for k in technical if k in curated}
    out.update(APP_ROW_ID=uid(curated['CURATED_ROW_ID']+':'+APP_MAPPING_VERSION),
               APP_MAPPING_VERSION=APP_MAPPING_VERSION, FIELD_PROVENANCE={}, WARNINGS=list(curated['WARNINGS']))
    evaluated = curated['OUTCOME_STATUS']=='predicted' and not curated['IS_DELETED']
    for name in BUSINESS_NAMES:
        out[name]=[] if evaluated else None
        out['FIELD_PROVENANCE'][name]=[]

    def append(name, value, source_key, kind, start, end, prediction_index=None):
        require(isinstance(value,str), 'APP_VALUE_TYPE')
        out['FIELD_PROVENANCE'][name].append(dict(value_index=len(out[name]), source_key=source_key,
            value_kind=kind, value_start=start, value_end=end, prediction_index=prediction_index))
        out[name].append(value)

    if evaluated:
        accepted = sorted([(i,s) for i,s in enumerate(raw['PREDICTION']['spans']) if s['accepted']],
                          key=lambda pair:(pair[1]['start'],pair[1]['end'],pair[0]))
        for dest,source in DIRECT_MAP.items():
            if dest=='SOURCE_IP' and raw['SOURCE_TYPE']!='ip-src':
                continue
            for index,span in accepted:
                if span['field']==source:
                    append(dest,span['text'],source,'model_span',span['value_start'],span['value_end'],index)
        for index,span in accepted:
            if span['field'] in ('url','domain'):
                append('URL',span['text'],span['field'],span['field'],span['value_start'],span['value_end'],index)
        value = raw['SOURCE_VALUE']
        group = raw['EVENT_SHARING_GROUP_ID']
        comment = raw['SOURCE_TYPE']=='comment'
        if comment and raw['SOURCE_CATEGORY']=='Other' and group in ('6','7','8') and isinstance(value,str):
            append('COMMENTS',value,'attribute.value','raw_comment',0,len(value))
        # Exact source row shown in the mapping sheet: group 8, comment/Targeting data.
        raw_category = comment and raw['SOURCE_CATEGORY']=='Targeting data' and group=='8' and isinstance(value,str)
        if raw_category:
            append('MERCHANT_CATEGORY',value,'attribute.value','raw_category_text',0,len(value))
        else:
            for index,span in accepted:
                if span['field']!='merchant_category_code':
                    continue
                label=raw['SOURCE_RECORD']['source_row']['source_comment'].strip().casefold()
                prefix=value[:span['value_start']] if isinstance(value,str) else ''
                explicit=(label in ('mcc','merchant category code') or
                          re.search(r'\b(?:mcc|merchant category code)\s*[:=#-]?\s*$',prefix,re.I) is not None)
                if re.fullmatch(r'[0-9]{4}',span['text']) and explicit:
                    append('MERCHANT_CATEGORY',span['text'],'merchant_category_code','mcc',span['value_start'],span['value_end'],index)
                else:
                    out['WARNINGS'].append('MCC prediction withheld: explicit code evidence required')
    require(set(out)=={c['name'] for c in APP_SCHEMA}-{'PUBLISHED_BY_LOAD_RUN_ID','PUBLISHED_AT'}, 'APP_SCHEMA_MISMATCH')
    return out


def validate_publication(batch, events, results, curated):
    expected = validate_raw(batch,events,results)
    index={r['RESULT_ID']:r for r in curated}
    require(len(index)==len(curated), 'DUPLICATE_CURATED_RESULT')
    require(set(index)=={r['RESULT_ID'] for r in expected}, 'INCOMPLETE_CURATED_BATCH')
    raw_index={r['RESULT_ID']:r for r in results}
    rows=[]
    for row in expected:
        saved=index[row['RESULT_ID']]
        require(all(saved.get(k)==v for k,v in row.items() if k!='REVIEW_STATUS'), 'CURATED_EVIDENCE_CONFLICT')
        rows.append(publish(saved,raw_index[row['RESULT_ID']]))
    return rows


def run(session, source_batch_id, load_run_id):
    def sql(q,p=None):return session.sql(q,params=p or []).collect()
    require(sql('SELECT CURRENT_TRANSACTION()')[0][0] is None,'CALL_OUTSIDE_TRANSACTION')
    for value in (source_batch_id,load_run_id):
        require(isinstance(value,str) and str(uuid.UUID(value))==value,'INVALID_UUID')
    require(not sql(f"SELECT 1 FROM {AUDIT} WHERE LOAD_RUN_ID=? AND STEP_NAME='PUBLISH_APP' LIMIT 1",[load_run_id]),'PUBLISH_RUN_ID_REUSED')
    aid=str(uuid.uuid4());in_tx=False;audit_ready=False
    expected=read=None;inserted=unchanged=0;query_id=None
    def finish(status,error=None):
        details=encoded({'mapping_sha256':MAPPING_SHA256,'mapping_version':APP_MAPPING_VERSION,
                         'review_status_policy':'snapshot at initial publication; no silent retry update'})
        sql(f'''UPDATE {AUDIT} SET STATUS=?,FINISHED_AT=CURRENT_TIMESTAMP(),LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP(),
            ELAPSED_MS=DATEDIFF('millisecond',STARTED_AT,CURRENT_TIMESTAMP()),ROWS_EXPECTED=?,ROWS_READ=?,
            ROWS_INSERTED=?,ROWS_UPDATED=0,ROWS_UNCHANGED=?,ERROR_CODE=?,ERROR_MESSAGE=?,QUERY_ID=?,DETAILS=PARSE_JSON(?)
            WHERE AUDIT_ID=?''',[status,expected,read,inserted if not error else 0,unchanged if not error else 0,
                error,'App publication failed; source values withheld.' if error else None,query_id,details,aid])
    try:
        sql('BEGIN TRANSACTION');in_tx=True
        sql(f'''INSERT INTO {AUDIT} (AUDIT_ID,LOAD_RUN_ID,SOURCE_BATCH_ID,STEP_NAME,STATUS,TARGET_TABLE,
            REQUESTED_TARGET,TRANSFORM_VERSION,APP_MAPPING_VERSION,LOADER_VERSION,SNOWFLAKE_ACCOUNT,SNOWFLAKE_USER,
            SNOWFLAKE_ROLE,SNOWFLAKE_WAREHOUSE,LAST_HEARTBEAT_AT)
            SELECT ?,?,?,'PUBLISH_APP','RUNNING','REPORTING.NCFTA_APP','APP',?,?,'app-v1',
            CURRENT_ACCOUNT(),CURRENT_USER(),CURRENT_ROLE(),CURRENT_WAREHOUSE(),CURRENT_TIMESTAMP()''',
            [aid,load_run_id,source_batch_id,TRANSFORM_VERSION,APP_MAPPING_VERSION])
        sql('COMMIT');in_tx=False;audit_ready=True
        def load(table, refined=False):
            params=[source_batch_id]
            query=f'SELECT TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*)) FROM {DB}.{table} WHERE SOURCE_BATCH_ID=?'
            if refined:
                query+=' AND TRANSFORM_VERSION=?';params.append(TRANSFORM_VERSION)
            rows=[];size=0
            for record in session.sql(query,params=params).to_local_iterator():
                size+=len(record[0].encode('utf-8'))
                require(size<=MAX_BYTES and len(rows)<MAX_ROWS,'APP_INPUT_TOO_LARGE')
                rows.append(json.loads(record[0]))
            return rows
        batches=load('RAW.NCFTA_BATCHES')
        require(len(batches)==1,'BATCH_NOT_UNIQUE_OR_MISSING')
        batch=batches[0];expected=batch['EXPECTED_PREDICTION_RECORDS']
        events=load('RAW.NCFTA_EVENTS');results=load('RAW.NCFTA_MODEL_RESULTS')
        curated=load('REFINED.NCFTA_FIELDS',True);read=len(curated)
        rows=validate_publication(batch,events,results,curated)
        sql(f'''UPDATE {AUDIT} SET SOURCE_RUN_NAME=?,SOURCE_INSTANCE_ID=?,MODEL_SHA256=?,SOURCE_DATE_FROM=?,
            SOURCE_DATE_TO=?,SOURCE_GROUP_IDS=PARSE_JSON(?)::ARRAY,LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP()
            WHERE AUDIT_ID=?''',[batch['SOURCE_RUN_NAME'],batch['SOURCE_INSTANCE_ID'],batch['MODEL_SHA256'],
                batch['SOURCE_DATE_FROM'],batch['SOURCE_DATE_TO'],encoded(batch['SOURCE_GROUP_IDS']),aid])
        sql('BEGIN TRANSACTION');in_tx=True
        stored=sql(f'''SELECT TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*)) FROM {TARGET}
            WHERE SOURCE_BATCH_ID=? AND TRANSFORM_VERSION=? AND APP_MAPPING_VERSION=?''',
            [source_batch_id,TRANSFORM_VERSION,APP_MAPPING_VERSION])
        existing={}
        for record in stored:
            value=json.loads(record[0]);key=value['CURATED_ROW_ID']
            require(key not in existing,'APP_DUPLICATES');existing[key]=value
        require(set(existing)<={row['CURATED_ROW_ID'] for row in rows},'ORPHAN_APP_ROW')
        for row in rows:
            old=existing.get(row['CURATED_ROW_ID'])
            if old is not None:
                require(all(old.get(k)==v for k,v in row.items() if k!='REVIEW_STATUS'),'APP_CONTENT_CONFLICT')
                require(old.get('REVIEW_STATUS')==row['REVIEW_STATUS'],'APP_REVIEW_STATUS_CHANGED')
                unchanged+=1;continue
            values=dict(row,PUBLISHED_BY_LOAD_RUN_ID=load_run_id)
            columns=[c for c in APP_SCHEMA if c['name']!='PUBLISHED_AT']
            expressions=[f"IFF(IS_NULL_VALUE(P:{c['name']}),NULL,P:{c['name']}::{c['data_type']})" for c in columns]
            sql(f"INSERT INTO {TARGET} ({','.join(c['name'] for c in columns)}) SELECT {','.join(expressions)} FROM (SELECT PARSE_JSON(?) P)",[encoded(values)])
            inserted+=1
        query_id=sql('SELECT LAST_QUERY_ID()')[0][0]
        status='SUCCESS' if inserted else 'NO_OP';finish(status)
        sql('COMMIT');in_tx=False
        return {'status':status,'source_batch_id':source_batch_id,'load_run_id':load_run_id,
                'inserted':inserted,'unchanged':unchanged,'app_mapping_version':APP_MAPPING_VERSION}
    except Exception as exc:
        if in_tx:sql('ROLLBACK');in_tx=False
        code=str(exc) if isinstance(exc,Invalid) else 'DATABASE_OR_VALIDATION_ERROR'
        if not audit_ready:
            return {'status':'FAILED','error_code':code,'audit_status':'WRITE_FAILED','load_run_id':load_run_id}
        try:
            sql('BEGIN TRANSACTION');finish('FAILED',code);sql('COMMIT')
        except Exception:
            try:sql('ROLLBACK')
            except Exception:pass
            return {'status':'FAILED','error_code':code,'audit_status':'WRITE_FAILED','load_run_id':load_run_id}
        return {'status':'FAILED','error_code':code,'inserted':0,'load_run_id':load_run_id}
$$;
