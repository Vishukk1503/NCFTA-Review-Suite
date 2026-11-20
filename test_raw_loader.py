import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'snowflake'))
import raw_loader as r
import build_raw_procedure


def fixture(value='001234', deleted=None):
    a = {'id':'3','type':'bank-account-nr','category':'Financial fraud','value':value}
    if deleted is not None:
        a['deleted'] = deleted
    e = {'id':'1','date':'2026-11-01','sharing_group_id':'6','Attribute':[a]}
    rid = 'R' + r.digest(['1','direct','3'])[:20]
    source = {'review_id':rid,'source_hash':r.digest(a),'input':'test input',
              'source_row':{'source_type':a['type'],'source_category':a['category'],'source_comment':'',
                            'source_value':value if isinstance(value,str) else ''},
              'deleted':r.flag(deleted) is True}
    prefix='Source type: '+a['type']+'\nSource category: '+a['category']+'\nSource comment: \nSource value:\n'
    source.update(input=prefix+source['source_row']['source_value'],value_offset=len(prefix),event_id='1',attribute_id='3')
    batch = {k:'a'*64 for k in ('SOURCE_EVENTS_SHA256','SOURCE_RECORDS_SHA256','SOURCE_PREDICTIONS_SHA256',
              'CATALOG_SHA256','MODEL_SHA256','ASSET_HASH')}
    batch.update(SOURCE_RUN_NAME='synthetic-run', SOURCE_INSTANCE_ID='synthetic',
                 SOURCE_MANIFEST={'fetch_complete':True,'inference_complete':True,'phase':'complete'},
                 THRESHOLD=.5,SOURCE_GROUP_IDS=[6],SOURCE_DATE_FROM=None,SOURCE_DATE_TO=None,
                 EXPECTED_EVENTS=1,EXPECTED_OBJECTS=0,EXPECTED_ATTRIBUTES=1,EXPECTED_PREDICTION_RECORDS=1)
    batch['PREDICTION_IDENTITY'] = r.digest({'assets':batch['ASSET_HASH'],'threshold':.5,'records':batch['SOURCE_RECORDS_SHA256']})
    p = {'review_id':rid,'input_hash':r.digest(source['input']),'identity':batch['PREDICTION_IDENTITY'],
         'status':'deleted' if source['deleted'] else 'predicted','spans':[],'warnings':[]}
    location = {'STAGED_FILENAME':'synthetic','FILE_RECORD_NUMBER':1,'FILE_CONTENT_KEY':'synthetic'}
    return batch, [(e,location)], [({'source_record':source,'prediction':p},location)]


class Frame:
    def __init__(self, rows): self.rows=rows
    def collect(self): return self.rows
    def to_local_iterator(self): return iter(self.rows)


class Session:
    """Behavioral transaction double; does not pretend to compile Snowflake SQL."""
    def __init__(self, batch, events, results):
        self.tables={t:[] for t in ('NCFTA_BATCHES','NCFTA_EVENTS','NCFTA_MODEL_RESULTS')}
        self.audits={}
        self.snapshot=None
        self.fail_results=False
        self.files=[]
        self.staged={}
        bid=r.metadata(batch)['SOURCE_BATCH_ID']
        for kind,rows in [('events',events),('results',results)]:
            path=f'{bid}/{kind}/part.jsonl'
            sha=hashlib.sha256(''.join(r.encoded(v)+'\n' for v,_ in rows).encode()).hexdigest()
            self.files.append(dict(path=path,kind=kind,rows=len(rows),canonical_sha256=sha))
            self.staged[path]=[(r.encoded(v),path,i+1,'key') for i,(v,_) in enumerate(rows)]
    def sql(self,q,params=None):
        p=params or []
        if q=='SELECT CURRENT_TRANSACTION()': return Frame([(None,)])
        if q=='SELECT LAST_QUERY_ID()': return Frame([('test-query',)])
        if q=='BEGIN TRANSACTION':
            assert self.snapshot is None
            self.snapshot=copy.deepcopy((self.tables,self.audits))
            return Frame([])
        if q=='COMMIT': self.snapshot=None; return Frame([])
        if q=='ROLLBACK':
            if self.snapshot: self.tables,self.audits=self.snapshot
            self.snapshot=None
            return Frame([])
        if 'SELECT 1 FROM' in q and r.AUDIT in q:
            return Frame([(1,)] if any(a['run']==p[0] and ("STEP_NAME <> 'UPLOAD'" not in q or a['step']!='UPLOAD') for a in self.audits.values()) else [])
        if q.startswith('INSERT INTO '+r.AUDIT):
            self.audits[p[0]]={'run':p[1],'step':p[2],'status':'RUNNING'}
            return Frame([])
        if q.startswith('UPDATE '+r.AUDIT):
            self.audits[p[-1]].update(status=p[0],inserted=p[4],error=p[7])
            return Frame([])
        if 'METADATA$FILENAME' in q: return Frame(self.staged[p[0]])
        if q.startswith('SELECT SOURCE_BATCH_ID'):
            return Frame([(x['SOURCE_BATCH_ID'],) for x in self.tables['NCFTA_BATCHES'] if x['BATCH_FINGERPRINT']==p[0] or x['SOURCE_BATCH_ID']==p[1]])
        table=next((t for t in self.tables if '.RAW.'+t in q),None)
        if q.startswith('SELECT TO_JSON'):
            return Frame([(json.dumps(x),) for x in self.tables[table] if x['SOURCE_BATCH_ID']==p[0]])
        if q.startswith('SELECT 1 FROM'):
            return Frame([(1,)] if any(x['SOURCE_BATCH_ID']==p[0] for x in self.tables[table]) else [])
        if q.startswith('INSERT INTO'):
            if table=='NCFTA_MODEL_RESULTS' and self.fail_results: raise RuntimeError('SECRET SOURCE VALUE')
            self.tables[table].append(json.loads(p[0])); return Frame([])
        raise AssertionError(q)


