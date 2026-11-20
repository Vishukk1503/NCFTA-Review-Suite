import copy
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import snowflake_loader as loader
import suite
from test_raw_loader import fixture


class Console:
    def print(self,*args,**kwargs):pass


def completed_run(root):
    run=root/'run-1';run.mkdir()
    b,e,p=fixture()
    for name,contents in [('events.json',json.dumps([e[0][0]])),
                          ('attribute-records.jsonl',json.dumps(p[0][0]['source_record'])+'\n')]:
        (run/name).write_text(contents,encoding='utf-8')
    records_hash=loader.sha(run/'attribute-records.jsonl')
    identity=loader.raw.digest({'assets':b['ASSET_HASH'],'threshold':b['THRESHOLD'],'records':records_hash})
    p[0][0]['prediction']['identity']=identity
    (run/'predictions.jsonl').write_text(json.dumps(p[0][0]['prediction'])+'\n',encoding='utf-8')
    manifest=dict(run_id='run-1',phase='complete',fetch_complete=True,inference_complete=True,
                  last_workbook='review.xlsx',records=1,events=1,objects=0,records_sha256=records_hash,
                  events_sha256=loader.sha(run/'events.json'),model_sha256=b['MODEL_SHA256'],
                  asset_hash=b['ASSET_HASH'],threshold=b['THRESHOLD'],filters={'from':'2026-11-01','to':'2026-11-01','groups':[6]})
    (run/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    (run/'review.xlsx').write_bytes(b'synthetic workbook presence marker')
    return run


class Client:
    def __init__(self,package,fail=None):self.package=package;self.calls=[];self.fail=fail
    def sql(self,q):
        self.calls.append(q)
        if q.startswith('PUT'):
            if self.fail=='UPLOAD':raise loader.LoadError('Upload failed')
            return [{'status':'UPLOADED'}]
        if q.startswith('CALL'):
            step='RAW' if '.RAW.' in q else 'REFINED' if '.REFINED.' in q else 'APP'
            if step==self.fail:return [{'RESULT':json.dumps({'status':'FAILED','error_code':'TEST_FAILURE'})}]
            load_id=re.findall(r"'([0-9a-f-]{36})'",q)[-1]
            return [{'RESULT':json.dumps({'status':'SUCCESS','source_batch_id':self.package['batch_id'],
                                         'load_run_id':load_id,'inserted':1,'unchanged':0})}]
        return [{'number of rows affected':1}]


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.run=completed_run(self.root)
        self.cfg={'connection':'cdp_prd','source_instance_id':'https://misp.ncfta.net'}
        self.source={'catalog':str(loader.HERE/'assets/field-catalog.json'),'runs':str(self.root)}
    def tearDown(self):self.tmp.cleanup()
    def package(self):return loader.build_package(self.run,self.cfg,self.source)

    def test_package_reproducible_originals_unchanged(self):
        originals={p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()}
        first=self.package();second=self.package()
        self.assertEqual(first['batch_id'],second['batch_id'])
        self.assertEqual(first['files'],second['files'])
        for name,contents in originals.items():self.assertEqual((self.run/name).read_bytes(),contents)
        self.assertEqual([f['rows'] for f in first['files']],[1,1])

    def test_rejects_incomplete_and_tampered_sources(self):
        manifest=loader.read(self.run/'manifest.json');manifest['phase']='extracting'
        (self.run/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(loader.LoadError):self.package()
        manifest['phase']='complete';(self.run/'manifest.json').write_text(json.dumps(manifest))
        with (self.run/'attribute-records.jsonl').open('a') as f:f.write('\n')
        with self.assertRaisesRegex(loader.LoadError,'checksum'):self.package()

    def test_group_folder_rejected(self):
        child=self.run/'group-6';child.mkdir()
        with self.assertRaisesRegex(loader.LoadError,'parent'):loader.build_package(child,self.cfg,self.source)

    def test_wrong_prediction_identity_rejected(self):
        path=self.run/'predictions.jsonl';p=loader.read(path);p['identity']='0'*64
        path.write_text(json.dumps(p)+'\n')
        with self.assertRaisesRegex(loader.LoadError,'PREDICTION_JOIN'):self.package()

    def test_pipeline_order_and_audit(self):
        package=self.package();client=Client(package);report=self.root/'report.json'
        result=loader.load_package(package,client,Console(),report)
        self.assertEqual(result['status'],'SUCCESS')
        self.assertEqual([s['step'] for s in result['steps']],['UPLOAD','UPLOAD','RAW','REFINED','APP'])
        calls=[q for q in client.calls if q.startswith('CALL')]
        self.assertEqual(len(calls),3)
        self.assertTrue(client.calls[-1].startswith('MERGE INTO'))
        self.assertFalse(any(q.startswith(('DELETE','REMOVE','DROP','CREATE')) for q in client.calls))

    def test_failed_refined_stops_before_app(self):
        package=self.package();client=Client(package,'REFINED');report=self.root/'report.json'
        with self.assertRaisesRegex(loader.LoadError,'TEST_FAILURE'):loader.load_package(package,client,Console(),report)
        self.assertFalse(any(q.startswith('CALL') and '.REPORTING.' in q for q in client.calls))
        self.assertEqual(loader.read(report)['status'],'FAILED')
        self.assertEqual(loader.read(report)['current_step'],'REFINED')

    def test_upload_failure_is_logged_and_stops_calls(self):
        package=self.package();client=Client(package,'UPLOAD');report=self.root/'report.json'
        with self.assertRaises(loader.LoadError):loader.load_package(package,client,Console(),report)
        self.assertFalse(any(q.startswith('CALL') for q in client.calls))
        self.assertTrue(any("'UPLOAD'" in q and "'FAILED'" in q for q in client.calls))
        self.assertEqual(loader.read(report)['status'],'FAILED')

    def test_response_validation_and_sql_quoting(self):
        self.assertEqual(loader.literal("a'b\\c"),"'a''b\\\\c'")
        with self.assertRaises(loader.LoadError):loader.parse_output('HTML error')
        with self.assertRaises(loader.LoadError):loader.procedure_result([{'R':{'status':'SUCCESS','source_batch_id':'wrong','load_run_id':'id'}}],'batch','id')
        with self.assertRaisesRegex(loader.LoadError,'PROCEDURE_FAILED'):
            loader.procedure_result([{'R':{'status':'FAILED','error_code':'SECRET VALUE'}}],'batch','id')

    def test_menu_and_package_only_do_not_connect(self):
        with patch('rich.prompt.Prompt.ask',return_value='8'),patch('rich.console.Console.print'):
            self.assertEqual(suite.interactive(self.source),['snowflake'])
        with patch.object(loader,'configuration',return_value=self.cfg),patch.object(loader,'SnowCLI') as cli,patch.object(suite,'terminal_console',return_value=Console()):
            self.assertEqual(loader.launch(self.source,self.run,package_only=True),0)
            cli.assert_not_called()

    def test_cli_uses_browser_profile_and_temporary_sql(self):
        from unittest.mock import Mock
        process=Mock();process.communicate.return_value=('[{"OK":1}]','');process.returncode=0
        def spawn(args,**kwargs):
            self.assertIn('externalbrowser',args);self.assertIn('cdp_prd',args)
            self.assertNotIn('sensitive sql',args)
            self.assertEqual(Path(args[args.index('--filename')+1]).read_text(),'sensitive sql')
            self.filename=Path(args[args.index('--filename')+1]);return process
        with patch('shutil.which',return_value='snow.exe'),patch('subprocess.Popen',side_effect=spawn):
            self.assertEqual(loader.SnowCLI(self.cfg).sql('sensitive sql'),[{'OK':1}])
        self.assertFalse(self.filename.exists())

    def test_cli_error_does_not_expose_stderr(self):
        from unittest.mock import Mock
        process=Mock();process.communicate.return_value=('SECRET SOURCE','SECRET PASSWORD');process.returncode=1
        with patch('shutil.which',return_value='snow.exe'),patch('subprocess.Popen',return_value=process):
            with self.assertRaises(loader.LoadError) as error:loader.SnowCLI(self.cfg).sql('SELECT 1')
        self.assertNotIn('SECRET',str(error.exception))


if __name__=='__main__':unittest.main()
