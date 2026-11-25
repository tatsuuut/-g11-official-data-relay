"""Fixed-scope, saved-only input audit. No prediction imports or state writer."""
import argparse
import copy
import gzip
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import urllib.parse
import urllib.request

REPO = 'tatsuuut/-g11-official-data-relay'
HERE = Path(__file__).resolve().parent
TARGETS = HERE / 'mukiken_saved_input_audit_20261125_targets.json'
TARGET_SHA = '8c8dedda8b85416bf6287b35b97a81c5685664991acbb2af4db8e8d7b2d2b8f1'
OUT = Path('.audit')
MAX_OUTPUT = 5 * 1024 * 1024 - 8192
MAX_RECORD = 240 * 1024
MAX_MEMBER = 32 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 ** 3
MAX_MEMBERS = 200000
MAX_CIPHER = 1024 ** 3
MAX_DOWNLOAD = 4 * 1024 ** 3
FIELDS = ('標準レースキー','艇番','当地勝率','モーター2連率','全国勝率','全国2連率',
          '平均ST','今節着順','今節ST','取得時刻')
RECEIPT_FIELDS = ('SCHEMA','SOURCE_ID','SHA256','SIZE_BYTES','CAPTURED_AT_JST','STATUS','HOST')


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def compact(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()


def snapshot_bytes(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()


def targets():
    raw = TARGETS.read_bytes()
    require(digest(raw) == TARGET_SHA, 'TARGET_MANIFEST_CHANGED')
    rows = json.loads(raw)['rows']
    require(len(rows) == len({r['key'] for r in rows}) == 16, 'TARGET_COUNT')
    return {r['key']:r for r in rows}


def load(name, default):
    directory=out_fd()
    try:
        try: descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        except FileNotFoundError:return copy.deepcopy(default)
        with os.fdopen(descriptor,'rb') as handle:
            require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode),'AUDIT_FILE_TYPE')
            return json.load(handle)
    finally:os.close(directory)


def out_fd():
    absolute=OUT if OUT.is_absolute() else Path.cwd()/OUT
    for parent in [*reversed(absolute.parents),absolute]:
        require(not parent.is_symlink(),'AUDIT_SYMLINK')
    absolute.mkdir(mode=0o700,exist_ok=True)
    return os.open(absolute,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)


def save(name, value):
    require(re.fullmatch(r'[a-z_]+\.json',name),'AUDIT_OUTPUT_NAME')
    directory=out_fd();temporary='temporary-'+str(os.getpid())+'-'+name
    try:
        try:require(stat.S_ISREG(os.stat(name,dir_fd=directory,follow_symlinks=False).st_mode),'AUDIT_FILE_TYPE')
        except FileNotFoundError:pass
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(descriptor,'wb') as handle:
            handle.write(compact(value));handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory);os.fsync(directory)
    finally:
        try:os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


