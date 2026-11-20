import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).parent/'snowflake'))
import refined_loader as f
import raw_loader as r
import build_refined_procedure as build
from test_raw_loader import fixture, Frame


def dataset(value='001234 001234',deleted=None):
    b,e,p=fixture(value,deleted)
    b['CATALOG_SHA256']=f.CATALOG_SHA256
    if deleted is None and isinstance(value,str) and value:
        offset=p[0][0]['source_record']['value_offset']
        p[0][0]['prediction']['spans']=[dict(field='bank_account_number',text=value[:6],start=offset,end=offset+6,
                                           value_start=0,value_end=6,accepted=True,confidence=.95)]
    return r.prepare(b,e,p)


class Session:
    """Behavioral double for transaction tests; not a Snowflake SQL compiler."""
    def __init__(self,b,e,p):
        self.tables={'RAW.NCFTA_BATCHES':[b],'RAW.NCFTA_EVENTS':e,'RAW.NCFTA_MODEL_RESULTS':p,'REFINED.NCFTA_FIELDS':[]}
        self.audits={};self.snapshot=None;self.fail=False
    def sql(self,q,params=None):
        p=params or []
        if q=='SELECT CURRENT_TRANSACTION()':return Frame([(None,)])
        if q=='SELECT LAST_QUERY_ID()':return Frame([('query-test',)])
        if q=='BEGIN TRANSACTION':
            assert self.snapshot is None
            self.snapshot=copy.deepcopy((self.tables,self.audits));return Frame([])
        if q=='COMMIT':self.snapshot=None;return Frame([])
        if q=='ROLLBACK':
            if self.snapshot:self.tables,self.audits=self.snapshot
            self.snapshot=None;return Frame([])
        if q.startswith('SELECT 1 FROM'):
            return Frame([(1,)] if any(a['run']==p[0] for a in self.audits.values()) else [])
        if q.startswith('INSERT INTO '+f.AUDIT):
            self.audits[p[0]]={'run':p[1],'status':'RUNNING'};return Frame([])
        if q.startswith('UPDATE '+f.AUDIT):
            if 'SET STATUS=' in q:self.audits[p[-1]].update(status=p[0],inserted=p[3],unchanged=p[4],error=p[5])
            return Frame([])
        if q.startswith('SELECT TO_JSON'):
            table=next(t for t in self.tables if f'{f.DB}.{t} ' in q)
            rows=[v for v in self.tables[table] if v['SOURCE_BATCH_ID']==p[0]]
            if len(p)>1:rows=[v for v in rows if v['TRANSFORM_VERSION']==p[1]]
            return Frame([(json.dumps(v),) for v in rows])
        if q.startswith('INSERT INTO '+f.TARGET):
            if self.fail:raise RuntimeError('PRIVATE ACCOUNT VALUE')
            self.tables['REFINED.NCFTA_FIELDS'].append(json.loads(p[0]));return Frame([])
        raise AssertionError(q)


