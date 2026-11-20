import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).parent/'snowflake'))
import app_loader as app
import refined_loader as refined
import raw_loader as raw
import build_app_procedure as builder
from test_raw_loader import fixture, Frame
from test_refined_loader import Session as RefinedSession


def dataset(value='001234', spans=None, kind='other', category='Financial fraud', group='6', comment='', deleted=None):
    b,e,p=fixture(value,deleted)
    b['CATALOG_SHA256']=refined.CATALOG_SHA256
    event=e[0][0];event['sharing_group_id']=group
    a=event['Attribute'][0];a.update(type=kind,category=category,comment=comment)
    r=p[0][0]['source_record'];r['source_hash']=raw.digest(a)
    r['source_row'].update(source_type=kind,source_category=category,source_comment=comment)
    prefix=f'Source type: {kind}\nSource category: {category}\nSource comment: {comment}\nSource value:\n'
    r.update(input=prefix+value,value_offset=len(prefix))
    pred=p[0][0]['prediction'];pred['input_hash']=raw.digest(r['input'])
    pred['spans']=[dict(field=field,text=value[start:end],start=len(prefix)+start,end=len(prefix)+end,
                        value_start=start,value_end=end,accepted=True) for field,start,end in (spans or [])]
    b,events,results=raw.prepare(b,e,p)
    curated=refined.validate_raw(b,events,results)
    return b,events,results,curated


class Session(RefinedSession):
    def __init__(self,b,e,p,c):
        super().__init__(b,e,p)
        self.tables['REFINED.NCFTA_FIELDS']=c
        self.tables['REPORTING.NCFTA_APP']=[]
        self.fail_after=None
    def sql(self,q,params=None):
        p=params or []
        if q.startswith('INSERT INTO '+app.TARGET):
            rows=self.tables['REPORTING.NCFTA_APP']
            if self.fail_after is not None and len(rows)>=self.fail_after:
                raise RuntimeError('SECRET SOURCE VALUE')
            rows.append(json.loads(p[0]));return Frame([])
        if q.startswith('SELECT TO_JSON') and app.TARGET in q:
            return Frame([(json.dumps(v),) for v in self.tables['REPORTING.NCFTA_APP']
                          if v['SOURCE_BATCH_ID']==p[0] and v['TRANSFORM_VERSION']==p[1] and v['APP_MAPPING_VERSION']==p[2]])
        return super().sql(q,p)