def output(key, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as handle:
            handle.write(key+'='+str(value)+'\n')


def receipts(envelope, key):
    all_rows = envelope.get('SOURCE_RECEIPTS',[])
    require(type(all_rows) is list, 'RECEIPTS_TYPE')
    return dict(count=len(all_rows), set_sha256=digest(compact(all_rows)),
        matching=[{k:r[k] for k in RECEIPT_FIELDS if k in r}
                  for r in all_rows if key in str(r.get('SOURCE_ID',''))])


def proof(path, raw, envelope, key):
    return dict(path=path,original_member_sha256=digest(raw),original_member_bytes=len(raw),
        canonical_definition='UTF8_json_ensure_ascii_false_sort_keys_true_indent2_allow_nan_false_LF',
        canonical_reencode_sha256=digest(snapshot_bytes(envelope)),
        canonical_roundtrip_bytes_match=snapshot_bytes(envelope)==raw,
        source_receipts=receipts(envelope,key))


def metadata(prefix):
    token = os.environ.get('GH_TOKEN','')
    require(bool(token),'METADATA_AUTH_MISSING')
    url = 'https://api.github.com/repos/'+REPO+'/actions/caches?'+urllib.parse.urlencode({'key':prefix,'ref':'refs/heads/main','per_page':100})
    request = urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
    with urllib.request.urlopen(request,timeout=25) as response:
        value = json.load(response)
    return [r for r in value.get('actions_caches',[]) if r.get('ref')=='refs/heads/main'
            and str(r.get('key','')).startswith(prefix)]


def plan():
    rows = targets()
    days = sorted({r['day'] for r in rows.values()})
    requests = [('latest','2026-11-25')]+[(d.replace('-',''),d) for d in days]
    planned = {}
    for label,day in requests:
        try:
            found = metadata('g11-state-v4-published-'+day+'-')
            pattern = re.compile(r'g11-state-v4-published-'+day+r'-\d+(?:-\d+-checkpoint)?')
            found = [r for r in found if pattern.fullmatch(r['key'])]
            original_runs={r['source_run_id'] for r in rows.values() if r['day']==day}
            exact=[r for r in found if any(('-'+str(run)) in r['key'] for run in original_runs)]
            selected=max(exact or found,key=lambda r:(r.get('created_at',''),r.get('id',0))) if found else None
            status='AVAILABLE' if selected else 'NO_RETAINED_CHECKPOINT'
            if selected and selected['size_in_bytes'] > MAX_CIPHER:
                status='CHECKPOINT_SIZE_BUDGET';selected=None
            planned[label]=dict(day=day,status=status,key=selected['key'] if selected else '',
                cache_id=selected['id'] if selected else None,
                cipher_bytes=selected['size_in_bytes'] if selected else 0)
        except Exception as exc:
            planned[label]=dict(day=day,status='METADATA_UNAVAILABLE_'+type(exc).__name__,key='',cipher_bytes=0)
        output('key_'+label,planned[label]['key'])
    save('plan.json',planned)
    save('working.json',{'records':{},'attempts':[],'downloaded_bytes':0})
    print('AUDIT_PLAN='+json.dumps({k:{'status':v['status'],'cipher_bytes':v['cipher_bytes']} for k,v in planned.items()},sort_keys=True))


def needed(label):
    rows=targets(); working=load('working.json',{}); entry=load('plan.json',{})[label]
    remaining=[k for k,r in rows.items() if working.get('records',{}).get(k,{}).get('status')!='COMPLETE'
               and (label=='latest' or r['day']==entry['day'])]
    allowed=bool(remaining and entry['key'] and working.get('downloaded_bytes',0)+entry['cipher_bytes']<=MAX_DOWNLOAD)
    output('needed','true' if allowed else 'false')
    if not allowed:
        working.setdefault('attempts',[]).append({'label':label,'status':entry['status'] if not entry['key'] else 'SKIPPED_COMPLETE_OR_DOWNLOAD_BUDGET'})
        save('working.json',working)


def cipher(label):
    path=Path('.relay-cache/state.tgz.gpg');entry=load('plan.json',{})[label]
    working=load('working.json',{})
    try:
        require(not path.parent.is_symlink() and not path.is_symlink(),'CIPHER_SYMLINK')
        info=path.lstat();require(stat.S_ISREG(info.st_mode),'CIPHER_TYPE')
        require(0<info.st_size<=MAX_CIPHER,'CIPHER_SIZE_BUDGET')
        require(working.get('downloaded_bytes',0)+info.st_size<=MAX_DOWNLOAD,'CIPHER_TOTAL_DOWNLOAD_BUDGET')
        hasher=hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):hasher.update(chunk)
        plan=load('plan.json',{});plan[label].update(actual_cipher_bytes=info.st_size,actual_cipher_sha256=hasher.hexdigest())
        save('plan.json',plan);return True
    except (ValueError,OSError) as exc:
        reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        save('cipher_failure.json',{'label':label,'reason':reason});return False


