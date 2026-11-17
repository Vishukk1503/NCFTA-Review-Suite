"""Compare unchanged v3 on saved inputs. Never fetches, trains or reads test data."""
from __future__ import annotations
from pathlib import Path
import json
import shutil
import uuid
import suite
import intel_backend

def select_rows(rows, count):
    unique = {}
    for row in rows:
        if not row['issues'] and not row['deleted'] and row['source_row']['source_value']:
            unique.setdefault(row['input'], row)
    ordered = sorted(unique.values(), key=lambda r: (len(r['input']), r['review_id']))
    if not ordered:
        raise suite.SuiteError('The saved run has no eligible inputs.')
    count = min(count, len(ordered))
    selected = {}
    def add(row):
        if len(selected) < count:
            selected.setdefault(row['review_id'], row)
    # Include the longest input, different source types, and a spread of lengths.
    add(ordered[-1]);add(ordered[0])
    for kind in sorted({r['source_row']['source_type'] for r in ordered}):
        add(next(r for r in ordered if r['source_row']['source_type'] == kind))
    for i in range(count):
        add(ordered[round(i * (len(ordered)-1) / max(count-1, 1))])
    for row in ordered:
        add(row)
    return sorted(selected.values(), key=lambda r: (len(r['input']), r['review_id']))

def signature(row):
    return (row['status'], sorted(row['warnings']), sorted(
        (s['field'], s['text'], s['start'], s['end'], s['accepted']) for s in row['spans']))

def compare(cpu, gpu):
    a = {r['review_id']: r for r in cpu}
    b = {r['review_id']: r for r in gpu}
    differences = [rid for rid in sorted(a.keys() | b.keys())
                   if rid not in a or rid not in b or signature(a[rid]) != signature(b[rid])]
    max_delta = 0.0
    for rid in a.keys() & b.keys():
        scores = {(s['field'], s['text'], s['start'], s['end']): s.get('confidence', 0) for s in a[rid]['spans']}
        for s in b[rid]['spans']:
            key = (s['field'], s['text'], s['start'], s['end'])
            if key in scores:
                max_delta = max(max_delta, abs(s.get('confidence', 0)-scores[key]))
    return differences, max_delta

def run_check(cfg, source, samples=20, threads=4, resume_check=None):
    source = Path(source).resolve()
    m = suite.read(source / 'manifest.json')
    if not m.get('fetch_complete'):
        raise suite.SuiteError('Choose a run with completed fetch/import.')
    if suite.sha(source/'events.json') != m.get('events_sha256', m.get('source_sha256')) or suite.sha(source/'attribute-records.jsonl') != m['records_sha256']:
        raise suite.SuiteError('Saved input checksum changed.')
    try:
        hardware = intel_backend.probe()
        intel_backend.verify_ir()
    except intel_backend.BackendError as exc:
        raise suite.SuiteError(str(exc)) from exc
    if not hardware['intel_gpus']:
        raise suite.SuiteError('No Intel GPU is available to check. Check setup lists available devices.')
    rows = select_rows(list(suite.lines(source/'attribute-records.jsonl')), samples)
    target = suite.HERE/'checks'/('intel-'+suite.dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    report = dict(status='running',source_run=str(source),samples=len(rows),threads=threads,
                  hardware=hardware,threshold=.5,precision='FP32',full_validation=False,
                  automatic_device_selection_changed=False,source_records_sha256=m['records_sha256'],
                  selected_input_hashes=[suite.canonical(r['input']) for r in rows],
                  note='Saved-input comparison, not a labeled accuracy evaluation. Includes first inference; model loading/compilation reported separately.')
    if resume_check:
        target=Path(resume_check).resolve()
        old=suite.read(target/'report.json')
        for key in ('source_run','samples','threads','source_records_sha256','selected_input_hashes','hardware'):
            if old.get(key)!=report[key]:raise suite.SuiteError('The saved check does not match these inputs, settings or hardware.')
        if old.get('status')=='complete':raise suite.SuiteError('This check is already complete; its results are preserved.')
        report['resumed_check']=True
    else:
        target.mkdir(parents=True)
    suite.save(target/'report.json',report)
    try:
        for device in ('cpu','intel-gpu'):
            run=target/device
            if not (run/'manifest.json').exists():
                run = suite.new_run(cfg, run, source='local_acceleration_check')
                shutil.copyfile(source/'events.json',run/'events.json')
                with (run/'attribute-records.jsonl').open('w',encoding='utf8') as f:
                    for row in rows:
                        f.write(json.dumps(row,ensure_ascii=False)+'\n')
                manifest=suite.read(run/'manifest.json')
                manifest.update(fetch_complete=True,source_sha256=suite.sha(run/'events.json'),
                                records_sha256=suite.sha(run/'attribute-records.jsonl'),records=len(rows),
                                selection_from=str(source),check_only=True)
                suite.save(run/'manifest.json',manifest)
            elif not resume_check:
                raise suite.SuiteError('Check folder already exists.')
            with suite.lock(run),suite.Dashboard(run) as ui:
                suite.infer(run,cfg,ui,device,threads)
            report[device]=suite.read(run/'manifest.json')['inference_sessions'][-1]
            suite.save(target/'report.json',report)
        cpu=list(suite.lines(target/'cpu/predictions.jsonl'))
        gpu=list(suite.lines(target/'intel-gpu/predictions.jsonl'))
        differences,delta=compare(cpu,gpu)
        errors=[r['review_id'] for r in cpu+gpu if r['status']!='predicted']
        ratio=report['cpu']['inference_seconds']/report['intel-gpu']['inference_seconds']
        report.update(status='complete',different_records=differences,input_errors=sorted(set(errors)),
                      matching_rows=len(rows)-len(differences),max_confidence_delta=delta,
                      inference_speedup=ratio,output_check_passed=not differences and not errors,
                      faster=ratio>1,finished_utc=suite.now())
        report['cpu_total_seconds']=report['cpu']['load_seconds']+report['cpu']['inference_seconds']
        report['intel_total_seconds']=report['intel-gpu']['load_seconds']+report['intel-gpu']['inference_seconds']
        report['total_speedup']=report['cpu_total_seconds']/report['intel_total_seconds']
        if differences or errors:
            verdict='Keep the current backend: the input/output check failed.'
        elif ratio<=1:
            verdict='Keep the current backend: Intel GPU was not faster on this sample.'
        else:
            verdict='Intel GPU was faster and matched this sample. Full validation is still required before default use.'
        report['verdict']=verdict
        suite.save(target/'report.json',report)
        message=(f"Intel GPU check: {hardware['intel_gpus'][0]['name']}\n"
                 f"Inputs: {len(rows)} | Matching rows: {report['matching_rows']}\n"
                 f"CPU inference: {report['cpu']['inference_seconds']:.2f} s"
                 f"\nIntel inference: {report['intel-gpu']['inference_seconds']:.2f} s\n"
                 f"Speed ratio: {ratio:.2f}x | Confidence delta: {delta:.8f}\n"
                 f"Including model load/compilation: CPU {report['cpu_total_seconds']:.2f} s; Intel {report['intel_total_seconds']:.2f} s\n"
                 f"{verdict}\nNo default was changed. No training or frozen-test access.\n")
        (target/'RESULTS.txt').write_text(message,encoding='utf8')
        suite.terminal_console().print(message)
        suite.terminal_console().print(str(target))
        return target
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',error_type=type(exc).__name__,
                      message=str(exc) if isinstance(exc,suite.SuiteError) else 'Check did not complete.')
        suite.save(target/'report.json',report)
        raise
