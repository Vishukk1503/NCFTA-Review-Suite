"""Optional FP32 Intel GPU encoder. No downloads, training or automatic selection."""
from __future__ import annotations
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
MODEL_SHA = 'ac95258b1c2baaffd6a7dabbe5f8433c243d1b968bba693bddf6a6296db41380'
IR_HASHES = {
    'encoder-fp32.xml': '34ecc9957c35a9b5198146c0d57490fae692f95f70e7c882f773fe7b17eb4a2a',
    'encoder-fp32.bin': '06d956899d8454bf10a7a7c09073a1de11b53c88ca983c58da2985b9ef8a1efe',
}

class BackendError(RuntimeError):
    pass

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def runtime():
    if importlib.util.find_spec('openvino') is None:
        bundled = HERE / 'runtime' / 'openvino-python312-win64'
        if not (bundled / 'openvino').is_dir():
            raise BackendError('OpenVINO is unavailable. Copy the bundled runtime folder with the suite; nothing was downloaded.')
        if sys.version_info[:2] != (3, 12) or sys.platform != 'win32':
            raise BackendError('The bundled OpenVINO runtime requires Windows and Python 3.12.')
        sys.path.insert(0, str(bundled))
    try:
        import openvino as ov
        return ov
    except (ImportError, OSError) as exc:
        raise BackendError('OpenVINO could not load. Check the runtime and Intel graphics driver.') from exc

def intel_devices(core):
    devices = []
    for device in core.available_devices:
        if not device.startswith('GPU'):
            continue
        name = str(core.get_property(device, 'FULL_DEVICE_NAME'))
        capabilities = [str(x) for x in core.get_property(device, 'OPTIMIZATION_CAPABILITIES')]
        if 'intel' in name.lower() and 'FP32' in capabilities:
            devices.append({'id': device, 'name': name, 'capabilities': capabilities})
    return devices

def probe():
    ov = runtime()
    return {'openvino_version': ov.__version__, 'intel_gpus': intel_devices(ov.Core())}

def verify_ir(folder=None):
    folder = Path(folder) if folder else HERE / 'accelerators' / 'openvino-v3'
    for name, expected in IR_HASHES.items():
        path = folder / name
        if not path.is_file() or digest(path) != expected:
            raise BackendError('The bundled FP32 encoder is missing or its checksum changed: ' + name)
    return folder

def require_fp32(precisions):
    values = {str(p).lower() for p in precisions}
    # U8 may represent boolean attention masks; it is not a floating-point downgrade.
    reduced = {'f16', 'fp16', 'float16', 'bf16', 'bfloat16', 'i8', 'int8'}
    if values & reduced or not values & {'f32', 'fp32', 'float32'}:
        raise BackendError('Cannot confirm FP32 encoder computation: ' + ', '.join(sorted(values)))

def require_execution_device(selected, execution):
    # OpenVINO documents GPU as an alias for GPU.0. Discovery and compilation
    # can return different spellings. Preserve every explicit device/tile index.
    def canonical(device):
        return 'GPU.0' if device == 'GPU' else device
    if [canonical(device) for device in execution] != [canonical(selected)]:
        raise BackendError(f'Unexpected encoder execution device: {execution}; requested {selected}')


def attach(model, model_path):
    """Replace only the encoder execution; the original extraction head stays on CPU."""
    import torch
    from types import SimpleNamespace
    if digest(Path(model_path) / 'model.safetensors') != MODEL_SHA:
        raise BackendError('Intel encoder conversion does not match the selected v3 checkpoint.')
    folder = verify_ir()
    ov = runtime()
    core = ov.Core()
    candidates = intel_devices(core)
    if not candidates:
        raise BackendError('No FP32-capable Intel GPU is exposed by OpenVINO. Check the Intel graphics driver; CPU remains available.')
    selected = candidates[0]
    start = time.perf_counter()
    try:
        compiled = core.compile_model(str(folder / 'encoder-fp32.xml'), selected['id'], {
            'INFERENCE_PRECISION_HINT': 'f32',
            'EXECUTION_MODE_HINT': 'ACCURACY',
            'PERFORMANCE_HINT': 'LATENCY',
        })
        execution = [str(d) for d in compiled.get_property('EXECUTION_DEVICES')]
        require_execution_device(selected['id'], execution)
        precisions = set()
        for node in compiled.get_runtime_model().get_ops():
            metadata = node.get_rt_info()
            if 'runtimePrecision' in metadata:
                precisions.add(str(metadata['runtimePrecision'].value))
        require_fp32(precisions)
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError('Intel GPU FP32 compilation failed (' + type(exc).__name__ + '). CPU remains available.') from exc
    details = dict(backend='openvino-intel',encoder_device=selected['id'],
                   encoder_device_name=selected['name'],head_device='cpu',precision='FP32',
                   openvino_version=ov.__version__,execution_devices=execution,
                   runtime_precisions=sorted(precisions),num_streams=str(compiled.get_property('NUM_STREAMS')),compile_seconds=time.perf_counter()-start,
                   encoder_hashes=IR_HASHES)
    class Encoder(torch.nn.Module):
        def forward(self, input_ids, attention_mask, **kwargs):
            if input_ids.device.type != 'cpu' or attention_mask.device.type != 'cpu':
                raise BackendError('Intel encoder inputs must be CPU tensors.')
            result = compiled({'input_ids': input_ids.numpy(), 'attention_mask': attention_mask.numpy()})[0]
            if str(result.dtype) != 'float32':
                raise BackendError('Intel encoder returned reduced precision.')
            # Copy before the next request can reuse the OpenVINO output buffer.
            return SimpleNamespace(last_hidden_state=torch.from_numpy(result.copy()))
    model.encoder = Encoder()
    gc.collect()
    return details
