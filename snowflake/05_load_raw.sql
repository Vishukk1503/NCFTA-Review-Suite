-- RAW loader v1. Prepared locally; requires Snowflake integration validation.
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


def run(session, batch, files, load_run_id):
    """Single-writer PoC. Call outside any transaction; JSON results are status contracts."""
    def sql(query, params=None):
        return session.sql(query, params=params or []).collect()

    require(sql('SELECT CURRENT_TRANSACTION()')[0][0] is None, 'CALL_OUTSIDE_TRANSACTION')
    require(isinstance(load_run_id, str) and str(uuid.UUID(load_run_id)) == load_run_id, 'LOAD_RUN_ID')
    # The suite uploads files before calling RAW and logs those under this run ID.
    require(not sql(f"SELECT 1 FROM {AUDIT} WHERE LOAD_RUN_ID=? AND STEP_NAME <> 'UPLOAD' LIMIT 1", [load_run_id]), 'LOAD_RUN_ID_REUSED')
    audit_ids = {}
    transaction = False
    committed = False
    audit_started = False
    step = 'VALIDATE'
    b = None
    counts = {}
    query_id = None
    def audit_start():
        sql('BEGIN TRANSACTION')
        try:
            for name in ('RUN', 'LOAD_RAW_EVENTS', 'LOAD_RAW_RESULTS'):
                aid = str(uuid.uuid4())
                audit_ids[name] = aid
                sql(f'''INSERT INTO {AUDIT}
                    (AUDIT_ID,LOAD_RUN_ID,STEP_NAME,STATUS,REQUESTED_TARGET,LOADER_VERSION,
                     SNOWFLAKE_ACCOUNT,SNOWFLAKE_USER,SNOWFLAKE_ROLE,SNOWFLAKE_WAREHOUSE,LAST_HEARTBEAT_AT)
                    SELECT ?,?,?,'RUNNING','RAW','raw-v1',CURRENT_ACCOUNT(),CURRENT_USER(),CURRENT_ROLE(),CURRENT_WAREHOUSE(),CURRENT_TIMESTAMP()''',
                    [aid, load_run_id, name])
            sql('COMMIT')
        except Exception:
            sql('ROLLBACK')
            raise

    def audit_end(status, error=None):
        for name, aid in audit_ids.items():
            n = counts.get(name)
            inserted = n if status == 'SUCCESS' else 0
            unchanged = n if status == 'NO_OP' else 0
            details = encoded({'procedure_version': 'raw-v1', 'failure_step': step if error else None,
                               'committed': status in ('SUCCESS','NO_OP')})
            sql(f'''UPDATE {AUDIT} SET STATUS=?,FINISHED_AT=CURRENT_TIMESTAMP(),LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP(),
                ELAPSED_MS=DATEDIFF('millisecond',STARTED_AT,CURRENT_TIMESTAMP()),
                SOURCE_BATCH_ID=?,ROWS_EXPECTED=?,ROWS_READ=?,ROWS_INSERTED=?,ROWS_UPDATED=?,ROWS_UNCHANGED=?,
                ERROR_CODE=?,ERROR_MESSAGE=?,DETAILS=PARSE_JSON(?),QUERY_ID=?,
                SOURCE_RUN_NAME=?,SOURCE_INSTANCE_ID=?,SOURCE_DATE_FROM=?,SOURCE_DATE_TO=?,
                SOURCE_GROUP_IDS=PARSE_JSON(?)::ARRAY,MODEL_SHA256=?,TARGET_TABLE=? WHERE AUDIT_ID=?''',
                [status, b['SOURCE_BATCH_ID'] if b else None, n, n, inserted if n is not None else None,
                 0 if n is not None else None, unchanged if n is not None else None,
                 error, 'RAW load failed; source values withheld.' if error else None, details, query_id,
                 b.get('SOURCE_RUN_NAME') if b else None, b.get('SOURCE_INSTANCE_ID') if b else None,
                 b.get('SOURCE_DATE_FROM') if b else None, b.get('SOURCE_DATE_TO') if b else None,
                 encoded(b['SOURCE_GROUP_IDS']) if b else None, b.get('MODEL_SHA256') if b else None,
                 {'LOAD_RAW_EVENTS':'RAW.NCFTA_EVENTS','LOAD_RAW_RESULTS':'RAW.NCFTA_MODEL_RESULTS'}.get(name), aid])

    try:
        audit_start()
        audit_started = True
        b = metadata(batch)
        validate_files(files, b['SOURCE_BATCH_ID'])
        groups = {'events': [], 'results': []}
        total_bytes = 0
        for f in files:
            step = 'READ_' + f['kind'].upper()
            # Only validated path characters enter SQL. Exact filename also filters prefix matches.
            query = f'''SELECT $1::VARCHAR, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER, METADATA$FILE_CONTENT_KEY
                FROM @{STAGE}/{f['path']} (FILE_FORMAT => '{DB}.INTERNAL_STAGE.NCFTA_JSONL_FF')
                WHERE METADATA$FILENAME = ? ORDER BY METADATA$FILE_ROW_NUMBER'''
            checksum = hashlib.sha256()
            count = 0
            for row in session.sql(query, params=[f['path']]).to_local_iterator():
                value = json.loads(row[0])
                line = (encoded(value) + '\n').encode('utf-8')
                total_bytes += len(line)
                require(total_bytes <= MAX_BYTES, 'PACKAGE_TOO_LARGE')
                checksum.update(line)
                count += 1
                require(count <= f['rows'], 'FILE_ROW_COUNT')
                groups[f['kind']].append((value, dict(STAGED_FILENAME=row[1], FILE_RECORD_NUMBER=row[2], FILE_CONTENT_KEY=row[3])))
                require(sum(len(g) for g in groups.values()) <= MAX_ROWS, 'PACKAGE_TOO_MANY_ROWS')
            require(count == f['rows'] and checksum.hexdigest() == f['canonical_sha256'], 'FILE_INTEGRITY')
        step = 'VALIDATE'
        b, events, results = prepare(batch, groups['events'], groups['results'])
        counts = {'LOAD_RAW_EVENTS': len(events), 'LOAD_RAW_RESULTS': len(results)}
        # All package reads and validation finish before the atomic data transaction.
        sql('BEGIN TRANSACTION')
        transaction = True
        old = sql(f'SELECT SOURCE_BATCH_ID FROM {DB}.RAW.NCFTA_BATCHES WHERE BATCH_FINGERPRINT=? OR SOURCE_BATCH_ID=?',
                  [b['BATCH_FINGERPRINT'], b['SOURCE_BATCH_ID']])
        require(len(old) <= 1, 'DUPLICATE_BATCH_REGISTRATION')
        tables = [('NCFTA_BATCHES', [b]), ('NCFTA_EVENTS', events), ('NCFTA_MODEL_RESULTS', results)]
        if old:
            step = 'RETRY_COMPARE'
            require(old[0][0] == b['SOURCE_BATCH_ID'], 'BATCH_ID_CONFLICT')
            for table, expected in tables:
                actual = sql(f'SELECT TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*)) FROM {DB}.RAW.{table} WHERE SOURCE_BATCH_ID=?', [b['SOURCE_BATCH_ID']])
                require(len(actual) == len(expected), 'EXISTING_ROW_COUNT')
                key = {'NCFTA_BATCHES':'SOURCE_BATCH_ID','NCFTA_EVENTS':'EVENT_SNAPSHOT_ID','NCFTA_MODEL_RESULTS':'RESULT_ID'}[table]
                indexed = {json.loads(a[0])[key]: json.loads(a[0]) for a in actual}
                require(len(indexed) == len(actual), 'EXISTING_DUPLICATES')
                for row in expected:
                    saved = indexed.get(row[key], {})
                    # Packaging location may change on retry; immutable evidence may not.
                    check = {k:v for k,v in row.items() if k not in ('STAGED_FILENAME','FILE_RECORD_NUMBER','FILE_CONTENT_KEY')}
                    require(all(saved.get(k) == v for k,v in check.items()), 'EXISTING_CONTENT_CONFLICT')
            status = 'NO_OP'
        else:
            for table, rows in tables:
                step = 'INSERT_' + table
                # Reject orphan rows left by an external/manual writer.
                require(not sql(f'SELECT 1 FROM {DB}.RAW.{table} WHERE SOURCE_BATCH_ID=? LIMIT 1', [b['SOURCE_BATCH_ID']]), 'ORPHAN_ROWS')
                for row in rows:
                    values = dict(row, FIRST_LOAD_RUN_ID=load_run_id)
                    columns = list(values)
                    projections = []
                    for k in columns:
                        if k in ('SOURCE_MANIFEST','EVENT_PAYLOAD','SOURCE_RECORD','PREDICTION'):
                            projections.append('P:' + k)
                        elif k == 'SOURCE_GROUP_IDS':
                            projections.append('P:' + k + '::ARRAY')
                        elif k.startswith('EXPECTED_') or k == 'FILE_RECORD_NUMBER':
                            projections.append('P:' + k + '::NUMBER(38,0)')
                        elif k in ('SOURCE_ATTRIBUTE_DELETED','SOURCE_OBJECT_DELETED','IS_DELETED'):
                            projections.append('IFF(IS_NULL_VALUE(P:' + k + '),NULL,P:' + k + '::BOOLEAN)')
                        elif k == 'THRESHOLD':
                            projections.append('P:' + k + '::FLOAT')
                        else:
                            projections.append('IFF(IS_NULL_VALUE(P:' + k + '),NULL,P:' + k + '::VARCHAR)')
                    sql(f"INSERT INTO {DB}.RAW.{table} ({','.join(columns)}) SELECT {','.join(projections)} FROM (SELECT PARSE_JSON(?) P)", [encoded(values)])
            status = 'SUCCESS'
        query_id = sql('SELECT LAST_QUERY_ID()')[0][0]
        audit_end(status)
        sql('COMMIT')
        transaction = False
        committed = True
        return {'status':status, 'load_run_id':load_run_id, 'source_batch_id':b['SOURCE_BATCH_ID'],
                'events':len(events), 'results':len(results), 'inserted':sum(counts.values()) if status == 'SUCCESS' else 0}
    except Exception as exc:
        if transaction:
            sql('ROLLBACK')
        code = str(exc) if isinstance(exc, Invalid) else 'DATABASE_OR_PACKAGE_ERROR'
        if not audit_started:
            return {'status':'FAILED', 'error_code':code, 'audit_status':'WRITE_FAILED', 'load_run_id':load_run_id}
        # Never expose database exception text: it can contain source values.
        try:
            sql('BEGIN TRANSACTION')
            audit_end('FAILED', code)
            sql('COMMIT')
        except Exception:
            try:
                sql('ROLLBACK')
            except Exception:
                pass
            return {'status':'FAILED', 'error_code':code, 'audit_status':'WRITE_FAILED', 'load_run_id':load_run_id}
        return {'status':'FAILED', 'error_code':code, 'committed':committed, 'load_run_id':load_run_id}

$$;
