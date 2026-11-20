"""Validate saved evidence and populate REFINED; never run a model."""
import json
import uuid
from raw_loader import restore_evidence, prepare, encoded, digest, uid, require, Invalid, DB, MAX_ROWS, MAX_BYTES
from refined_contract import SCHEMA, CATALOG_SHA256

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
    batch = restore_evidence(batch)
    events = [restore_evidence(r) for r in events]
    results = [restore_evidence(r) for r in results]
    info = {k:v for k,v in batch.items() if k not in ('SOURCE_BATCH_ID','BATCH_FINGERPRINT','FIRST_LOAD_RUN_ID','REGISTERED_AT','SOURCE_MANIFEST_JSON')}
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


def run(session, source_batch_id, load_run_id):
    def sql(q,p=None): return session.sql(q,params=p or []).collect()
    require(sql('SELECT CURRENT_TRANSACTION()')[0][0] is None, 'CALL_OUTSIDE_TRANSACTION')
    for value in (source_batch_id,load_run_id):
        require(isinstance(value,str) and str(uuid.UUID(value))==value, 'INVALID_UUID')
    require(not sql(f'SELECT 1 FROM {AUDIT} WHERE LOAD_RUN_ID=? AND STEP_NAME=\'CURATE\' LIMIT 1',[load_run_id]), 'CURATE_RUN_ID_REUSED')
    aid = str(uuid.uuid4())
    in_tx = False
    audit_ready = False
    expected = read = None
    inserted = unchanged = 0
    query_id = None
    def finish(status,error=None):
        details=encoded({'transform_version':TRANSFORM_VERSION,'accepted_spans_are_not_row_counts':True})
        sql(f'''UPDATE {AUDIT} SET STATUS=?, FINISHED_AT=CURRENT_TIMESTAMP(),LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP(),
            ELAPSED_MS=DATEDIFF('millisecond',STARTED_AT,CURRENT_TIMESTAMP()),ROWS_EXPECTED=?,ROWS_READ=?,
            ROWS_INSERTED=?,ROWS_UPDATED=0,ROWS_UNCHANGED=?,ERROR_CODE=?,ERROR_MESSAGE=?,QUERY_ID=?,DETAILS=PARSE_JSON(?)
            WHERE AUDIT_ID=?''',[status,expected,read,inserted if not error else 0,unchanged if not error else 0,
                                error,'REFINED validation or load failed; source values withheld.' if error else None,query_id,details,aid])
    try:
        sql('BEGIN TRANSACTION');in_tx=True
        sql(f'''INSERT INTO {AUDIT} (AUDIT_ID,LOAD_RUN_ID,SOURCE_BATCH_ID,STEP_NAME,STATUS,TARGET_TABLE,
            REQUESTED_TARGET,TRANSFORM_VERSION,LOADER_VERSION,SNOWFLAKE_ACCOUNT,SNOWFLAKE_USER,SNOWFLAKE_ROLE,
            SNOWFLAKE_WAREHOUSE,LAST_HEARTBEAT_AT)
            SELECT ?,?,?,'CURATE','RUNNING','REFINED.NCFTA_FIELDS','REFINED',?,'refined-v1',
            CURRENT_ACCOUNT(),CURRENT_USER(),CURRENT_ROLE(),CURRENT_WAREHOUSE(),CURRENT_TIMESTAMP()''',
            [aid,load_run_id,source_batch_id,TRANSFORM_VERSION])
        sql('COMMIT');in_tx=False;audit_ready=True
        def load(table):
            rows=[];size=0
            for record in session.sql(f'SELECT TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*)) FROM {DB}.{table} WHERE SOURCE_BATCH_ID=?',params=[source_batch_id]).to_local_iterator():
                size+=len(record[0].encode('utf-8'))
                require(size<=MAX_BYTES and len(rows)<MAX_ROWS,'RAW_BATCH_TOO_LARGE')
                rows.append(json.loads(record[0]))
            return rows
        batches=load('RAW.NCFTA_BATCHES')
        require(len(batches)==1,'BATCH_NOT_UNIQUE_OR_MISSING')
        batch=batches[0];expected=batch['EXPECTED_PREDICTION_RECORDS']
        events=load('RAW.NCFTA_EVENTS');results=load('RAW.NCFTA_MODEL_RESULTS');read=len(results)
        rows=validate_raw(batch,events,results)
        sql(f'''UPDATE {AUDIT} SET SOURCE_RUN_NAME=?,SOURCE_INSTANCE_ID=?,MODEL_SHA256=?,
            SOURCE_DATE_FROM=?,SOURCE_DATE_TO=?,SOURCE_GROUP_IDS=PARSE_JSON(?)::ARRAY,LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP()
            WHERE AUDIT_ID=?''',[batch['SOURCE_RUN_NAME'],batch['SOURCE_INSTANCE_ID'],batch['MODEL_SHA256'],
                              batch['SOURCE_DATE_FROM'],batch['SOURCE_DATE_TO'],encoded(batch['SOURCE_GROUP_IDS']),aid])
        sql('BEGIN TRANSACTION');in_tx=True
        stored=sql(f'SELECT TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*)) FROM {TARGET} WHERE SOURCE_BATCH_ID=? AND TRANSFORM_VERSION=?',
                   [source_batch_id,TRANSFORM_VERSION])
        existing={}
        for record in stored:
            value=json.loads(record[0]);key=value['RESULT_ID']
            require(key not in existing,'CURATED_DUPLICATES')
            existing[key]=value
        require(set(existing) <= {row['RESULT_ID'] for row in rows},'ORPHAN_CURATED_ROW')
        for row in rows:
            old=existing.get(row['RESULT_ID'])
            if old is not None:
                # Review changes belong to the review workflow and are never overwritten.
                require(all(old.get(k)==v for k,v in row.items() if k!='REVIEW_STATUS'),'CURATED_CONTENT_CONFLICT')
                unchanged+=1
                continue
            values=dict(row,CREATED_BY_LOAD_RUN_ID=load_run_id)
            cols=[c for c in SCHEMA if c['name']!='CURATED_AT']
            projections=[]
            for c in cols:
                k=c['name'];kind=c['data_type']
                projections.append(f'IFF(IS_NULL_VALUE(P:{k}),NULL,P:{k}::{kind})')
            sql(f"INSERT INTO {TARGET} ({','.join(c['name'] for c in cols)}) SELECT {','.join(projections)} FROM (SELECT PARSE_JSON(?) P)",[encoded(values)])
            inserted+=1
        query_id=sql('SELECT LAST_QUERY_ID()')[0][0]
        status='SUCCESS' if inserted else 'NO_OP'
        finish(status)
        sql('COMMIT');in_tx=False
        return {'status':status,'source_batch_id':source_batch_id,'load_run_id':load_run_id,
                'inserted':inserted,'unchanged':unchanged,'transform_version':TRANSFORM_VERSION}
    except Exception as exc:
        if in_tx: sql('ROLLBACK');in_tx=False
        code=str(exc) if isinstance(exc,Invalid) else 'DATABASE_OR_VALIDATION_ERROR'
        if not audit_ready:
            return {'status':'FAILED','error_code':code,'audit_status':'WRITE_FAILED','load_run_id':load_run_id}
        try:
            sql('BEGIN TRANSACTION');finish('FAILED',code);sql('COMMIT')
        except Exception:
            try: sql('ROLLBACK')
            except Exception: pass
            return {'status':'FAILED','error_code':code,'audit_status':'WRITE_FAILED','load_run_id':load_run_id}
        return {'status':'FAILED','error_code':code,'inserted':0,'load_run_id':load_run_id}
