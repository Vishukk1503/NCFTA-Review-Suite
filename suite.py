"""NCFTA local review workflow. Read-only network access; no training or test data."""
from __future__ import annotations
import argparse, contextlib, datetime as dt, hashlib, importlib.util, json, os
from pathlib import Path
import shutil, subprocess, sys, time, uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'assets'))
import source_input

class SuiteError(Exception): pass

def safe_failure_message(exc):
    # The optional PyMISP module emits sanitized errors. Do not hide them again
    # or import PyMISP on machines configured to use the requests transport.
    client_module=sys.modules.get('ncfta_client')
    safe_types=(SuiteError,)
    if client_module is not None:
        safe_types+=(client_module.NCFTAClientError,)
    if isinstance(exc,safe_types):return str(exc)
    return 'Operation failed ('+type(exc).__name__+'); source details withheld.'
def terminal_console():
    from rich.console import Console
    # A visible PowerShell launcher can expose Python stdout as a pipe.
    if os.environ.get('NCFTA_TERMINAL')=='1':
        return Console(force_terminal=True,color_system='truecolor',no_color=False)
    return Console()

def menu_view():
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    options=Table.grid(padding=(0,2),expand=True)
    options.add_column(style='bold cyan',width=3)
    options.add_column(ratio=2,style='bold white')
    options.add_column(ratio=3,style='dim')
    for row in [
        ('1','Fetch & extract','Choose dates and groups; create an Excel review'),
        ('2','Extract saved JSON','Use a local event export'),
        ('3','Resume a run','Continue from saved progress'),
        ('4','Export to Excel','Export a completed run'),
        ('5','Check setup','Check model, device and dependencies'),
        ('6','Open output folder','Browse saved runs and Excel workbooks'),
        ('7','Check Intel GPU','Compare CPU and Intel GPU on saved inputs'),
        ('0','Exit','Close the suite'),
    ]:options.add_row(*row)
    return Group(
        Panel(Group(Text('NCFTA  /  REVIEW SUITE',style='bold cyan'),
                    Text('Model v3  |  Groups 6 / 7 / 8',style='white'),
                    Text('Events → Objects & attributes → Extraction → Excel',style='dim')),
              border_style='cyan',padding=(1,2)),
        Panel(options,title='[bold]Choose a workflow[/bold]',title_align='left',
              border_style='blue',padding=(1,1)),
        Text('  Local model inference • Review results in Excel',style='dim'),
    )

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def canonical(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    for attempt in range(40):
        try:temp.replace(path);break
        except PermissionError:
            if attempt==39:raise
            time.sleep(.05)
def lines(path):
    with Path(path).open(encoding='utf8') as f:
        for line in f:
            if line.strip(): yield json.loads(line)
def log(run,code,**values):
    with (run/'run.log').open('a',encoding='utf8') as f: f.write(json.dumps(dict(time=now(),code=code,**values))+'\n')
def settings():
    cfg=read(HERE/('settings.json' if (HERE/'settings.json').is_file() else 'settings.example.json'))
    for key in ['model','vendor','catalog','env','runs','node','artifact_modules','ca_bundle']:
        if cfg.get(key):
            p=Path(os.path.expandvars(cfg[key])).expanduser()
            cfg[key]=str((HERE/p).resolve()) if not p.is_absolute() else str(p)
    return cfg
@contextlib.contextmanager
def lock(run):
    run.mkdir(parents=True,exist_ok=True)
    with (run/'run.lock').open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt; msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl; fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: raise SuiteError('Another process is using this run.') from None
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)

class Dashboard:
    def __init__(self,run):
        from rich.console import Console
        from rich.progress import Progress,SpinnerColumn,BarColumn,TextColumn,TimeElapsedColumn,TimeRemainingColumn
        self.run=run;self.console=terminal_console();self.start=time.perf_counter();self.state={}
        self.progress=Progress(SpinnerColumn(),TextColumn('{task.description}'),BarColumn(),TextColumn('{task.fields[count]}'),TextColumn('elapsed'),TimeElapsedColumn(),TextColumn('ETA'),TimeRemainingColumn(),console=self.console)
        self.task=self.progress.add_task('Preparing',total=1,count='');self.live=None
    def render(self):
        from rich.panel import Panel
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text
        phase=self.state.get('phase','Preparing')
        stage=(0 if phase in ('Fetching','Importing saved JSON','Preparing','Fetch complete') else
               1 if phase in ('Verifying model assets','Loading v3') else
               3 if phase in ('Exporting Excel','Complete') else 2)
        steps=Table.grid(expand=True,padding=(0,1))
        for _ in range(4):steps.add_column(ratio=1)
        steps.add_row(*[Text(f'{i+1}  {name}',style='bold cyan' if i==stage else 'dim')
                       for i,name in enumerate(('FETCH / IMPORT','LOAD MODEL','EXTRACT','EXCEL'))])
        stats=Table.grid(padding=(0,3),expand=True)
        stats.add_column(style='dim',ratio=1);stats.add_column(ratio=2)
        labels={'group':'Sharing group','events':'Events fetched','transport':'Connection',
                'dates':'Event dates','device':'Compute device','precision':'Precision',
                'resumed':'Saved predictions','rows_per_second':'Attributes / second',
                'GPU_GB':'GPU memory (GB)','warnings':'Review warnings','errors':'Input errors',
                'cpu_threads':'CPU threads','cache_hits':'Repeated inputs reused','records':'Attributes','workbook':'Excel workbook','next_action':'Next step'}
        for key,value in self.state.items():
            if key in ('done','total','updated_utc','phase'):continue
            stats.add_row(labels.get(key,key.replace('_',' ').title()),Text(str(value),style='yellow' if key in ('warnings','errors') and value else 'white'))
        if not stats.row_count:stats.add_row('Status',Text('Preparing this stage…'))
        return Group(
            Panel(Group(Text('NCFTA  /  REVIEW SUITE',style='bold cyan'),Text(self.run.name,style='dim'),steps),border_style='cyan',padding=(1,2)),
            Panel(self.progress,title=Text(phase,style='bold cyan'),title_align='left',border_style='blue',padding=(1,2)),
            Panel(stats,title='[bold]Run statistics[/bold]',title_align='left',border_style='blue',padding=(1,2)),
            Text('  Ctrl+C pauses safely • Resume from the home menu',style='dim'),
        )
    def __enter__(self):
        from rich.live import Live
        self.live=Live(self.render(),console=self.console,refresh_per_second=4);self.live.__enter__();return self
    def update(self,phase,done=0,total=1,**values):
        if phase!=self.state.get('phase'):
            self.progress.reset(self.task,total=total,completed=done,start=True)
        self.state=dict(phase=phase,done=done,total=total,**values,updated_utc=now())
        self.progress.update(self.task,description=phase,completed=done,total=total,count=f'{done}/{total}' if total is not None else f'page {done}; total unknown')
        save(self.run/'status.json',self.state)
        if self.live:self.live.update(self.render())
    def __exit__(self,*exc):
        if self.live:self.live.__exit__(*exc)

