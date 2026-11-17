import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import intel_backend
import intel_check
import suite

class IntelTests(unittest.TestCase):
    def attach_with_reported_device(self, execution, precisions=('f32',)):
        # Reproduce a single-GPU PC: discovery says GPU, compilation says GPU.0.
        class Compiled:
            def get_property(self, name):
                return execution if name == 'EXECUTION_DEVICES' else 1
            def get_runtime_model(self):
                return SimpleNamespace(get_ops=lambda: [
                    SimpleNamespace(get_rt_info=lambda p=p: {
                        'runtimePrecision': SimpleNamespace(value=p)}) for p in precisions])
        class Core:
            available_devices = ['CPU', 'GPU']
            def get_property(self, device, name):
                return 'Intel Arc' if name == 'FULL_DEVICE_NAME' else ['FP32']
            def compile_model(self, path, device, options):
                self.requested = device
                self.options = options
                return Compiled()
        core = Core()
        model = SimpleNamespace(encoder=object())
        fake_torch = SimpleNamespace(nn=SimpleNamespace(Module=object))
        with patch.dict('sys.modules', {'torch': fake_torch}), \
             patch.object(intel_backend, 'digest', return_value=intel_backend.MODEL_SHA), \
             patch.object(intel_backend, 'verify_ir', return_value=Path('synthetic-ir')), \
             patch.object(intel_backend, 'runtime', return_value=SimpleNamespace(Core=lambda: core, __version__='synthetic')):
            result = intel_backend.attach(model, Path('synthetic-model'))
        self.assertEqual(core.options['INFERENCE_PRECISION_HINT'], 'f32')
        self.assertEqual(core.options['EXECUTION_MODE_HINT'], 'ACCURACY')
        self.assertEqual(core.requested, 'GPU')
        return result
    def test_attach_accepts_gpu_alias_reported_as_gpu_zero(self):
        result = self.attach_with_reported_device(['GPU.0'])
        self.assertEqual(result['execution_devices'], ['GPU.0'])
        self.assertEqual(result['head_device'], 'cpu')
        self.assertEqual(result['precision'], 'FP32')
    def test_attach_alias_keeps_device_and_precision_guards(self):
        for execution in (['CPU'], ['NPU'], ['GPU.1'], ['GPU.0', 'CPU'], []):
            with self.subTest(execution=execution), self.assertRaisesRegex(intel_backend.BackendError, 'execution device'):
                self.attach_with_reported_device(execution)
        for precisions in (['f32', 'f16'], ['bf16'], ['i8'], ['dynamic']):
            with self.subTest(precisions=precisions), self.assertRaisesRegex(intel_backend.BackendError, 'FP32'):
                self.attach_with_reported_device(['GPU.0'], precisions)
    def test_execution_alias_does_not_hide_other_gpu_or_tile(self):
        for selected, execution in [('GPU', ['GPU.0']), ('GPU.0', ['GPU']),
                                    ('GPU', ['GPU']), ('GPU.1', ['GPU.1']),
                                    ('GPU.0.1', ['GPU.0.1'])]:
            with self.subTest(selected=selected, execution=execution):
                intel_backend.require_execution_device(selected, execution)
        for selected, execution in [('GPU.1', ['GPU']), ('GPU', ['GPU.1']),
                                    ('GPU', ['GPU.0.1']), ('GPU.0.1', ['GPU']),
                                    ('GPU', ['GPU.0', 'GPU.0'])]:
            with self.subTest(selected=selected, execution=execution), self.assertRaises(intel_backend.BackendError):
                intel_backend.require_execution_device(selected, execution)
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
