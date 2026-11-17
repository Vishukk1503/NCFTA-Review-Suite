import unittest
from unittest.mock import patch
import intel_backend
import intel_check
import suite

class IntelTests(unittest.TestCase):
    def test_interactive_saved_json_can_select_intel(self):
        with patch('rich.prompt.Prompt.ask',side_effect=['2','C:/saved.json','intel-gpu']),patch('rich.console.Console.print'):
            self.assertEqual(suite.interactive({}),['import','--json','C:/saved.json','--device','intel-gpu'])
    def test_device_selection_excludes_nvidia_and_cpu(self):
        class Core:
            available_devices=['CPU','GPU.0','GPU.1','GPU.2']
            def get_property(self,device,name):
                if name=='FULL_DEVICE_NAME':return {'GPU.0':'Intel Arc','GPU.1':'NVIDIA','GPU.2':'Intel legacy'}[device]
                return ['FP32'] if device!='GPU.2' else ['FP16']
        self.assertEqual([d['id'] for d in intel_backend.intel_devices(Core())],['GPU.0'])
    def test_precision_rejects_downgrade_and_unknown(self):
        intel_backend.require_fp32(['f32','i64','dynamic'])
        for values in (['f32','f16'],['f32','bf16'],['f32','i8'],['dynamic']):
            with self.assertRaises(intel_backend.BackendError):intel_backend.require_fp32(values)
    def test_same_value_different_context_is_not_deduplicated(self):
        rows=[dict(review_id=str(i),input=text,issues=[],deleted=False,source_row=dict(source_value='X',source_type=kind))
              for i,(text,kind) in enumerate([('A','other'),('A','other'),('B','aba-rtn'),('C'*100,'comment')])]
        chosen=intel_check.select_rows(rows,20)
        self.assertEqual(len(chosen),3);self.assertEqual(chosen[-1]['input'],'C'*100)
    def test_prediction_comparison_checks_labels_spans_and_rejections(self):
        base=dict(review_id='1',status='predicted',warnings=[],spans=[dict(field='bank_name',text='X',start=3,end=4,accepted=True,confidence=.8)])
        same=dict(base,spans=[dict(base['spans'][0],confidence=.800001)])
        self.assertEqual(intel_check.compare([base],[same])[0],[])
        for changes in ({'field':'person_name'},{'start':2},{'accepted':False}):
            changed=dict(base,spans=[dict(base['spans'][0],**changes)])
            self.assertEqual(intel_check.compare([base],[changed])[0],['1'])
        self.assertEqual(intel_check.compare([base],[])[0],['1'])

if __name__=='__main__':unittest.main()