def docs_events(doc):
    if isinstance(doc,list): items=doc
    elif isinstance(doc,dict):
        if 'error' in doc or 'errors' in doc:raise SuiteError('Server returned an API error.')
        items=doc.get('response',doc.get('Event',[doc] if 'id' in doc else None))
        if isinstance(items,dict) and isinstance(items.get('Event'),list):items=items['Event']
        elif isinstance(items,dict):items=[items]
    else:items=None
    if not isinstance(items,list):raise SuiteError('Unexpected event JSON structure.')
    out=[]
    for item in items:
        e=item.get('Event',item) if isinstance(item,dict) else None
        if not isinstance(e,dict) or not (e.get('uuid') or e.get('id')):raise SuiteError('Event lacks an identity.')
        out.append(e)
    return out

def prepare(run,events):
    records=[];seen={};objects=0;duplicates=0
    for e in events:
        eventkey=str(e.get('uuid') or e['id'])
        groups=[(None,e.get('Attribute') or [])]
        for obj in e.get('Object') or []:
            if not isinstance(obj,dict):raise SuiteError('Malformed object in saved JSON.')
            groups.append((obj,obj.get('Attribute') or []));objects+=1
        for obj,attrs in groups:
            if not isinstance(attrs,list):raise SuiteError('Malformed attribute list.')
            for pos,a in enumerate(attrs):
                if not isinstance(a,dict):raise SuiteError('Malformed attribute.')
                objkey=str((obj or {}).get('uuid') or (obj or {}).get('id') or 'direct')
                attrkey=str(a.get('uuid') or a.get('id') or f'position-{pos}')
                key=canonical([eventkey,objkey,attrkey]);fingerprint=canonical(a)
                if key in seen:
                    if seen[key]!=fingerprint:raise SuiteError('Conflicting duplicate attribute; input needs review.')
                    duplicates+=1;continue
                seen[key]=fingerprint
                row={};issues=[]
                for dest,src in [('source_type','type'),('source_category','category'),('source_comment','comment'),('source_value','value')]:
                    value=a.get(src,'')
                    if value is None:value=''
                    if not isinstance(value,str):issues.append('Non-string '+src);value=''
                    row[dest]=value
                p=source_input.packet(row)
                r=dict(review_id='R'+key[:20],event_id=str(e.get('id','')),event_uuid=e.get('uuid',''),event_date=e.get('date',''),group=str(e.get('sharing_group_id','')),object_id=str((obj or {}).get('id','')),object_uuid=(obj or {}).get('uuid',''),object_name=(obj or {}).get('name',''),object_relation=a.get('object_relation',''),attribute_id=str(a.get('id','')),attribute_uuid=a.get('uuid',''),attribute_distribution=a.get('distribution'),attribute_sharing_group_id=a.get('sharing_group_id'),first_seen=a.get('first_seen'),last_seen=a.get('last_seen'),source_row=row,input=p['text'],value_offset=p['source_value_offset'],source_hash=fingerprint,issues=issues,deleted=a.get('deleted') in (True,1,'1','true'))
                records.append(r)
    target=run/'attribute-records.jsonl'
    content=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records)
    if target.exists() and target.read_text(encoding='utf8')!=content:raise SuiteError('Existing prepared records differ from raw JSON.')
    temp=run/'attribute-records.jsonl.tmp';temp.write_text(content,encoding='utf8');temp.replace(target)
    m=read(run/'manifest.json');m.update(records=len(records),events=len(events),objects=objects,duplicate_attributes=duplicates,records_sha256=sha(target),phase='prepared');save(run/'manifest.json',m)
    log(run,'prepared',records=len(records),events=len(events),objects=objects)
    return records