class Tests(unittest.TestCase):
    def test_original_text_and_nullable_flags(self):
        for value in ('001234','',None,123):
            b,e,p=fixture(value)
            result=r.prepare(b,e,p)[2][0]
            self.assertEqual(result['SOURCE_VALUE'],value if isinstance(value,str) else None)
            self.assertIsNone(result['SOURCE_ATTRIBUTE_DELETED'])
            self.assertFalse(result['IS_DELETED'])

    def test_deletion_and_prediction_conflict(self):
        b,e,p=fixture(deleted=True)
        self.assertTrue(r.prepare(b,e,p)[2][0]['IS_DELETED'])
        p[0][0]['prediction']['status']='predicted'
        with self.assertRaisesRegex(r.Invalid,'DELETED_PREDICTION'): r.prepare(b,e,p)

    def test_invalid_join_hash_count_and_value(self):
        for change in ('join','hash','count','value'):
            b,e,p=fixture()
            if change=='join': p[0][0]['prediction']['review_id']='wrong'
            if change=='hash': p[0][0]['source_record']['source_hash']='wrong'
            if change=='count': b['EXPECTED_EVENTS']=2
            if change=='value': p[0][0]['source_record']['source_row']['source_value']='wrong'
            with self.assertRaises(r.Invalid): r.prepare(b,e,p)

    def test_paths_and_duplicates(self):
        b,e,p=fixture(); s=Session(b,e,p); bid=r.metadata(b)['SOURCE_BATCH_ID']
        for bad in ('../secret.jsonl',bid+'/events/a.jsonl\';DROP TABLE X;--'):
            f=copy.deepcopy(s.files);f[0]['path']=bad
            with self.assertRaises(r.Invalid): r.validate_files(f,bid)
        with self.assertRaises(r.Invalid): r.validate_files(s.files+s.files,bid)

    def test_input_scaffold_cannot_be_changed(self):
        b,e,p=fixture()
        p[0][0]['source_record']['input']='changed'
        p[0][0]['prediction']['input_hash']=r.digest('changed')
        with self.assertRaisesRegex(r.Invalid,'INPUT_CONTEXT'): r.prepare(b,e,p)

    def test_parent_deletion(self):
        b,e,p=fixture()
        event=e[0][0];a=event.pop('Attribute')[0]
        event['Object']=[{'id':'2','deleted':True,'Attribute':[a]}]
        b['EXPECTED_OBJECTS']=1
        rid='R'+r.digest(['1','2','3'])[:20]
        p[0][0]['source_record'].update(review_id=rid,object_id='2',deleted=True)
        p[0][0]['prediction'].update(review_id=rid,status='deleted')
        row=r.prepare(b,e,p)[2][0]
        self.assertIsNone(row['SOURCE_ATTRIBUTE_DELETED'])
        self.assertTrue(row['SOURCE_OBJECT_DELETED'])
        self.assertTrue(row['IS_DELETED'])

    def test_success_then_noop(self):
        b,e,p=fixture();s=Session(b,e,p)
        self.assertEqual(r.run(s,b,s.files,str(r.uuid.uuid4()))['status'],'SUCCESS')
        self.assertEqual(r.run(s,b,s.files,str(r.uuid.uuid4()))['status'],'NO_OP')
        self.assertEqual([len(v) for v in s.tables.values()],[1,1,1])
        self.assertEqual(len(s.audits),6)

    def test_upload_audit_can_precede_raw_call(self):
        b,e,p=fixture();s=Session(b,e,p);run_id=str(r.uuid.uuid4())
        s.audits['upload']={'run':run_id,'step':'UPLOAD','status':'SUCCESS'}
        self.assertEqual(r.run(s,b,s.files,run_id)['status'],'SUCCESS')
        self.assertEqual(s.audits['upload']['status'],'SUCCESS')
        with self.assertRaisesRegex(r.Invalid,'LOAD_RUN_ID_REUSED'):r.run(s,b,s.files,run_id)

    def test_failure_rolls_back_and_sanitizes(self):
        b,e,p=fixture();s=Session(b,e,p);s.fail_results=True
        out=r.run(s,b,s.files,str(r.uuid.uuid4()))
        self.assertEqual(out['status'],'FAILED')
        self.assertFalse(any(s.tables.values()))
        self.assertTrue(all(a['status']=='FAILED' for a in s.audits.values()))
        self.assertNotIn('SECRET',json.dumps(out)+json.dumps(s.audits))

    def test_retry_detects_corruption(self):
        b,e,p=fixture();s=Session(b,e,p)
        r.run(s,b,s.files,str(r.uuid.uuid4()))
        s.tables['NCFTA_MODEL_RESULTS'][0]['SOURCE_VALUE']='corrupted'
        out=r.run(s,b,s.files,str(r.uuid.uuid4()))
        self.assertEqual(out['error_code'],'EXISTING_CONTENT_CONFLICT')
        self.assertEqual(s.tables['NCFTA_MODEL_RESULTS'][0]['SOURCE_VALUE'],'corrupted')

    def test_file_checksum_rejects_before_insert(self):
        b,e,p=fixture();s=Session(b,e,p);s.files[0]['canonical_sha256']='0'*64
        self.assertEqual(r.run(s,b,s.files,str(r.uuid.uuid4()))['error_code'],'FILE_INTEGRITY')
        self.assertFalse(any(s.tables.values()))

    def test_generated_handler_current(self):
        self.assertEqual((Path(__file__).parent/'snowflake/05_load_raw.sql').read_text(),build_raw_procedure.render())


if __name__=='__main__': unittest.main()
