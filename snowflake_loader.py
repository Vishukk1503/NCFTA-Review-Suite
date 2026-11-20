"""Optional suite uploader. Uses the installed Snowflake CLI and browser SSO."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'snowflake'))
import raw_loader as raw
from build_refined_schema import CATALOG_SHA256


class LoadError(Exception):
    pass


def need(ok,message):
    if not ok:raise LoadError(message)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def literal(value):
    # Snowflake quoted strings interpret backslashes as well as single quotes.
    return "'"+str(value).replace('\\','\\\\').replace("'","''")+"'"


def configuration():
    path=HERE/'snowflake-loader.json'
    cfg=read(path) if path.exists() else {'connection':'cdp_prd','source_instance_id':'https://misp.ncfta.net'}
    need(isinstance(cfg,dict),'Snowflake loader configuration must be an object.')
    need(set(cfg)<={'connection','source_instance_id','warehouse'},'Unknown Snowflake loader setting; do not store passwords here.')
    need(isinstance(cfg.get('connection'),str) and re.fullmatch(r'[A-Za-z0-9_-]+',cfg['connection']),'Invalid Snowflake connection profile name.')
    need(isinstance(cfg.get('source_instance_id'),str) and bool(cfg['source_instance_id']),'Set source_instance_id in snowflake-loader.json.')
    return cfg


def completed(run):
    try:
        m=read(Path(run)/'manifest.json')
        name=m.get('last_workbook','')
        return (m.get('fetch_complete') is True and m.get('inference_complete') is True
                and m.get('phase')=='complete' and isinstance(name,str) and Path(name).name==name
                and (Path(run)/name).is_file() and Path(name).suffix.lower()=='.xlsx')
    except (OSError,ValueError,TypeError):return False


def choose_run(cfg,console):
    from rich.prompt import Prompt
    from rich.table import Table
    candidates=sorted([p.parent for p in Path(cfg['runs']).glob('*/manifest.json') if completed(p.parent)],
                      key=lambda p:(p/'manifest.json').stat().st_mtime,reverse=True)[:20]
    need(candidates,'No completed extraction with an Excel workbook found. Finish extraction/export first.')
    table=Table(title='Completed runs available for Snowflake')
    for label in ('#','Run','Attributes'):table.add_column(label)
    for i,p in enumerate(candidates,1):table.add_row(str(i),p.name,str(read(p/'manifest.json').get('records',0)))
    console.print(table)
    number=Prompt.ask('Select run',choices=[str(i) for i in range(1,len(candidates)+1)],default='1',console=console)
    return candidates[int(number)-1]


def build_package(run,cfg,source_cfg):
    """Read the parent run only; never infer data from Excel or mutate its files."""
    from suite import docs_events
    run=Path(run).resolve()
    need(not (run.parent/'manifest.json').exists(),'Select the parent run, not a group subfolder.')
    need(completed(run),'Run must have completed extraction and an existing Excel export.')
    names=['manifest.json','events.json','attribute-records.jsonl','predictions.jsonl']
    need(all((run/n).is_file() for n in names),'Completed run is missing required saved JSON files.')
    need(sum((run/n).stat().st_size for n in names)<=raw.MAX_BYTES,'Run exceeds the 64 MiB PoC package limit.')
    before={n:sha(run/n) for n in names}
    m=read(run/'manifest.json')
    need(before['attribute-records.jsonl']==m.get('records_sha256'),'Saved attribute records checksum does not match the manifest.')
    event_hash=m.get('events_sha256') or m.get('source_sha256')
    need(event_hash==before['events.json'],'Saved event checksum does not match the manifest.')
    catalog=Path(source_cfg['catalog'])
    need(sha(catalog)==CATALOG_SHA256,'Field catalog does not match the deployed transformation contract.')
    events=docs_events(read(run/'events.json'))
    def lines(name):
        out=[]
        with (run/name).open(encoding='utf-8-sig') as stream:
            for line in stream:
                if line.strip():out.append(json.loads(line))
        return out
    records=lines('attribute-records.jsonl');predictions=lines('predictions.jsonl')
    need(len(events)+len(records)<=raw.MAX_ROWS,'Run exceeds the 50,000 combined-row PoC limit.')
    need(len(records)==m.get('records') and len(predictions)==len(records),'Saved extraction counts do not reconcile.')
    need(all(isinstance(p,dict) and isinstance(p.get('review_id'),str) for p in predictions),'Invalid prediction journal.')
    pred={p['review_id']:p for p in predictions}
    need(len(pred)==len(predictions),'Duplicate prediction IDs found.')
    need(all(isinstance(r,dict) and r.get('review_id') in pred for r in records),'Missing prediction for an attribute.')
    filters=m.get('filters') or {}
    groups=filters.get('groups') if filters else m.get('groups',[])
    batch=dict(SOURCE_RUN_NAME=m.get('run_id') or run.name,SOURCE_INSTANCE_ID=cfg['source_instance_id'],SOURCE_MANIFEST=m,
        SOURCE_EVENTS_SHA256=before['events.json'],SOURCE_RECORDS_SHA256=before['attribute-records.jsonl'],
        SOURCE_PREDICTIONS_SHA256=before['predictions.jsonl'],CATALOG_SHA256=CATALOG_SHA256,
        MODEL_SHA256=m.get('model_sha256'),ASSET_HASH=m.get('asset_hash'),THRESHOLD=m.get('threshold'),
        SOURCE_GROUP_IDS=sorted(set(int(g) for g in groups)),SOURCE_DATE_FROM=filters.get('from'),SOURCE_DATE_TO=filters.get('to'),
        EXPECTED_EVENTS=len(events),EXPECTED_OBJECTS=sum(len(e.get('Object') or []) for e in events),
        EXPECTED_ATTRIBUTES=len(records),EXPECTED_PREDICTION_RECORDS=len(predictions))
    batch['PREDICTION_IDENTITY']=raw.digest({'assets':batch['ASSET_HASH'],'threshold':batch['THRESHOLD'],'records':batch['SOURCE_RECORDS_SHA256']})
    results=[{'source_record':r,'prediction':pred[r['review_id']]} for r in records]
    location={'STAGED_FILENAME':'local-validation','FILE_RECORD_NUMBER':1,'FILE_CONTENT_KEY':None}
    try:b,_,_=raw.prepare(batch,[(e,location) for e in events],[(r,location) for r in results])
    except raw.Invalid as exc:raise LoadError('Package validation failed: '+str(exc)) from None
    need(all(sha(run/n)==value for n,value in before.items()),'Source files changed while preparing the package; retry after extraction stops.')
    dest=run/'snowflake-loads'/'packages'/b['SOURCE_BATCH_ID'];dest.mkdir(parents=True,exist_ok=True)
    files=[];local={};total=0
    for kind,values in [('events',events),('results',results)]:
        if not values:continue
        content=''.join(raw.encoded(v)+'\n' for v in values).encode('utf-8');total+=len(content)
        need(total<=raw.MAX_BYTES,'Generated JSON exceeds the 64 MiB PoC limit.')
        path=dest/(kind+'.jsonl');checksum=hashlib.sha256(content).hexdigest()
        if path.exists():need(sha(path)==checksum,'Existing immutable Snowflake package differs; original files were not changed.')
        else:path.write_bytes(content)
        remote=b['SOURCE_BATCH_ID']+'/'+kind+'/'+path.name
        files.append(dict(path=remote,kind=kind,rows=len(values),canonical_sha256=checksum));local[remote]=path
    (dest/'package.json').write_text(json.dumps({'batch':batch,'files':files},ensure_ascii=False,indent=2),encoding='utf-8')
    return {'batch':batch,'batch_id':b['SOURCE_BATCH_ID'],'files':files,'local':local,'folder':dest}


def parse_output(output):
    try:rows=json.loads(output)
    except (ValueError,TypeError):raise LoadError('Snowflake CLI did not return JSON. Check your CLI version and connection configuration.') from None
    need(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),'Unexpected Snowflake CLI JSON result format.')
    return rows


class SnowCLI:
    def __init__(self,cfg):
        self.cfg=cfg;self.exe=shutil.which('snow')
        need(self.exe is not None,"Snowflake CLI 'snow' was not found. Use the Windows account where your existing loader works.")
    def sql(self,query):
        # SQL goes through a file, not command-line interpolation. Never save CLI error bodies.
        fd,name=tempfile.mkstemp(prefix='ncfta-snow-',suffix='.sql')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(query)
            command=[self.exe,'sql','--connection',self.cfg['connection'],'--authenticator','externalbrowser',
                     '--database',raw.DB,'--format','JSON','--silent','--filename',name]
            if self.cfg.get('warehouse'):command+=['--warehouse',self.cfg['warehouse']]
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:out,err=process.communicate()
            except KeyboardInterrupt:
                process.terminate()
                try:process.communicate(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.communicate()
                raise
            need(process.returncode==0,'Snowflake CLI operation failed. Check login, deployed objects and privileges; source details withheld.')
            return parse_output(out)
        finally:Path(name).unlink(missing_ok=True)


def preflight(client):
    rows=client.sql('SELECT CURRENT_ACCOUNT() AS ACCOUNT_NAME,CURRENT_USER() AS USER_NAME,CURRENT_ROLE() AS ROLE_NAME,CURRENT_WAREHOUSE() AS WAREHOUSE_NAME;')
    need(len(rows)==1 and rows[0].get('ACCOUNT_NAME') and rows[0].get('WAREHOUSE_NAME'),'Snowflake login succeeded but account/warehouse context is incomplete.')
    context=rows[0]
    tables=client.sql(f"SELECT TABLE_SCHEMA,TABLE_NAME,COUNT(*) AS COLUMN_COUNT FROM {raw.DB}.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA IN ('RAW','REFINED','REPORTING','AUDIT_LAYER') GROUP BY TABLE_SCHEMA,TABLE_NAME;")
    actual={(r['TABLE_SCHEMA'],r['TABLE_NAME']):int(r['COLUMN_COUNT']) for r in tables}
    expected={('RAW','NCFTA_BATCHES'):22,('RAW','NCFTA_EVENTS'):14,('RAW','NCFTA_MODEL_RESULTS'):32,
              ('REFINED','NCFTA_FIELDS'):151,('REPORTING','NCFTA_APP'):62,('AUDIT_LAYER','NCFTA_LOAD_AUDIT'):40}
    need(all(actual.get(k)==v for k,v in expected.items()),'Deploy and verify Snowflake scripts 01–07 first: required table definitions are missing or differ.')
    procs=client.sql(f"SELECT PROCEDURE_SCHEMA,PROCEDURE_NAME FROM {raw.DB}.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_NAME IN ('SP_LOAD_NCFTA_RAW','SP_POPULATE_NCFTA_REFINED','SP_POPULATE_NCFTA_APP');")
    need({('RAW','SP_LOAD_NCFTA_RAW'),('REFINED','SP_POPULATE_NCFTA_REFINED'),('REPORTING','SP_POPULATE_NCFTA_APP')} <= {(p['PROCEDURE_SCHEMA'],p['PROCEDURE_NAME']) for p in procs},'Required Snowflake procedures are missing or not visible to your role.')
    return context


def procedure_result(rows,batch_id,load_id):
    need(len(rows)==1 and len(rows[0])==1,'Unexpected procedure response.')
    value=next(iter(rows[0].values()))
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:raise LoadError('Procedure response was not valid JSON.') from None
    need(isinstance(value,dict),'Procedure response was not an object.')
    if value.get('status') not in ('SUCCESS','NO_OP'):
        code=value.get('error_code','PROCEDURE_FAILED')
        code=code if isinstance(code,str) and re.fullmatch('[A-Z_]+',code) else 'PROCEDURE_FAILED'
        raise LoadError('Snowflake procedure failed: '+code)
    need(value.get('source_batch_id')==batch_id and value.get('load_run_id')==load_id,'Procedure returned a different batch/run identity.')
    return value


def load_package(package,client,console,report_path):
    load_id=str(uuid.uuid4());bid=package['batch_id'];batch=package['batch']
    report={'load_run_id':load_id,'source_batch_id':bid,'status':'RUNNING','steps':[]}
    def save():report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    def record(step,**values):report['steps'].append(dict(step=step,**values));save()
    def audit_upload(f,status):
        q=f'''INSERT INTO {raw.DB}.AUDIT_LAYER.NCFTA_LOAD_AUDIT
            (AUDIT_ID,LOAD_RUN_ID,SOURCE_BATCH_ID,STEP_NAME,FILE_NAME,FILE_SHA256,FILE_SIZE_BYTES,STATUS,FINISHED_AT,ROWS_EXPECTED,REQUESTED_TARGET,LOADER_VERSION)
            SELECT {literal(str(uuid.uuid4()))},{literal(load_id)},{literal(bid)},'UPLOAD',{literal(f['path'])},
            {literal(f['canonical_sha256'])},{package['local'][f['path']].stat().st_size},{literal(status)},CURRENT_TIMESTAMP(),{f['rows']},'APP','suite-loader-v1';'''
        client.sql(q)
    def final_audit(status):
        q=f'''MERGE INTO {raw.DB}.AUDIT_LAYER.NCFTA_LOAD_AUDIT T
            USING (SELECT {literal(load_id)} AS RUN_ID) S ON T.LOAD_RUN_ID=S.RUN_ID AND T.STEP_NAME='RUN'
            WHEN MATCHED THEN UPDATE SET STATUS={literal(status)},REQUESTED_TARGET='APP',FINISHED_AT=CURRENT_TIMESTAMP(),
                LAST_HEARTBEAT_AT=CURRENT_TIMESTAMP(),ELAPSED_MS=DATEDIFF('millisecond',STARTED_AT,CURRENT_TIMESTAMP())
            WHEN NOT MATCHED THEN INSERT (AUDIT_ID,LOAD_RUN_ID,SOURCE_BATCH_ID,STEP_NAME,STATUS,REQUESTED_TARGET,FINISHED_AT)
                VALUES ({literal(str(uuid.uuid4()))},S.RUN_ID,{literal(bid)},'RUN',{literal(status)},'APP',CURRENT_TIMESTAMP());'''
        client.sql(q)
    save()
    try:
        for f in package['files']:
            report['current_step']='UPLOAD_'+f['kind'].upper();save()
            path=package['local'][f['path']]
            need(sha(path)==f['canonical_sha256'],'Prepared upload file changed; rebuild the package.')
            console.print('Uploading '+f['kind']+' JSON…')
            stage=f"@{raw.STAGE}/{bid}/{f['kind']}/"
            try:
                rows=client.sql(f"PUT {literal('file://'+path.resolve().as_posix())} {stage} AUTO_COMPRESS=FALSE OVERWRITE=TRUE;")
                need(len(rows)==1 and str(rows[0].get('status',rows[0].get('STATUS',''))).upper() in ('UPLOADED','SKIPPED'),'Upload did not confirm a staged file.')
            except BaseException:
                try:audit_upload(f,'FAILED')
                except BaseException:report['audit_status']='UPLOAD_AUDIT_UNCONFIRMED'
                raise
            audit_upload(f,'SUCCESS');record('UPLOAD',kind=f['kind'],status='SUCCESS',rows=f['rows'])
        calls=[('RAW',f"CALL {raw.DB}.RAW.SP_LOAD_NCFTA_RAW(PARSE_JSON({literal(raw.encoded(batch))}),PARSE_JSON({literal(raw.encoded(package['files']))})::ARRAY,{literal(load_id)});"),
               ('REFINED',f"CALL {raw.DB}.REFINED.SP_POPULATE_NCFTA_REFINED({literal(bid)},{literal(load_id)});"),
               ('APP',f"CALL {raw.DB}.REPORTING.SP_POPULATE_NCFTA_APP({literal(bid)},{literal(load_id)});")]
        for name,query in calls:
            console.print('Loading '+name+'…')
            report['current_step']=name;save()
            result=procedure_result(client.sql(query),bid,load_id)
            unchanged=result.get('unchanged')
            if name=='RAW':unchanged=(result.get('events',0)+result.get('results',0)) if result['status']=='NO_OP' else 0
            record(name,status=result['status'],inserted=result.get('inserted'),unchanged=unchanged)
            if name=='RAW':
                client.sql(f"UPDATE {raw.DB}.AUDIT_LAYER.NCFTA_LOAD_AUDIT SET STATUS='RUNNING',REQUESTED_TARGET='APP',FINISHED_AT=NULL WHERE LOAD_RUN_ID={literal(load_id)} AND STEP_NAME='RUN';")
        final_audit('SUCCESS');report['status']='SUCCESS';report['current_step']='COMPLETE';save()
        # Keep staged evidence for reproducible retries; no source/archive deletion.
        return report
    except BaseException as exc:
        status='INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'FAILED'
        report['status']=status
        report['message']=str(exc) if isinstance(exc,LoadError) else 'Load stopped; source details withheld. Check audit before retrying.'
        try:final_audit(status)
        except BaseException:report['audit_status']='WRITE_FAILED_OR_UNCONFIRMED'
        save();raise


def launch(source_cfg,run=None,connection=None,package_only=False):
    import suite
    console=suite.terminal_console();report=None;report_path=None
    try:
        cfg=configuration()
        if connection:
            need(re.fullmatch(r'[A-Za-z0-9_-]+',connection),'Invalid connection profile name.')
            cfg['connection']=connection
        run=Path(run).resolve() if run else choose_run(source_cfg,console)
        with suite.lock(run):
            console.print('Validating completed extraction and preparing JSON…')
            package=build_package(run,cfg,source_cfg)
            if package_only:
                console.print('Package ready: '+str(package['folder']),markup=False);return 0
            report_path=run/'snowflake-loads'/('load-'+uuid.uuid4().hex+'.json')
            report_path.write_text(json.dumps({'status':'CONNECTING','source_batch_id':package['batch_id']}),encoding='utf-8')
            client=SnowCLI(cfg)
            console.print('Connecting through '+cfg['connection']+'. Complete sign-in in your browser; the loader will wait.',markup=False)
            context=preflight(client)
            console.print('Connected: '+str(context['ACCOUNT_NAME'])+' / '+str(context['ROLE_NAME'])+' / '+str(context['WAREHOUSE_NAME']),markup=False)
            report=load_package(package,client,console,report_path)
        from rich.table import Table
        table=Table(title='Snowflake load complete')
        for label in ('Step','Status','Inserted','Unchanged'):table.add_column(label)
        for step in report['steps']:
            table.add_row(step['step'],step['status'],str(step.get('inserted','—')),str(step.get('unchanged','—')))
        console.print(table);console.print('Audit run: '+report['load_run_id']);return 0
    except KeyboardInterrupt:
        if report_path and report_path.exists() and read(report_path).get('status')=='CONNECTING':
            report_path.write_text(json.dumps({'status':'INTERRUPTED','step':'CONNECT'}),encoding='utf-8')
        console.print('Stopped. A submitted server query may still be running; check the audit before retrying.');return 130
    except Exception as exc:
        if report_path and report_path.exists() and read(report_path).get('status')=='CONNECTING':
            report_path.write_text(json.dumps({'status':'FAILED','step':'CONNECT','message':'Login/preflight failed; source details withheld.'}),encoding='utf-8')
        console.print(str(exc) if isinstance(exc,(LoadError,suite.SuiteError)) else 'Snowflake load failed ('+type(exc).__name__+'); source details withheld.',markup=False)
        return 1