def credentials(cfg):
    key=os.environ.get('NCFTA_API_KEY') or os.environ.get('MISP_API_KEY')
    env=HERE/'.env' if (HERE/'.env').is_file() else Path(cfg['env'])
    if not key and env.is_file():
        for raw in env.read_text(encoding='utf-8-sig').splitlines():
            line=raw.strip().removeprefix('export ')
            if not line or line.startswith('#') or '=' not in line:continue
            k,v=line.split('=',1)
            if k.strip() in ('NCFTA_API_KEY','MISP_API_KEY','MISP_KEY'):
                v=v.strip()
                if len(v)>1 and v[0]==v[-1] and v[0] in '\"\'':v=v[1:-1]
                else:v=v.split(' #',1)[0].strip()
                if v:key=v;break
    if not key:raise SuiteError('No API key found. Set NCFTA_API_KEY in the configured .env file.')
    return key

class HTTPClient:
    def __init__(self,url,key,ca_bundle=None):
        from tls_support import configure_tls, TLSConfigurationError
        try:self.verify,_=configure_tls(ca_bundle)
        except TLSConfigurationError as exc:raise SuiteError(str(exc)) from None
        import requests
        self.session=requests.Session();self.url=url+'/events/restSearch'
        self.session.headers.update(Authorization=key,Accept='application/json',**{'Content-Type':'application/json'})
    def __enter__(self):return self
    def __exit__(self,*a):self.session.close()
    def search(self,**args):
        import requests
        body={'returnFormat':'json','sharinggroup':args['sharinggroup'],'from':args['date_from'],'to':args['date_to'],'page':args['page'],'limit':args['limit'],'metadata':False,'withAttachments':False}
        try:
            r=self.session.post(self.url,json=body,timeout=(20,180),allow_redirects=False,verify=self.verify)
            if r.status_code!=200:raise SuiteError(f'MISP HTTP {r.status_code}; response body withheld.')
            return r.json()
        except requests.RequestException:raise SuiteError('Network/TLS request failed; credentials and response withheld.') from None
        except ValueError:raise SuiteError('MISP returned invalid JSON.') from None

def transport(cfg):
    from urllib.parse import urlsplit
    u=urlsplit(cfg['url'])
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:raise SuiteError('MISP URL must be an HTTPS origin without credentials or path.')
    mode=cfg.get('transport','auto')
    available=importlib.util.find_spec('pymisp') is not None
    if mode=='pymisp' and not available:raise SuiteError('PyMISP is not installed. Install requirements-network.txt or explicitly choose requests.')
    return 'pymisp' if mode!='requests' and available else 'requests'

def fetch(run,cfg,ui,client_factory=None):
    m=read(run/'manifest.json');f=m['filters'];key=credentials(cfg) if client_factory is None else ''
    mode=transport(cfg);m.update(transport=mode,server=cfg['url'],attachments_requested=False);save(run/'manifest.json',m)
    if client_factory:context=client_factory()
    elif mode=='pymisp':
        from ncfta_client import connect
        context=connect(cfg['url'],key,180,cfg.get('ca_bundle'))
    else:context=HTTPClient(cfg['url'],key,cfg.get('ca_bundle'))
    del key
    all_events=[];global_seen={};queries=[]
    with context as client:
        for group in f['groups']:
            seen=set();count=0
            for page in range(1,100001):
                ui.update('Fetching',page,None,group=group,events=len(all_events),transport=mode,dates=f['from']+' to '+f['to'])
                body=None
                for attempt in range(3):
                    try:
                        body=client.search(controller='events',return_format='json',sharinggroup=[group],date_from=f['from'],date_to=f['to'],page=page,limit=10,metadata=False,with_attachments=False,pythonify=False);break
                    except Exception:
                        log(run,'fetch_retry',group=group,page=page,attempt=attempt+1)
                        if attempt==2:raise SuiteError(f'Fetch failed for group {group}, page {page}; saved pages retained. Check permissions/network.') from None
                        time.sleep(2**attempt)
                batch=docs_events(body)
                if len(batch)>10:raise SuiteError('Server ignored page size; refusing incomplete export.')
                for e in batch:
                    try:day=dt.date.fromisoformat(e.get('date','')).isoformat()
                    except (ValueError,TypeError):raise SuiteError('Server returned an invalid event date.') from None
                    if str(e.get('sharing_group_id'))!=str(group) or not f['from']<=day<=f['to']:raise SuiteError('Server returned an event outside requested group/date filters.')
                    ident=str(e.get('uuid') or e['id'])
                    if ident in seen:raise SuiteError('Repeated event across pages; server pagination changed. Start a new run.')
                    seen.add(ident)
                    if ident in global_seen and global_seen[ident]!=canonical(e):raise SuiteError('Conflicting event versions. Start a new run.')
                    if ident not in global_seen:global_seen[ident]=canonical(e);all_events.append(e)
                target=run/'raw'/f'group-{group}-page-{page:06d}.json'
                normalized={'response':[{'Event':e} for e in batch]}
                if target.exists():
                    if canonical(read(target))!=canonical(normalized):raise SuiteError('Saved page changed on server. Resume refused; start a fresh run.')
                else:save(target,normalized)
                count+=len(batch);log(run,'fetch_page',group=group,page=page,events=len(batch))
                if not batch:queries.append(dict(group=group,events=count,pages=page,exhausted=True));break
            else:raise SuiteError('Pagination bound reached; fetch incomplete.')
    save(run/'events.json',{'response':[{'Event':e} for e in all_events]})
    m.update(fetch_complete=True,fetched_utc=now(),queries=queries,events_sha256=sha(run/'events.json'),transactional_snapshot=False);save(run/'manifest.json',m)
    return prepare(run,all_events)

