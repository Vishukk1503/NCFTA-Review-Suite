"""Publish the approved app mapping without reclassifying or merging attributes."""
import json
import re
import uuid
from raw_loader import encoded, uid, require, Invalid, DB, MAX_ROWS, MAX_BYTES
from refined_loader import validate_raw, TRANSFORM_VERSION
from app_contract import APP_SCHEMA, BUSINESS_NAMES, DIRECT_MAP, MAPPING_SHA256

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
