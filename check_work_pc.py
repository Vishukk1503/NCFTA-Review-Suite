"""Offline installation check using synthetic input; never connects to NCFTA."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import sys
import uuid

HERE=Path(__file__).resolve().parent

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',choices=['intel','cpu','nvidia'],default='intel')
    parser.add_argument('--skip-model-check',action='store_true')
    args=parser.parse_args(argv)
    # A clean cache proves the release does not depend on this PC's cached models.
    os.environ.update(HF_HOME=str(HERE/'setup'/'empty-model-cache'),HF_HUB_OFFLINE='1',
                      TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1')
    import torch
    import openvino as ov
    from rich.console import Console
    from rich.table import Table
    import suite
    import intel_backend
    from export_xlsx import write_workbook
    console=Console()
    core=ov.Core()
    devices=[]
    for device in core.available_devices:
        devices.append(dict(id=str(device),name=str(core.get_property(device,'FULL_DEVICE_NAME')),
                            capabilities=[str(c) for c in core.get_property(device,'OPTIMIZATION_CAPABILITIES')]))
    report=dict(python=platform.python_version(),platform=platform.system(),machine=platform.machine(),
                torch=torch.__version__,openvino=ov.__version__,devices=devices,
                cuda_available=torch.cuda.is_available(),profile=args.profile,
                npu_policy='Disabled: NPU hardware computation is FP16; v3 requires FP32.',
                model_smoke='not_run',full_validation=False,errors=[])
    table=Table(title='NCFTA work PC setup')
    table.add_column('Check');table.add_column('Result')
    table.add_row('Python',platform.python_version()+' / '+platform.machine())
    table.add_row('Torch',torch.__version__)
    table.add_row('CUDA',torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'Unavailable in this environment')
    for device in devices:
        table.add_row(device['id'],device['name']+' | '+', '.join(device['capabilities']))
    table.add_row('NPU policy','Detected separately; disabled for strict FP32 v3 inference')
    console.print(table)
    target=HERE/'setup'
    target.mkdir(exist_ok=True)
    # A synthetic Excel check includes the actual portable exporter.
    from test_install import synthetic_payload
    workbook=target/('excel-smoke-'+uuid.uuid4().hex[:8]+'.xlsx')
    write_workbook(synthetic_payload(),workbook)
    report['excel_smoke']='passed'
    if args.profile=='intel' and not intel_backend.intel_devices(core):
        report['errors'].append('Intel GPU is not exposed by OpenVINO. Install the OEM-approved Intel graphics driver and rerun. CPU profile remains available.')
    if args.profile=='nvidia' and not torch.cuda.is_available():
        report['errors'].append('CUDA is unavailable. Check the NVIDIA driver and use the nvidia installer profile.')
    if not args.skip_model_check:
        try:
            cfg=suite.settings()
            labels,asset_hash=suite.assets(cfg)
            report.update(extraction_fields=len(labels),asset_hash=asset_hash)
            if args.profile=='intel': intel_backend.verify_ir()
            class UI:
                def update(self,phase,*args,**kwargs):
                    if phase not in ('Extracting','Running model'): console.print(phase)
            ui=UI()
            # Test the same source formatter, tokenizer, checkpoint and 116 queries.
            event={'id':'synthetic-event','uuid':str(uuid.uuid4()),'date':'2026-01-01','sharing_group_id':'6',
                   'Attribute':[{'id':'synthetic-attribute','uuid':str(uuid.uuid4()),'type':'email',
                                 'category':'Person','comment':'Synthetic setup check',
                                 'value':'setup@example.invalid'}]}
            document=target/('synthetic-input-'+uuid.uuid4().hex[:8]+'.json')
            suite.save(document,[event])
            predictions={}
            timings={}
            modes=['cpu']+(['intel-gpu'] if args.profile=='intel' else ['cuda'] if args.profile=='nvidia' else [])
            for mode in modes:
                run=suite.new_run(cfg,run=target/('smoke-'+mode+'-'+uuid.uuid4().hex[:8]))
                suite.import_json(run,document,ui)
                suite.infer(run,cfg,ui,device=mode,threads=4)
                predictions[mode]=list(suite.lines(run/'predictions.jsonl'))[0]
                timings[mode]=suite.read(run/'manifest.json').get('inference_sessions',[])
            def signature(prediction):
                return sorted((s['field'],s['text'],s['start'],s['end'],s['accepted']) for s in prediction['spans'])
            baseline=predictions['cpu']
            if baseline['status']!='predicted': raise RuntimeError('CPU synthetic input was not evaluated.')
            for mode,prediction in predictions.items():
                if prediction['status']!='predicted' or signature(prediction)!=signature(baseline):
                    raise RuntimeError('Synthetic output agreement failed for '+mode)
            report.update(model_smoke='passed',sample_count=1,sample_outputs_match=True,timings=timings)
            console.print('[green]Original v3 loaded; synthetic inference and output agreement passed.[/green]')
            console.print('One synthetic input checks installation only. It does not replace model validation.')
        except Exception as exc:
            report['errors'].append(type(exc).__name__+': '+str(exc))
    else:
        console.print('[yellow]Model load/inference skipped. This does not establish inference readiness.[/yellow]')
    report['ready']=not report['errors'] and report['model_smoke']=='passed'
    suite.save(target/'hardware-report.json',report)
    for error in report['errors']: console.print(error,style='red',markup=False)
    console.print('Report: '+str(target/'hardware-report.json'))
    return 1 if report['errors'] else 0

if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        print('Setup check failed: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(1)