EXPECTED={'bank-account-nr':'bank_account_number','aba-rtn':'routing_number','phone-number':'phone_number','date-of-birth':'date_of_birth','threat-actor':'reported_actor_name','target-org':'target_organization','email':'email_address'}
def checked_predictions(record,entities,labels):
    p=source_input.packet(record['source_row']);out=[];warnings=[]
    for field,spans in entities.items():
        for v in spans:
            v=dict(v);v['field']=field;v['accepted']=False
            try:
                if field not in labels:raise ValueError('Unknown field')
                location=source_input.source_span(p,v.get('start'),v.get('end'),v.get('text'))
                v.update(accepted=True,value_start=location['start'],value_end=location['end'])
            except (ValueError,TypeError):v['reason']='Outside source value, nonliteral, invalid position, or unknown field'
            out.append(v)
    expected=EXPECTED.get(record['source_row']['source_type'])
    if expected and not any(v['accepted'] and v['field']==expected for v in out):warnings.append('Expected field missing: '+expected)
    if any(not v['accepted'] for v in out):warnings.append('Rejected raw predictions')
    return out,warnings

def assets(cfg):
    for name,rel in [('model','model-v3'),('catalog','field-catalog.json'),('vendor','')]:
        if Path(cfg[name]).resolve()!=(HERE/'assets'/rel).resolve():raise SuiteError('Use the verified bundled '+name+' asset path.')
    manifest=read(HERE/'assets'/'checksums.json')
    for rel,expected in manifest.items():
        path=HERE/'assets'/rel
        if not path.is_file() or sha(path)!=expected:raise SuiteError('Asset integrity failure: '+rel)
    if sha(Path(cfg['model'])/'model.safetensors')!='ac95258b1c2baaffd6a7dabbe5f8433c243d1b968bba693bddf6a6296db41380':raise SuiteError('Configured checkpoint is not the verified v3 model.')
    cat=read(cfg['catalog']);labels=[f['key'] for f in cat['fields'] if f['key'] not in ('observation_first_seen','observation_last_seen')]
    if len(cat['fields'])!=118 or len(labels)!=116:raise SuiteError('Field catalog changed.')
    return labels,canonical(manifest)

def journal(run,rows,identity):
    path=run/'predictions.jsonl';done={};known={r['review_id']:r for r in rows}
    if not path.exists():return done
    data=path.read_bytes();parts=data.splitlines(keepends=True);good=bytearray()
    for i,line in enumerate(parts):
        try:obj=json.loads(line)
        except (ValueError,UnicodeDecodeError):
            if i!=len(parts)-1 or line.endswith(b'\n'):raise SuiteError('Prediction journal is corrupt.') from None
            (run/('interrupted-tail-'+uuid.uuid4().hex+'.bin')).write_bytes(line)
            path.write_bytes(good);log(run,'recovered_incomplete_journal_tail');break
        rid=obj.get('review_id')
        if rid not in known or rid in done or obj.get('identity')!=identity or obj.get('input_hash')!=canonical(known[rid]['input']):raise SuiteError('Prediction journal does not match this run/model.')
        done[rid]=obj;good.extend(line)
    if done and path.read_bytes() and not path.read_bytes().endswith(b'\n'):
        with path.open('ab') as f:f.write(b'\n')
    return done