def stage_data(path,raw,envelope,key,stage,target):
    expected=target['formal_morning_sha' if stage=='morning' else 'formal_live_sha']
    require(digest(raw)==expected,'ORIGINAL_SNAPSHOT_SHA_MISMATCH')
    payload=envelope['PAYLOAD'];result=payload['RESULT']
    require(envelope['STAGE']==('MORNING' if stage=='morning' else 'PREDEADLINE')
            and envelope['NAME']=='abeken-v53-shadow-'+('morning-' if stage=='morning' else 'live-')+key,'SNAPSHOT_ENVELOPE_BINDING')
    require(payload.get('RACE_KEY')==key and payload.get('FORMAL_USE') is True
            and all(payload.get(k) is False for k in ('RESULT_FEEDBACK_TO_MODEL','P3_INPUT_USED','WILD_INPUT_USED')),'FORMAL_MUKI_BINDING')
    require(result.get('RACE_ID')==key and result.get('RACE_DATE')==target['day']
            and result.get('STATUS')=='READY' and result.get('LEGACY_45_FALLBACK') is False,'RESULT_BINDING')
    expected_time=target['formal_morning_time' if stage=='morning' else 'formal_live_time']
    require(result.get('LOCKED_AT')==expected_time,'LOCK_TIME_BINDING')
    require(datetime.fromisoformat(expected_time)<datetime.fromisoformat(target['deadline_jst']),'LATE_LOCK')
    if stage=='live':
        require(result.get('PRE_RACE_DATA_CAPTURED_AT')==target['formal_live_capture'],'CAPTURE_TIME_BINDING')
    wanted=('CANDIDATE_INPUT','HEAD_FEATURES','RIGHT_TRANSITION_AUDIT','PRE_RACE_CANDIDATE_INPUT',
            'PRE_RACE_DATA_CAPTURED_AT','LOCKED_AT','RUNTIME_LOGIC_VERSION','RUNTIME_DISPLAY_VERSION','SNAPSHOT_TYPE')
    data={k:result[k] for k in wanted if k in result}
    return dict(data=data,proof={**proof(path,raw,envelope,key),'known_formal_snapshot_sha256':expected,
                 'known_original_hash_matched':True},
                bindings={k:payload[k] for k in ('INPUT_SNAPSHOT_SHA256','MORNING_V53_SNAPSHOT_SHA256','BEFOREINFO_SNAPSHOT_SHA256') if k in payload})