class Tests(unittest.TestCase):
    def test_all_columns_and_repeated_order(self):
        b,e,rows=dataset();row=rows[0];p=row['PREDICTION'];s=copy.deepcopy(p['spans'][0])
        s.update(start=s['start']+7,end=s['end']+7,value_start=7,value_end=13)
        p['spans'].insert(0,s)
        out=f.transform(b,row)
        self.assertEqual(out['BANK_ACCOUNT_NUMBER'],['001234','001234'])
        self.assertEqual(out['ACCEPTED_SPAN_COUNT'],2)
        self.assertEqual(out['MULTIVALUE_FIELDS'],['bank_account_number'])
        self.assertEqual(out['BANK_NAME'],[])
        self.assertEqual(set(out),{c['name'] for c in f.SCHEMA}-{'CURATED_AT','CREATED_BY_LOAD_RUN_ID'})

    def test_rejected_predictions_never_promoted(self):
        b,e,rows=dataset();rows[0]['PREDICTION']['spans'].append({'field':'unknown','accepted':False,'text':'x'})
        out=f.transform(b,rows[0])
        self.assertEqual(out['REJECTED_SPAN_COUNT'],1)
        self.assertEqual(out['ACCEPTED_SPAN_COUNT'],1)

    def test_invalid_accepted_spans_fail(self):
        for key,value in [('field','unknown'),('text','invented'),('start',0),('value_start',1),('accepted','true')]:
            b,e,rows=dataset();rows[0]['PREDICTION']['spans'][0][key]=value
            with self.subTest(key=key),self.assertRaises(r.Invalid):f.transform(b,rows[0])

    def test_deleted_arrays_null_and_metadata_preserved(self):
        b,e,rows=dataset(deleted=True)
        rows[0]['SOURCE_RECORD'].update(first_seen='2026-11-01T08:00:00',last_seen=None)
        out=f.transform(b,rows[0])
        self.assertTrue(out['IS_DELETED'])
        self.assertTrue(all(out[k.upper()] is None for k in f.MODEL_FIELDS))
        self.assertEqual(out['OBSERVATION_FIRST_SEEN'],'2026-11-01T08:00:00')
        self.assertIsNone(out['OBSERVATION_LAST_SEEN'])

    def test_catalog_drift_rejected(self):
        b,e,rows=dataset();b['CATALOG_SHA256']='0'*64
        with self.assertRaisesRegex(r.Invalid,'CATALOG_MISMATCH'):f.transform(b,rows[0])

    def test_raw_tampering_detected(self):
        for kind in ('value','hash','id'):
            b,e,rows=dataset()
            if kind=='value':rows[0]['SOURCE_VALUE']='changed'
            if kind=='hash':rows[0]['SOURCE_ATTRIBUTE_SHA256']='0'*64
            if kind=='id':rows[0]['RESULT_ID']=str(r.uuid.uuid4())
            with self.subTest(kind=kind),self.assertRaises(r.Invalid):f.validate_raw(b,e,rows)

    def test_success_retry_preserves_review(self):
        b,e,rows=dataset();s=Session(b,e,rows)
        first=f.run(s,b['SOURCE_BATCH_ID'],str(r.uuid.uuid4()))
        self.assertEqual(first['status'],'SUCCESS')
        stored=s.tables['REFINED.NCFTA_FIELDS'];stored[0]['REVIEW_STATUS']='REVIEWED'
        second=f.run(s,b['SOURCE_BATCH_ID'],str(r.uuid.uuid4()))
        self.assertEqual(second['status'],'NO_OP')
        self.assertEqual(len(stored),1)
        self.assertEqual(stored[0]['REVIEW_STATUS'],'REVIEWED')

    def test_conflict_fails_without_overwriting(self):
        b,e,rows=dataset();s=Session(b,e,rows)
        f.run(s,b['SOURCE_BATCH_ID'],str(r.uuid.uuid4()))
        s.tables['REFINED.NCFTA_FIELDS'][0]['BANK_ACCOUNT_NUMBER']=['changed']
        out=f.run(s,b['SOURCE_BATCH_ID'],str(r.uuid.uuid4()))
        self.assertEqual(out['error_code'],'CURATED_CONTENT_CONFLICT')
        self.assertEqual(s.tables['REFINED.NCFTA_FIELDS'][0]['BANK_ACCOUNT_NUMBER'],['changed'])

    def test_failure_rollback_audit_and_sanitization(self):
        b,e,rows=dataset();s=Session(b,e,rows);s.fail=True
        out=f.run(s,b['SOURCE_BATCH_ID'],str(r.uuid.uuid4()))
        self.assertEqual(out['status'],'FAILED')
        self.assertEqual(s.tables['REFINED.NCFTA_FIELDS'],[])
        self.assertTrue(all(a['status']=='FAILED' and a['inserted']==0 for a in s.audits.values()))
        self.assertNotIn('PRIVATE',json.dumps(out)+json.dumps(s.audits))

    def test_generated_sql_embeds_exact_handler(self):
        contract,ddl=build.artifacts()
        self.assertEqual((Path(__file__).parent/'snowflake/06_populate_refined.sql').read_text(),ddl)
        compile(ddl.split('AS $$\n',1)[1].rsplit('$$;',1)[0],'<embedded handler>','exec')
        self.assertEqual((Path(__file__).parent/'snowflake/refined_contract.py').read_text(),contract)


if __name__=='__main__':unittest.main()