def infer(run,cfg,ui,device='auto',threads=4,max_new=None):
    m=read(run/'manifest.json')
    if not m.get('fetch_complete'):raise SuiteError('Fetch/import is incomplete.')
    if sha(run/'events.json')!=m.get('events_sha256',m.get('source_sha256')):raise SuiteError('Saved source JSON changed.')
    if sha(run/'attribute-records.jsonl')!=m['records_sha256']:raise SuiteError('Saved attribute records changed.')
    ui.update('Verifying model assets')
    labels,asset_hash=assets(cfg);identity=canonical(dict(assets=asset_hash,threshold=.5,records=m['records_sha256']))
    rows=list(lines(run/'attribute-records.jsonl'));done=journal(run,rows,identity)
    if len(done)==len(rows):
        m.update(inference_complete=True,phase='extracted',model='v3',model_sha256=sha(Path(cfg['model'])/'model.safetensors'),asset_hash=asset_hash,threshold=.5,fields=116,precision='FP32',tf32=False);save(run/'manifest.json',m);return
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false')
    sys.path.insert(0,cfg['vendor'])
    import torch
    from gliner2 import AutoExtractor
    from gliner2.processor import SamplingConfig
    actual='cuda' if device=='auto' and torch.cuda.is_available() else 'cpu' if device=='auto' else device
    tensor_device='cpu' if actual=='intel-gpu' else actual
    if actual=='cuda' and not torch.cuda.is_available():raise SuiteError('CUDA requested but unavailable. Choose CPU or auto.')
    torch.set_num_threads(threads);torch.manual_seed(42);torch.set_float32_matmul_precision('highest')
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False;torch.backends.cudnn.benchmark=False
    ui.update('Loading v3',device=actual,precision='FP32',resumed=len(done))
    start=time.perf_counter();model=AutoExtractor.from_pretrained(cfg['model'],local_files_only=True,attention_backend='standard').float().to(tensor_device).eval()
    model.processor.sampling_config=SamplingConfig(remove_json_structure_prob=0,shuffle_json_fields=False,remove_json_field_prob=0,remove_entities_prob=0,shuffle_entities=False,remove_entity_prob=0,synthetic_entity_label_prob=0,remove_relations_prob=0,swap_head_tail_prob=0,remove_classification_prob=0,shuffle_classification_labels=False,remove_classification_label_prob=0,synthetic_label_prob=0)
    if not all(p.dtype==torch.float32 and p.device.type==tensor_device for p in model.parameters()):raise SuiteError('Unexpected model device/precision.')
    backend_info={}
    device_display=actual
    if actual=='intel-gpu':
        import intel_backend
        ui.update('Compiling Intel encoder',device='Intel GPU encoder / CPU extraction head',precision='FP32')
        try:backend_info=intel_backend.attach(model,cfg['model'])
        except intel_backend.BackendError as exc:raise SuiteError(str(exc)) from exc
        device_display=backend_info['encoder_device_name']+' / CPU head'
    if actual=='cuda':torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
    load_seconds=time.perf_counter()-start
    def strict(batch):
        b=model.processor.collate_fn_inference(batch,max_len=None,architecture='boundary',error_policy='raise')
        for i,(text,_) in enumerate(batch):
            if b.original_texts[i]!=model.processor._normalize_text(text):raise SuiteError('Input contract mismatch')
            if len(b.text_tokens[i])>model.config.max_len:raise ValueError('Input exceeds supported length')
        return b
    model._inference_collator=strict
    processed=0;start=time.perf_counter();errors=sum(v['status']=='input_error' for v in done.values());warning_count=sum(bool(v['warnings']) for v in done.values())
    m.update(model='v3',model_sha256=sha(Path(cfg['model'])/'model.safetensors'),asset_hash=asset_hash,threshold=.5,fields=116,precision='FP32',tf32=False,device=actual,device_details=backend_info,torch_version=torch.__version__,cpu_threads=threads,phase='extracting')
    save(run/'manifest.json',m)
    session=dict(device=actual,device_details=backend_info,processed=0,load_seconds=load_seconds,inference_seconds=0,started_utc=now(),status='running',initial_records=len(done),application_sha256=sha(HERE/'suite.py'))
    m.setdefault('inference_sessions',[]).append(session);save(run/'manifest.json',m)
    input_cache={v['input_hash']:v for v in done.values() if v['status']=='predicted'}
    cache_hits=0
    with torch.inference_mode(),(run/'predictions.jsonl').open('a',encoding='utf8') as f:
        for r in rows:
            if r['review_id'] in done:continue
            if max_new is not None and processed>=max_new:break
            status='predicted';entities={};warnings=[]
            input_hash=canonical(r['input']);cached=input_cache.get(input_hash)
            if r['issues']:status='invalid_source';warnings=list(r['issues'])
            elif r['deleted']:status='deleted'
            elif not r['source_row']['source_value']:status='empty_value'
            elif cached is not None:
                cache_hits+=1
            else:
                try:entities=model.extract_entities(r['input'],labels,threshold=.5,include_confidence=True,include_spans=True,max_len=None).get('entities',{})
                except torch.OutOfMemoryError:raise SuiteError('Model ran out of memory. Progress saved; resume on CPU or close other applications.') from None
                except ValueError:status='input_error';warnings=['Input unsupported by model; no truncation performed'];errors+=1
                except Exception:raise SuiteError('Inference failed; completed predictions saved. Run doctor and resume. Source text withheld from logs.') from None
            if status=='predicted' and cached is not None:
                spans=cached['spans'];warnings=list(cached['warnings'])
            else:
                spans,flags=checked_predictions(r,entities,labels)
                if status=='predicted':warnings+=flags
            result=dict(review_id=r['review_id'],input_hash=canonical(r['input']),identity=identity,status=status,spans=spans,warnings=warnings)
            if status=='predicted':input_cache[input_hash]=result
            warning_count+=bool(warnings)
            f.write(json.dumps(result,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno());done[r['review_id']]=result;processed+=1
            elapsed=time.perf_counter()-start
            ui.update('Extracting',len(done),len(rows),device=device_display,cpu_threads=threads,rows_per_second=round(processed/max(elapsed,.001),2),GPU_GB=round(torch.cuda.memory_allocated()/1024**3,2) if actual=='cuda' else 'Shared system memory' if actual=='intel-gpu' else 'CPU',cache_hits=cache_hits,warnings=warning_count,errors=errors)
    if actual=='cuda':torch.cuda.synchronize()
    timing=dict(device=actual,processed=processed,cache_hits=cache_hits,load_seconds=load_seconds,inference_seconds=time.perf_counter()-start,peak_gpu_bytes=torch.cuda.max_memory_allocated() if actual=='cuda' else None,at=now())
    session.update(timing,status='complete');m.update(inference_complete=len(done)==len(rows),predicted_records=len(done),phase='extracted' if len(done)==len(rows) else 'paused');save(run/'manifest.json',m)
    log(run,'inference_session',**timing)
    if not m['inference_complete']:ui.update('Paused',len(done),len(rows),next_action='Resume this run to continue')
    import gc
    del model
    gc.collect()
    if actual=='cuda':torch.cuda.empty_cache()

def export_file(payload,dest,cfg):
    if cfg.get('excel_backend')=='openpyxl':
        from export_xlsx import write_workbook
        try:write_workbook(read(payload),dest)
        except Exception:raise SuiteError('Python Excel export failed; predictions are saved. Check openpyxl installation and available disk space.') from None
        return HERE/'export_xlsx.py'
    env=os.environ.copy();env['NCFTA_ARTIFACT_MODULES']=cfg.get('artifact_modules','')
    node=cfg.get('node') or shutil.which('node')
    if not node or not Path(node).is_file():raise SuiteError('Configure Node, or set excel_backend to openpyxl in settings.json.')
    result=subprocess.run([node,str(HERE/'export.mjs'),str(payload),str(dest)],env=env,capture_output=True,text=True,encoding='utf8')
    if result.returncode:raise SuiteError('Excel exporter failed; predictions are saved.')
    return HERE/'export.mjs'

def export(run,cfg,ui):
    m=read(run/'manifest.json')
    if not m.get('inference_complete'):raise SuiteError('Extraction is incomplete. Resume before exporting.')
    if sha(run/'attribute-records.jsonl')!=m['records_sha256']:raise SuiteError('Saved records changed.')
    rows=list(lines(run/'attribute-records.jsonl'));labels,asset_hash=assets(cfg)
    identity=canonical(dict(assets=asset_hash,threshold=.5,records=m['records_sha256']));pred=journal(run,rows,identity)
    if len(pred)!=len(rows):raise SuiteError('Missing predictions.')
    ui.update('Exporting Excel',records=len(rows))
    payload=run/'export-data.json';save(payload,dict(manifest=m,records=[dict(**r,prediction=pred[r['review_id']]) for r in rows]))
    name='review-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.xlsx';dest=run/name
    exporter=export_file(payload,dest,cfg)
    group_workbooks={}
    for group in ('6','7','8'):
        folder=run/('group-'+group);folder.mkdir(exist_ok=True)
        selected=[r for r in rows if str(r['group'])==group]
        group_events=[e for e in docs_events(read(run/'events.json')) if str(e.get('sharing_group_id',''))==group]
        save(folder/'events.json',group_events)
        for filename,items in [('attribute-records.jsonl',selected),('predictions.jsonl',[pred[r['review_id']] for r in selected])]:
            with (folder/filename).open('w',encoding='utf8') as f:
                for item in items:f.write(json.dumps(item,ensure_ascii=False)+'\n')
        gm=dict(m,groups=[group],events=len(group_events),records=len(selected),
                objects=sum(len(e.get('Object',[])) for e in group_events),
                records_sha256=sha(folder/'attribute-records.jsonl'),inference_sessions=[])
        if m.get('filters'):gm['filters']=dict(m['filters'],groups=[int(group)])
        # Parent timing covers all groups; do not present it as group-specific timing.
        gm['timing_note']='Inference timing is recorded for the combined run in the parent folder.'
        save(folder/'manifest.json',gm)
        if not selected:
            (folder/'NO-DATA.txt').write_text('No attributes for this group in this run. No model inference or workbook for this group.\n',encoding='utf8')
            continue
        (folder/'NO-DATA.txt').unlink(missing_ok=True)
        save(folder/'export-data.json',dict(manifest=gm,records=[dict(**r,prediction=pred[r['review_id']]) for r in selected]))
        ui.update('Exporting Excel',records=len(selected),group=group)
        export_file(folder/'export-data.json',folder/name,cfg)
        group_workbooks[group]=str(Path('group-'+group)/name)
    m['group_workbooks']=group_workbooks
    m['last_workbook']=name;m['phase']='complete';m['exporter_sha256']=sha(exporter);save(run/'manifest.json',m)
    ui.update('Complete',len(rows),len(rows),workbook=name)
    log(run,'export_complete',file=name);return dest

def new_run(cfg,run=None,filters=None,source=None):
    if shutil.disk_usage(HERE).free<512*1024**2:raise SuiteError('Less than 512 MB disk space remains. Free space before starting a run.')
    run=Path(run).resolve() if run else Path(cfg['runs'])/(dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    if run.exists():raise SuiteError('Run directory already exists. Use resume or choose another directory.')
    run.mkdir(parents=True)
    for group in (6,7,8):(run/f"group-{group}").mkdir()
    save(run/'manifest.json',dict(version=1,run_id=run.name,created_utc=now(),filters=filters,source=source,fetch_complete=False,inference_complete=False,phase='created',remote_writes=0,ground_truth_available=False))
    return run

def import_json(run,path,ui):
    path=Path(path)
    if not path.is_file():raise SuiteError('Input JSON file does not exist.')
    ui.update('Importing saved JSON')
    doc=read(path);events=docs_events(doc);shutil.copyfile(path,run/'events.json')
    m=read(run/'manifest.json');m.update(fetch_complete=True,source_sha256=sha(run/'events.json'),source='local_json',event_dates=sorted({str(e.get('date','')) for e in events}),groups=sorted({str(e.get('sharing_group_id','')) for e in events}));save(run/'manifest.json',m)
    return prepare(run,events)

def doctor(cfg):
    from rich.console import Console
    from rich.table import Table
    table=Table(title='NCFTA Review Suite: local readiness');table.add_column('Check');table.add_column('Result')
    for module in ('rich','requests','pymisp','torch'):
        table.add_row(module,'Available' if importlib.util.find_spec(module) else 'Missing')
    try:labels,_=assets(cfg);table.add_row('v3 assets',f'Verified; {len(labels)} extraction fields')
    except Exception:table.add_row('v3 assets','Missing or checksum mismatch')
    if importlib.util.find_spec('torch'):
        import torch
        table.add_row('Device',torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')
    import intel_backend
    try:
        result=intel_backend.probe()
        table.add_row('Intel GPU',', '.join(d['name'] for d in result['intel_gpus']) or 'Not exposed by the installed driver')
        intel_backend.verify_ir();table.add_row('Intel FP32 encoder','Checksum verified; opt-in backend')
    except intel_backend.BackendError as exc:table.add_row('Intel backend',str(exc))
    table.add_row('Free space',f'{shutil.disk_usage(HERE).free/1024**3:.1f} GB')
    table.add_row('Credential file','Present (not read)' if Path(cfg['env']).is_file() else 'Not found')
    table.add_row('Fetch transport',transport(cfg)+' (auto prefers PyMISP)')
    if cfg.get('excel_backend')=='openpyxl':
        table.add_row('Excel export','openpyxl available' if importlib.util.find_spec('openpyxl') else 'Missing: run Install-Work-PC.bat')
    else:
        table.add_row('Excel Node', 'Found' if Path(cfg.get('node','')).is_file() else 'Missing')
        table.add_row('Excel package','Found' if (Path(cfg.get('artifact_modules',''))/'@oai/artifact-tool/package.json').is_file() else 'Missing')
    terminal_console().print(table)

def open_output_folder(folder):
    folder=Path(folder).resolve()
    if not folder.is_dir():raise SuiteError('Output folder does not exist.')
    os.startfile(str(folder))

def show_run_summary(run):
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    m=read(run/'manifest.json')
    table=Table.grid(padding=(0,2),expand=True)
    table.add_column(style='dim');table.add_column(ratio=1)
    table.add_row('Run folder',Text(str(run)))
    table.add_row('Attributes',str(m.get('records','—')))
    if m.get('group_workbooks'):table.add_row('Group folders','group-6 / group-7 / group-8')
    if m.get('last_workbook'):
        table.add_row('Excel file',Text(str(run/m['last_workbook']),style='bold green'))
        title='Excel review ready';color='green'
    else:
        title='Progress saved';color='yellow'
        table.add_row('Next step','Choose Resume a run from the home menu.')
    terminal_console().print(Panel(table,title=title,border_style=color,padding=(1,2)))

def parse_event_date(value):
    value=value.strip()
    for fmt in ('%Y-%m-%d','%m/%d/%Y'):
        try:return dt.datetime.strptime(value,fmt).date()
        except ValueError:pass
    raise argparse.ArgumentTypeError('Enter a valid date as YYYY-MM-DD or MM/DD/YYYY (for example, 2026-11-15 or 11/15/2026).')

def interactive(cfg):
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.panel import Panel
    c=terminal_console();c.print();c.print(menu_view());c.print()
    def ask(label,**kwargs):return Prompt.ask(label,console=c,**kwargs)
    def with_device(args):
        c.print('[dim]auto: NVIDIA GPU if available, otherwise CPU. Intel GPU is opt-in; use Check Intel GPU first.[/dim]')
        device=ask('[bold cyan]Processing device[/bold cyan]',choices=['auto','cpu','cuda','intel-gpu'],default='auto')
        return args if device=='auto' else [*args,'--device',device]
    choice=ask('[bold cyan]  Choose workflow[/bold cyan]',choices=['1','2','3','4','5','6','7','0'],default='1')
    if choice=='0':return None
    if choice=='5':return ['doctor']
    if choice=='6':return ['outputs']
    if choice=='7':return ['check-intel']
    if choice=='1':
        c.print(Panel('Enter an inclusive event date range, then select groups.\n[dim]Dates: YYYY-MM-DD or MM/DD/YYYY · Groups: 6, 7, 8[/dim]',title='[bold cyan]Fetch & extract[/bold cyan]',border_style='cyan'))
        def ask_date(label,minimum=None):
            while True:
                try:value=parse_event_date(ask('[bold cyan]'+label+'[/bold cyan]'))
                except argparse.ArgumentTypeError as exc:
                    c.print(str(exc),style='yellow');continue
                if minimum and value<minimum:
                    c.print('End date must be on or after the start date.',style='yellow');continue
                return value.isoformat()
        start=ask_date('Start event date')
        end=ask_date('End event date',dt.date.fromisoformat(start))
        while True:
            groups=ask('[bold cyan]Groups[/bold cyan] [dim](all = 6, 7, 8)[/dim]',default='all').strip().lower().replace(',',' ').split()
            if groups==['all']:groups=['6','7','8']
            if groups and all(g in ('6','7','8') for g in groups):break
            c.print('Enter all, or groups 6, 7, 8 separated by spaces or commas.',style='yellow')
        return with_device(['run','--from-date',start,'--to-date',end,'--groups',*groups])
    if choice=='2':return with_device(['import','--json',ask('[bold cyan]Saved JSON path[/bold cyan]').strip('"')])
    args=['resume' if choice=='3' else 'export','--run',ask('[bold cyan]Run folder path[/bold cyan]').strip('"')]
    return with_device(args) if choice=='3' else args

def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    cfg=settings();argv=sys.argv[1:] if argv is None else argv
    interactive_mode=not argv
    if not argv:
        argv=interactive(cfg)
        if argv is None:return 0
    p=argparse.ArgumentParser(description='NCFTA date-range fetch, v3 extraction, and Excel review')
    sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('doctor')
    sub.add_parser('outputs')
    check=sub.add_parser('check-intel',help='Compare the same saved inputs on CPU and Intel GPU; no default changes')
    check.add_argument('--run',type=Path);check.add_argument('--samples',type=int,default=20);check.add_argument('--threads',type=int,default=4)
    check.add_argument('--resume-check',type=Path,help='Resume a failed check without repeating completed CPU predictions')
    for name in ['run','fetch','import','resume','infer','export']:
        s=sub.add_parser(name);s.add_argument('--run',type=Path,required=name in ('resume','infer','export'))
        if name in ('run','fetch'):
            s.add_argument('--from-date',required=True,type=parse_event_date);s.add_argument('--to-date',required=True,type=parse_event_date);s.add_argument('--groups',type=int,nargs='+',default=[6,7,8],choices=[6,7,8])
        if name=='import':s.add_argument('--json',required=True,type=Path)
        if name not in ('fetch','export'):
            s.add_argument('--device',choices=['auto','cpu','cuda','intel-gpu'],default='auto');s.add_argument('--threads',type=int,default=4);s.add_argument('--max-new',type=int,help='Stop after this many new attributes; resume later')
    a=p.parse_args(argv)
    if a.action=='doctor':doctor(cfg);return 0
    if a.action=='outputs':
        folder=Path(cfg['runs']);folder.mkdir(parents=True,exist_ok=True)
        open_output_folder(folder);return 0
    if a.action=='check-intel':
        if a.samples<2 or a.threads<1:p.error('Use at least two samples and one CPU thread.')
        try:
            from intel_check import run_check
            source=a.run or choose_check_run(cfg)
            result=run_check(cfg,source,a.samples,a.threads,a.resume_check)
            if a.run is None:open_output_folder(result)
            return 0
        except KeyboardInterrupt:
            print('Check stopped. Saved results remain in the checks folder.');return 130
        except Exception as exc:
            print(str(exc) if isinstance(exc,SuiteError) else 'Intel check failed ('+type(exc).__name__+'). See checks/report.json.');return 1
    if getattr(a,'threads',1)<1 or (getattr(a,'max_new',None) is not None and a.max_new<1):p.error('Threads and max-new must be positive.')
    run=None
    try:
        if a.action in ('run','fetch'):
            if a.from_date>a.to_date:p.error('Start date must be on or before end date.')
            run=new_run(cfg,a.run,dict(**{'from':str(a.from_date),'to':str(a.to_date)},groups=sorted(set(a.groups)),date_basis='inclusive MISP event date'))
        elif a.action=='import':run=new_run(cfg,a.run,source='local_json')
        else:
            run=a.run.resolve()
            if not (run/'manifest.json').is_file():raise SuiteError('Not a suite run folder.')
        with lock(run),Dashboard(run) as ui:
            if a.action in ('run','fetch'):fetch(run,cfg,ui)
            elif a.action=='import':import_json(run,a.json,ui)
            elif a.action=='resume':
                m=read(run/'manifest.json')
                if not m['fetch_complete']:
                    if not m.get('filters'):raise SuiteError('Local import was interrupted; start a new import.')
                    fetch(run,cfg,ui)
                elif not (run/'attribute-records.jsonl').exists() or not m.get('records_sha256'):prepare(run,docs_events(read(run/'events.json')))
            if a.action=='fetch':ui.update('Fetch complete',records=read(run/'manifest.json')['records'])
            elif a.action=='export':export(run,cfg,ui)
            else:
                infer(run,cfg,ui,a.device,a.threads,a.max_new)
                if a.action!='infer' and read(run/'manifest.json')['inference_complete']:export(run,cfg,ui)
        show_run_summary(run)
        if interactive_mode:
            from rich.prompt import Confirm
            if Confirm.ask('Open folder?',default=True,console=terminal_console()):open_output_folder(run)
        return 0
    except KeyboardInterrupt:
        if run:
            close_interrupted_session(run)
            log(run,'interrupted');save(run/'status.json',dict(phase='interrupted',updated_utc=now()))
        print('Interrupted. Completed pages/predictions retained. Use resume.');return 130
    except Exception as exc:
        message=safe_failure_message(exc)
        if run:
            import traceback
            close_interrupted_session(run)
            log(run,'failed',error_type=type(exc).__name__,frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)])
            save(run/'status.json',dict(phase='failed',message=message,updated_utc=now()))
        print(message);return 1

def choose_check_run(cfg):
    from rich.prompt import Prompt
    from rich.table import Table
    candidates=[]
    for path in Path(cfg['runs']).glob('*/manifest.json'):
        try:
            m=read(path)
            if m.get('fetch_complete') and (path.parent/'attribute-records.jsonl').is_file():candidates.append((path.stat().st_mtime,path.parent,m))
        except (OSError,ValueError):continue
    candidates=sorted(candidates,key=lambda item:item[0],reverse=True)[:15]
    if not candidates:raise SuiteError('No saved inputs found. Copy an existing run folder into runs, or fetch/import data first.')
    table=Table(title='Choose saved inputs for the Intel GPU check')
    for label in ('#','Run','Attributes'):table.add_column(label)
    for i,(_,folder,m) in enumerate(candidates,1):table.add_row(str(i),folder.name,str(m.get('records',0)))
    console=terminal_console();console.print(table)
    choice=Prompt.ask('Run',choices=[str(i) for i in range(1,len(candidates)+1)],default='1',console=console)
    return candidates[int(choice)-1][1]

def close_interrupted_session(run):
    if not (run/'manifest.json').exists():return
    m=read(run/'manifest.json');sessions=m.get('inference_sessions',[])
    if sessions and sessions[-1].get('status')=='running':
        s=sessions[-1];s.update(status='interrupted',inference_seconds=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(s['started_utc'])).total_seconds())
        count=0
        if (run/'predictions.jsonl').exists():
            with (run/'predictions.jsonl').open(encoding='utf8') as f:
                for line in f:
                    try:json.loads(line);count+=1
                    except ValueError:break
        s['processed']=max(0,count-s['initial_records'])
        save(run/'manifest.json',m)

if __name__=='__main__':
    # Optional check modules share this instance and its SuiteError type.
    sys.modules.setdefault('suite',sys.modules[__name__])
    raise SystemExit(main())