def scan(label):
    # Stage selected data only. The caller commits it only after GPG authentication succeeds.
    fixed=targets();working=load('working.json',{})
    active={k:v for k,v in fixed.items() if working.get('records',{}).get(k,{}).get('status')!='COMPLETE'}
    stages={}; befores={}; day_inputs={}; response_hashes={}; authorities={}; errors=[]
    class BoundedReader:
        def __init__(self,source):self.source=source;self.count=0
        def read(self,n=-1):
            data=self.source.read(n);self.count+=len(data)
            require(self.count<=MAX_EXPANDED,'ARCHIVE_EXPANDED_BUDGET');return data
    member_count=declared_bytes=0;seen=set();selected_hashes={};duplicates={}
    decompressed=gzip.GzipFile(fileobj=sys.stdin.buffer,mode='rb');bounded=BoundedReader(decompressed)
    with tarfile.open(fileobj=bounded,mode='r|') as archive:
        for member in archive:
            member_count+=1;declared_bytes+=member.size
            require(member_count<=MAX_MEMBERS and 0<=declared_bytes<=MAX_EXPANDED,'ARCHIVE_HEADER_BUDGET')
            name=member.name.removeprefix('./');p=PurePosixPath(name)
            require(not p.is_absolute() and '..' not in p.parts,'ARCHIVE_PATH')
            name=str(p);duplicate=name in seen;seen.add(name)
            if not member.isfile():
                require(not duplicate,'ARCHIVE_DUPLICATE_PATH')
                continue
            # Accept only canonical or existing rescue stores; no extraction to disk.
            relative=name.removeprefix('rescue/')
            hit=re.fullmatch(r'store/snapshots/(morning|predeadline)/abeken-v53-shadow-(morning|live)-(\d{8}-\d{2}-\d{2})\.json',relative)
            source_hit=re.fullmatch(r'store/snapshots/predeadline/(?:beforeinfo|published-exhibition)-(\d{8}-\d{2}-\d{2})\.json',relative)
            response_hit=re.fullmatch(r'store/responses/beforeinfo-(\d{8}-\d{2}-\d{2})/response\.bin',relative)
            authority_hit=re.fullmatch(r'daily-runtime/(\d{4}-\d{2}-\d{2})/authority\.json',relative)
            wanted_authority=bool(authority_hit and authority_hit.group(1) in {r['day'] for r in active.values()})
            day_input=relative=='store/snapshots/morning/official-day-input.json'
            key=(hit.group(3) if hit else source_hit.group(1) if source_hit else response_hit.group(1) if response_hit else None)
            if not ((key in active and (hit or source_hit or response_hit)) or day_input or wanted_authority):
                require(not duplicate,'ARCHIVE_DUPLICATE_PATH')
                continue
            if member.size>MAX_MEMBER:
                errors.append({'path':name,'status':'MEMBER_SIZE_BUDGET'});continue
            raw=archive.extractfile(member).read(MAX_MEMBER+1)
            require(len(raw)==member.size,'MEMBER_SIZE')
            logical=relative
            if logical in selected_hashes:
                duplicates[logical]=duplicates.get(logical,1)+1
                require(duplicates[logical]<=3 and selected_hashes[logical]==digest(raw),'ARCHIVE_CONFLICTING_DUPLICATE')
                continue
            require(not duplicate,'ARCHIVE_DUPLICATE_PATH');selected_hashes[logical]=digest(raw)
            if response_hit:
                response_hashes[key]=dict(path=name,sha256=digest(raw),size=len(raw));continue
            try:
                envelope=json.loads(raw)
                if wanted_authority:
                    authority_day=authority_hit.group(1);unsigned=dict(envelope)
                    stated=unsigned.pop('authority_sha256',None)
                    actual=digest(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
                    expected=next(r for r in active.values() if r['day']==authority_day)
                    require(stated==actual==expected['authority_sha256']
                            and envelope.get('operational_date_jst')==authority_day
                            and envelope.get('runtime_source_sha')==expected['runtime_source_sha']
                            and envelope.get('immutable') is True,'DAILY_AUTHORITY_BINDING')
                    authorities[authority_day]={'path':name,'original_member_sha256':digest(raw),
                        'authority_sha256':actual,'runtime_source_sha':envelope['runtime_source_sha'],'original_authority_rehashed':True}
                    continue
                require(type(envelope) is dict and set(envelope)=={'SCHEMA','STAGE','NAME','SOURCE_SHA256','SOURCE_RECEIPTS','PAYLOAD'},'SNAPSHOT_SHAPE')
                if hit:
                    stage=hit.group(2)
                    require(hit.group(1)==('morning' if stage=='morning' else 'predeadline'),'SNAPSHOT_PATH_STAGE')
                    stages.setdefault(key,{})[stage]=stage_data(name,raw,envelope,key,stage,active[key])
                elif source_hit:
                    befores.setdefault(key,[]).append((name,digest(raw),envelope,proof(name,raw,envelope,key)))
                elif day_input:
                    payload=envelope['PAYLOAD'];fields=payload.get('RAW38_FIELDS',[])
                    selected={k:[] for k in active}
                    for row in payload.get('RAW38_ROWS',[]):
                        if type(row) is not list or len(row)!=len(fields):continue
                        value=dict(zip(fields,row));k=value.get('標準レースキー')
                        if k in active:selected[k].append({f:value[f] for f in FIELDS})
                    day_inputs[digest(raw)]={'rows':selected,'proof':proof(name,raw,envelope,'UNEXPORTED_GLOBAL_RECEIPTS')}
            except Exception as exc:
                errors.append({'path':name,'status':str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z0-9_]+',str(exc)) else type(exc).__name__})
    # Drain through GzipFile to verify CRC/ISIZE, bounded expansion and complete GPG EOF.
    while bounded.read(1024*1024):pass
    decompressed.close()
    result={}
    for key,target in active.items():
        existing=copy.deepcopy(working.get('records',{}).get(key,{}))
        combined={**existing,**stages.get(key,{})};missing=[]
        if 'morning' in combined:
            input_sha=combined['morning']['bindings'].get('INPUT_SNAPSHOT_SHA256')
            source=day_inputs.get(input_sha)
            if source and len(source['rows'].get(key,[]))==6:
                raw_rows=source['rows'][key]
                boats_ok=all(type(r.get('艇番')) is int for r in raw_rows) and sorted(r['艇番'] for r in raw_rows)==list(range(1,7))
                try:
                    times=[datetime.fromisoformat(r['取得時刻']) for r in raw_rows]
                    time_ok=all(t.tzinfo is not None and t<=datetime.fromisoformat(target['formal_morning_time']) for t in times)
                except (ValueError,KeyError,TypeError):time_ok=False
                if boats_ok and time_ok:
                    combined['official_rows']={'data':sorted(raw_rows,key=lambda r:r['艇番']),'proof':source['proof'],
                        'six_unique_boats_verified':True,'feature_times_aware_and_not_after_morning_lock':True}
        if 'live' in combined:
            expected=combined['live']['bindings'].get('BEFOREINFO_SNAPSHOT_SHA256')
            for name,sha,envelope,srcproof in befores.get(key,[]):
                payload=envelope['PAYLOAD']
                if sha!=expected or payload.get('RACE_KEY')!=key or payload.get('KIND')!='BEFOREINFO' or payload.get('CAPTURED_AT_JST')!=target['formal_live_capture']:continue
                parsed=payload.get('PARSED',{})
                require(type(parsed) is dict and set(parsed)=={'RACERS','WEATHER','AIR_TEMPERATURE','WIND_SPEED','WIND_DIRECTION_CODE','WATER_TEMPERATURE','WAVE_HEIGHT'},'BEFOREINFO_FIELDS')
                racers=parsed.get('RACERS');require(type(racers) is list and len(racers)==6,'BEFOREINFO_RACERS')
                require(sorted(r['BOAT'] for r in racers)==list(range(1,7)) and sorted(r['COURSE'] for r in racers)==list(range(1,7)),'BEFOREINFO_COURSES')
                require(all(set(r)=={'BOAT','WEIGHT','EXHIBITION_TIME','TILT','COURSE','START_TIMING'} for r in racers),'BEFOREINFO_RACER_FIELDS')
                combined['beforeinfo']={'data':parsed,'proof':srcproof,'captured_at_jst':payload['CAPTURED_AT_JST']}
                matching=srcproof['source_receipts']['matching'];original=response_hashes.get(key)
                combined['beforeinfo']['official_response_byte_rehash']={'available':bool(original),
                    'proof':original,'matches_bound_receipt':bool(original and any(r.get('SHA256')==original['sha256'] and r.get('SIZE_BYTES')==original['size'] for r in matching))}
        for field in ('morning','live','official_rows','beforeinfo'):
            if field not in combined:missing.append(field)
        if not combined.get('morning',{}).get('data',{}).get('CANDIDATE_INPUT',{}).get('HEAD_FEATURES'):missing.append('HEAD_FEATURES')
        if not combined.get('live',{}).get('data',{}).get('RIGHT_TRANSITION_AUDIT'):missing.append('RIGHT_TRANSITION_AUDIT')
        combined.update(key=key,day=target['day'],status='COMPLETE' if not missing else 'PARTIAL',missing=missing)
        combined['daily_source_authority']=authorities.get(target['day'],existing.get('daily_source_authority',{
            'original_authority_rehashed':False,'status':'ORIGINAL_AUTHORITY_NOT_IN_THIS_CHECKPOINT',
            'expected_authority_sha256':target['authority_sha256'],'expected_runtime_source_sha':target['runtime_source_sha']}))
        if len(compact(combined))>MAX_RECORD:
            combined={'key':key,'day':target['day'],'status':'OUTPUT_RECORD_BUDGET','missing':['record_over_240KiB']}
        result[key]=combined
    save('pending.json',{'records':result,'errors':errors,'label':label,
        'stream_budget':{'members':member_count,'declared_bytes':declared_bytes,'expanded_bytes_read':bounded.count,'identical_duplicates':duplicates,'gzip_crc_and_eof_verified':True}})


def commit(label):
    pending=load('pending.json',{});require(pending.get('label')==label,'PENDING_BINDING')
    working=load('working.json',{});entry=load('plan.json',{})[label]
    working.setdefault('records',{}).update(pending['records'])
    working.setdefault('attempts',[]).append({**entry,'label':label,'gpg_authenticated':True,'scan_errors':pending['errors'],'stream_budget':pending['stream_budget']})
    working['downloaded_bytes']=working.get('downloaded_bytes',0)+entry.get('actual_cipher_bytes',entry['cipher_bytes'])
    save('working.json',working);(OUT/'pending.json').unlink()
    print('AUDIT_AUTHENTICATED_COMPLETE_KEYS='+str(sum(r.get('status')=='COMPLETE' for r in working['records'].values())))


def failed(label):
    working=load('working.json',{});entry=load('plan.json',{})[label]
    failure=load('cipher_failure.json',{})
    reason=failure.get('reason') if failure.get('label')==label else 'RESTORE_OR_AUTHENTICATION_FAILED'
    working.setdefault('attempts',[]).append({**entry,'label':label,'status':reason,'gpg_authenticated':False})
    working['downloaded_bytes']=working.get('downloaded_bytes',0)+entry.get('actual_cipher_bytes',entry['cipher_bytes'])
    save('working.json',working);(OUT/'pending.json').unlink(missing_ok=True)


def finish():
    fixed=targets();working=load('working.json',{})
    rows=[working.get('records',{}).get(k,{'key':k,'day':r['day'],'status':'NOT_RECOVERED','missing':['original_checkpoint_inputs']}) for k,r in fixed.items()]
    payload={'schema':'G11_FIXED_16_SAVED_INPUT_READ_ONLY_EXPORT_V1','target_manifest_sha256':TARGET_SHA,
        'record_count':16,'records':rows,'manifest':{'attempts':working.get('attempts',[]),
        'prediction_calls':0,'official_http':0,'observer_calls':0,'canonical_state_writes':0,'cache_saves':0,
        'original_bytes_hash_definition':'SHA256 of exact selected tar-member bytes; compared with previously fixed public formal snapshot identity',
        'filtered_records_have_distinct_hashes':{r['key']:digest(compact(r)) for r in rows},
        'downloaded_cipher_bytes':working.get('downloaded_bytes',0),'plaintext_export_limit_bytes':MAX_OUTPUT}}
    raw=compact(payload);require(len(raw)<=MAX_OUTPUT,'OUTPUT_5MiB_BUDGET')
    save('selected.json',payload)
    print('AUDIT_EXPORT='+json.dumps({'records':16,'complete':sum(r['status']=='COMPLETE' for r in rows),'plaintext_bytes':len(raw),'sha256':digest(raw)},sort_keys=True))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['plan','needed','cipher','scan','commit','failed','finish']);parser.add_argument('--label',default='')
    args=parser.parse_args();descriptor=out_fd();os.close(descriptor)
    if args.mode=='plan':plan()
    elif args.mode=='needed':needed(args.label)
    elif args.mode=='cipher':sys.exit(0 if cipher(args.label) else 1)
    elif args.mode=='scan':scan(args.label)
    elif args.mode=='commit':commit(args.label)
    elif args.mode=='failed':failed(args.label)
    else:finish()


if __name__=='__main__':
    main()