class Tests(unittest.TestCase):
    def publish(self,**kw):
        b,e,r,c=dataset(**kw)
        return app.validate_publication(b,e,r,c)[0]

    def test_all_29_fields_and_62_column_contract(self):
        out=self.publish(spans=[('bank_account_number',0,6)])
        self.assertEqual(out['BANK_ACCOUNT_NUMBER'],['001234'])
        self.assertEqual(out['BANK_NAME'],[])
        self.assertEqual(len(app.BUSINESS_NAMES),29)
        self.assertEqual(set(out),{c['name'] for c in app.APP_SCHEMA}-{'PUBLISHED_AT','PUBLISHED_BY_LOAD_RUN_ID'})
        self.assertEqual(len(app.APP_SCHEMA),62)

    def test_comments_use_value_not_metadata(self):
        out=self.publish(value='Original comment',kind='comment',category='Other',comment='Metadata must not become content')
        self.assertEqual(out['COMMENTS'],['Original comment'])
        self.assertEqual(out['FIELD_PROVENANCE']['COMMENTS'][0]['source_key'],'attribute.value')
        self.assertEqual(self.publish(value='Other text',kind='comment',category='Internal reference')['COMMENTS'],[])

    def test_merchant_category_exact_source_rule(self):
        out=self.publish(value='Retail',kind='comment',category='Targeting data',group='8')
        self.assertEqual(out['MERCHANT_CATEGORY'],['Retail'])
        self.assertEqual(out['FIELD_PROVENANCE']['MERCHANT_CATEGORY'][0]['value_kind'],'raw_category_text')
        self.assertEqual(self.publish(value='Retail',kind='comment',category='Targeting data',group='6')['MERCHANT_CATEGORY'],[])

    def test_mcc_requires_explicit_code_evidence(self):
        out=self.publish(value='MCC: 5411',spans=[('merchant_category_code',5,9)])
        self.assertEqual(out['MERCHANT_CATEGORY'],['5411'])
        self.assertEqual(out['FIELD_PROVENANCE']['MERCHANT_CATEGORY'][0]['value_kind'],'mcc')
        no=self.publish(value='5411',spans=[('merchant_category_code',0,4)])
        self.assertEqual(no['MERCHANT_CATEGORY'],[])
        self.assertIn('MCC prediction withheld: explicit code evidence required',no['WARNINGS'])
        self.assertEqual(self.publish(value='5411',comment='MCC',spans=[('merchant_category_code',0,4)])['MERCHANT_CATEGORY'],['5411'])

    def test_url_domain_kinds_and_source_order(self):
        value='example.test https://example.test'
        out=self.publish(value=value,spans=[('url',13,len(value)),('domain',0,12)])
        self.assertEqual(out['URL'],['example.test','https://example.test'])
        self.assertEqual([x['value_kind'] for x in out['FIELD_PROVENANCE']['URL']],['domain','url'])
        self.assertEqual([x['value_index'] for x in out['FIELD_PROVENANCE']['URL']],[0,1])

    def test_repeated_accounts_preserve_zeros_and_positions(self):
        out=self.publish(value='001234 001234',spans=[('bank_account_number',7,13),('bank_account_number',0,6)])
        self.assertEqual(out['BANK_ACCOUNT_NUMBER'],['001234','001234'])
        self.assertEqual([x['value_start'] for x in out['FIELD_PROVENANCE']['BANK_ACCOUNT_NUMBER']],[0,7])

    def test_ip_scope_and_company_roles(self):
        self.assertEqual(self.publish(value='1.2.3.4',kind='ip-dst',spans=[('source_ip',0,7)])['SOURCE_IP'],[])
        self.assertEqual(self.publish(value='1.2.3.4',kind='ip-src',spans=[('source_ip',0,7)])['SOURCE_IP'],['1.2.3.4'])
        out=self.publish(value='Acme',spans=[('company_name',0,4)])
        self.assertEqual(out['COMPANY_NAME'],['Acme'])
        self.assertEqual(out['MERCHANT_NAME'],[])
        self.assertEqual(out['TARGET_ORGANIZATION'],[])

    def test_deleted_raw_comments_are_not_published(self):
        out=self.publish(value='Original comment',kind='comment',category='Other',deleted=True)
        self.assertTrue(out['IS_DELETED'])
        self.assertTrue(all(out[k] is None for k in app.BUSINESS_NAMES))
        self.assertTrue(all(v==[] for v in out['FIELD_PROVENANCE'].values()))

    def test_missing_or_changed_refined_batch_fails(self):
        b,e,r,c=dataset(spans=[('bank_account_number',0,6)])
        with self.assertRaisesRegex(raw.Invalid,'INCOMPLETE_CURATED_BATCH'):app.validate_publication(b,e,r,[])
        c[0]['BANK_ACCOUNT_NUMBER']=['invented']
        with self.assertRaisesRegex(raw.Invalid,'CURATED_EVIDENCE_CONFLICT'):app.validate_publication(b,e,r,c)

    def test_success_retry_and_preserved_history(self):
        b,e,r,c=dataset(spans=[('bank_account_number',0,6)]);s=Session(b,e,r,c)
        first=app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        self.assertEqual(first['status'],'SUCCESS')
        second=app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        self.assertEqual(second['status'],'NO_OP')
        self.assertEqual(len(s.tables['REPORTING.NCFTA_APP']),1)
        self.assertEqual(len(s.audits),2)

    def test_conflicting_retry_not_overwritten(self):
        b,e,r,c=dataset();s=Session(b,e,r,c)
        app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        s.tables['REPORTING.NCFTA_APP'][0]['COMMENTS']=['changed']
        out=app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        self.assertEqual(out['error_code'],'APP_CONTENT_CONFLICT')
        self.assertEqual(s.tables['REPORTING.NCFTA_APP'][0]['COMMENTS'],['changed'])

    def test_review_change_requires_explicit_workflow(self):
        b,e,r,c=dataset();c[0]['REVIEW_STATUS']='REVIEWED';s=Session(b,e,r,c)
        app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        c[0]['REVIEW_STATUS']='REJECTED'
        out=app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        self.assertEqual(out['error_code'],'APP_REVIEW_STATUS_CHANGED')
        self.assertEqual(s.tables['REPORTING.NCFTA_APP'][0]['REVIEW_STATUS'],'REVIEWED')

    def test_rollback_after_first_insert_and_sanitized_audit(self):
        # Two source attributes in one event, each with its own review ID/prediction.
        b,e,r,c=dataset()
        payload=e[0]['EVENT_PAYLOAD'];second=copy.deepcopy(payload['Attribute'][0]);second['id']='4'
        payload['Attribute'].append(second)
        record=copy.deepcopy(r[0]['SOURCE_RECORD']);record.update(attribute_id='4',source_hash=raw.digest(second),review_id='R'+raw.digest(['1','direct','4'])[:20])
        pred=copy.deepcopy(r[0]['PREDICTION']);pred['review_id']=record['review_id']
        metadata={k:v for k,v in b.items() if k not in ('SOURCE_BATCH_ID','BATCH_FINGERPRINT')}
        metadata.update(EXPECTED_ATTRIBUTES=2,EXPECTED_PREDICTION_RECORDS=2)
        loc={k:e[0][k] for k in ('STAGED_FILENAME','FILE_RECORD_NUMBER','FILE_CONTENT_KEY')}
        b,e,r=raw.prepare(metadata,[(payload,loc)],[({'source_record':r[0]['SOURCE_RECORD'],'prediction':r[0]['PREDICTION']},loc),({'source_record':record,'prediction':pred},loc)])
        c=refined.validate_raw(b,e,r);s=Session(b,e,r,c);s.fail_after=1
        out=app.run(s,b['SOURCE_BATCH_ID'],str(raw.uuid.uuid4()))
        self.assertEqual(out['status'],'FAILED')
        self.assertEqual(s.tables['REPORTING.NCFTA_APP'],[])
        self.assertTrue(all(x['status']=='FAILED' and x['inserted']==0 for x in s.audits.values()))
        self.assertNotIn('SECRET',json.dumps(out)+json.dumps(s.audits))

    def test_generated_sql_and_embedded_python(self):
        contract,ddl=builder.artifacts()
        base=Path(__file__).parent/'snowflake'
        self.assertEqual((base/'07_populate_app.sql').read_text(),ddl)
        self.assertEqual((base/'app_contract.py').read_text(),contract)
        body=ddl.split('AS $$\n',1)[1].rsplit('$$;',1)[0]
        compile(body,'<app handler>','exec')
        namespace={};exec(body,namespace)
        b,e,r,c=dataset(spans=[('bank_account_number',0,6)])
        self.assertEqual(namespace['validate_publication'](b,e,r,c),app.validate_publication(b,e,r,c))


if __name__=='__main__':unittest.main()
