import contextlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import suite

class UI:
 def update(self,*a,**kw):pass
def event(n,attrs=None,objects=None):
 return dict(id=str(n),uuid='e'+str(n),sharing_group_id='6',date='2026-11-09',Attribute=attrs or [],Object=objects or [])
class SuiteTests(unittest.TestCase):
 def test_date_formats(self):
  for value in ('2026-11-15','11/15/2026',' 11/15/2026 '):
   self.assertEqual(suite.parse_event_date(value).isoformat(),'2026-11-15')
  for value in ('02/30/2026','15/11/2026','nonsense',''):
   with self.assertRaises(suite.argparse.ArgumentTypeError):suite.parse_event_date(value)
 def test_interactive_retries_dates_and_groups(self):
  with patch('rich.prompt.Prompt.ask',side_effect=['1','bad','11/15/2026','11/14/2026','11/15/2026','9','6','auto']),patch('rich.console.Console.print'):
   args=suite.interactive({})
  self.assertEqual(args,['run','--from-date','2026-11-15','--to-date','2026-11-15','--groups','6'])
 def test_identical_inputs_reuse_predictions_without_merging_ids(self):
  from types import SimpleNamespace as NS
  from unittest.mock import Mock
  run=self.run_dir()
  events=[event(1,[dict(uuid='a',type='other',value='Example'),dict(uuid='b',type='other',value='Example'),dict(uuid='c',type='other',value='Example',comment='Different context'),dict(uuid='d',type='other',value='Example',deleted=True)],
                [dict(uuid='revoked-object',deleted=True,Attribute=[dict(uuid='e',type='other',value='Example',deleted=False)])])]
  suite.save(run/'events.json',events);rows=suite.prepare(run,events)
  m=suite.read(run/'manifest.json');m.update(fetch_complete=True,source_sha256=suite.sha(run/'events.json'));suite.save(run/'manifest.json',m)
  modeldir=Path(self.tmp.name)/'model';modeldir.mkdir();(modeldir/'model.safetensors').write_bytes(b'test')
  cfg=dict(self.cfg,model=str(modeldir))
  model=Mock();model.float.return_value=model;model.to.return_value=model;model.eval.return_value=model
  model.parameters.return_value=[];model.extract_entities.return_value={'entities':{}}
  torch=NS(set_num_threads=Mock(),manual_seed=Mock(),set_float32_matmul_precision=Mock(),
           backends=NS(cuda=NS(matmul=NS()),cudnn=NS()),cuda=NS(is_available=lambda:False),
           inference_mode=contextlib.nullcontext,__version__='test',float32='float32',OutOfMemoryError=MemoryError)
  modules={'torch':torch,'gliner2':NS(AutoExtractor=NS(from_pretrained=Mock(return_value=model))),
           'gliner2.processor':NS(SamplingConfig=lambda **kwargs:None)}
  with patch.dict('sys.modules',modules),patch.object(suite,'assets',return_value=(['bank_name'],'test-assets')):
   suite.infer(run,cfg,UI(),'cpu',4,None)
  saved=list(suite.lines(run/'predictions.jsonl'))
  self.assertEqual(model.extract_entities.call_count,2)
  self.assertEqual(len({r['review_id'] for r in saved}),5)
  self.assertEqual(saved[0]['spans'],saved[1]['spans'])
  self.assertEqual([r['status'] for r in saved[-2:]],['deleted','deleted'])
  self.assertTrue(all(not r['spans'] for r in saved[-2:]))
  self.assertEqual(suite.read(run/'manifest.json')['inference_sessions'][-1]['cache_hits'],1)
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.cfg=suite.settings();self.cfg['runs']=self.tmp.name;self.cfg['transport']='requests'
 def run_dir(self):return suite.new_run(self.cfg,filters={'from':'2026-11-09','to':'2026-11-09','groups':[6]})
 def test_hierarchy_unicode_leading_zeros_and_nonstring(self):
  run=self.run_dir();rows=suite.prepare(run,[event(1,[dict(uuid='a',type='aba-rtn',value='001234567')],[dict(uuid='o',name='person',Attribute=[dict(uuid='b',type='other',comment='Name',value='😀 José'),dict(uuid='c',value=123)])])])
  self.assertEqual(len(rows),3);self.assertEqual(rows[0]['source_row']['source_value'],'001234567');self.assertEqual(rows[1]['object_uuid'],'o');self.assertTrue(rows[2]['issues'])
  r=rows[1];start=r['value_offset'];spans,warnings=suite.checked_predictions(r,{'reported_actor_name':[dict(text='😀 José',start=start,end=start+6,confidence=.9)],'bank_name':[dict(text='other',start=13,end=18)]},['reported_actor_name','bank_name'])
  self.assertTrue(spans[0]['accepted']);self.assertFalse(spans[1]['accepted'])
 def test_routing_warning_is_not_a_prediction(self):
  run=self.run_dir();r=suite.prepare(run,[event(1,[dict(uuid='a',type='aba-rtn',value='021000089')])])[0]
  spans,warnings=suite.checked_predictions(r,{},['routing_number'])
  self.assertEqual(spans,[]);self.assertIn('routing_number',warnings[0])
 def test_changed_duplicate_rejected(self):
  with self.assertRaises(suite.SuiteError):suite.prepare(self.run_dir(),[event(1,[dict(uuid='a',value='x'),dict(uuid='a',value='y')])])
 def test_empty_page_required_and_page_files_saved(self):
  class Client:
   def search(self,**kw):return {'response':[{'Event':event(kw['page'])}]} if kw['page']<3 else {'response':[]}
  run=self.run_dir();suite.fetch(run,self.cfg,UI(),lambda:contextlib.nullcontext(Client()))
  m=suite.read(run/'manifest.json');self.assertTrue(m['fetch_complete']);self.assertEqual(m['events'],2);self.assertEqual(len(list((run/'raw').glob('*.json'))),3)
 def test_resume_changed_page_refused(self):
  run=self.run_dir();suite.save(run/'raw/group-6-page-000001.json',{'response':[{'Event':event(1)}]})
  class Client:
   def search(self,**kw):return {'response':[{'Event':event(2)}]}
  with self.assertRaisesRegex(suite.SuiteError,'changed'):suite.fetch(run,self.cfg,UI(),lambda:contextlib.nullcontext(Client()))
 def test_repeated_page_refused(self):
  class Client:
   def search(self,**kw):return {'response':[{'Event':event(1)}]}
  with self.assertRaisesRegex(suite.SuiteError,'Repeated'):suite.fetch(self.run_dir(),self.cfg,UI(),lambda:contextlib.nullcontext(Client()))
 def test_wrong_group_and_date_refused(self):
  for change in [dict(sharing_group_id='8'),dict(date='2026-11-10')]:
   e=event(1);e.update(change)
   class Client:
    def search(self,**kw):return {'response':[{'Event':e}]}
   with self.assertRaises(suite.SuiteError):suite.fetch(self.run_dir(),self.cfg,UI(),lambda:contextlib.nullcontext(Client()))
 def test_journal_tail_recovery_and_identity(self):
  run=self.run_dir();rows=suite.prepare(run,[event(1,[dict(uuid='a',value='x')])]);r=rows[0]
  obj=dict(review_id=r['review_id'],identity='test',input_hash=suite.canonical(r['input']))
  p=run/'predictions.jsonl';p.write_bytes((json.dumps(obj)+'\n{"broken').encode())
  self.assertEqual(len(suite.journal(run,rows,'test')),1)
  with self.assertRaises(suite.SuiteError):suite.journal(run,rows,'different')
 def test_no_overwrite(self):
  run=self.run_dir()
  with self.assertRaises(suite.SuiteError):suite.new_run(self.cfg,run)
 def test_transient_windows_replace_lock(self):
  target=Path(self.tmp.name)/'status.json';original=Path.replace;calls=[]
  def replace(p,dest):
   calls.append(1)
   if len(calls)<3:raise PermissionError('simulated reader lock')
   return original(p,dest)
  with patch.object(Path,'replace',replace),patch.object(suite.time,'sleep'):suite.save(target,{'done':7})
  self.assertEqual(suite.read(target),{'done':7});self.assertEqual(len(calls),3)
 def test_key_not_saved(self):
  keyfile=Path(self.tmp.name)/'.env';keyfile.write_text('NCFTA_API_KEY="test-only-key"\n')
  self.cfg['env']=str(keyfile)
  # Do not consult a developer's real .env when testing credential loading.
  with patch.dict('os.environ',{},clear=True),patch.object(suite,'HERE',Path(self.tmp.name)):
   self.assertEqual(suite.credentials(self.cfg),'test-only-key')

if __name__=='__main__':unittest.main()
