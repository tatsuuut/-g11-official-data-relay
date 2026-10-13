import contextlib, hashlib, io, json, os
from pathlib import Path
import tarfile, tempfile, textwrap, types, unittest
from unittest.mock import patch
WORKFLOW=Path(__file__).with_name('g11-wild4-shadow-lock-only.yml')
y=WORKFLOW.read_text();section=y.split('      - name: Restore and verify earlier immutable SHADOW locks',1)[1].split('\n      - name:',1)[0]
CODE=textwrap.dedent(section.split("          python3 - <<'PY'\n",1)[1].split('\n          PY',1)[0])
def sealed(v):
 v={k:x for k,x in v.items() if k!='CONTENT_SHA256'}
 return {**v,'CONTENT_SHA256':hashlib.sha256((json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()).hexdigest()}
def read(p):
 v=json.loads(p.read_text())
 if v!=sealed(v):raise ValueError('LIFECYCLE_SHA')
 return v
class CheckpointRestore(unittest.TestCase):
 def run_case(self,kind='valid'):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); src=root/'src/g11-wild4-future-locks';src.mkdir(parents=True)
   model={'identity':'a'*64,'model_sha256':'a'*64}
   pin={'schema':'G11_WILD4_DAY_MODEL_BINDING_V1','date':'2026-10-13','authority':'contract','model':model}
   state={'schema':'G11_WILD4_LIFECYCLE_V1','authority':'contract','active':model}
   if kind=='wrong_day':pin['date']='2026-10-12'
   def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(sealed(v)))
   save(src/'day-model-binding.json',pin);save(src/'model-registry/lifecycle.json',state)
   if kind=='corrupt':(src/'day-model-binding.json').write_text('{}')
   if kind=='missing_pin':(src/'day-model-binding.json').unlink()
   lock={'CANDIDATE_MODEL_SHA256':'a'*64,'STANDARD_RACE_KEY':'20261013-14-02','LOCKED_AT':'2026-10-13T08:55:00+09:00','RACE_DEADLINE_AT':'2026-10-13T09:01:00+09:00','BUY_COUNT':1}
   if kind in ('lock_without_manifest','locked'):
    save(src/'20261013-14-02.json',lock)
   if kind=='locked':save(src/'batch-manifest.json',{'CANDIDATE_MODEL_SHA256':'a'*64,'CANDIDATE_INPUT_SHA256':'b'*64,'DATE':'2026-10-13','RACE_KEYS':['20261013-14-02']})
   if kind=='symlink':(src/'unsafe').symlink_to('/etc/passwd')
   with tarfile.open(root/'prior-locks.tgz','w:gz') as a:a.add(src,arcname=src.name)
   before={p.relative_to(src):p.read_bytes() for p in src.rglob('*') if p.is_file() and not p.is_symlink()}
   life=types.ModuleType('g11.wild_pack.v1.wild4_lifecycle_v1');life.read=read;life.state_path=lambda p:p/'model-registry/lifecycle.json';life.AUTHORITY='contract';life.model_info=lambda p: model if kind!='model_mismatch' else {'identity':'bad'}
   modules={name:types.ModuleType(name) for name in ['g11','g11.wild_pack','g11.wild_pack.v1']};modules['g11.wild_pack.v1.wild4_lifecycle_v1']=life;modules['g11.wild_pack.v1'].wild4_lifecycle_v1=life
   output=io.StringIO()
   with patch.dict(os.environ,{'RUNNER_TEMP':str(root),'GITHUB_WORKSPACE':str(root),'OPERATIONAL_DATE':'2026-10-13'}),patch.dict(__import__('sys').modules,modules),contextlib.redirect_stdout(output):
    try:exec(compile(CODE,'actual_workflow_restore','exec'),{})
    except SystemExit as e:
     if e.code not in (0,None):raise
   after={p.relative_to(root/'g11-wild4-future-locks'):p.read_bytes() for p in (root/'g11-wild4-future-locks').rglob('*') if p.is_file()}
   self.assertEqual(before,after)
   return output.getvalue()
 def test_model_only_keeps_pin_and_bytes(self):self.assertIn('VERIFIED_MODEL_ONLY_CHECKPOINT',self.run_case())
 def test_existing_saved_lock_keeps_old_validation(self):self.assertIn('WILD4_PRIOR_LOCKS=1',self.run_case('locked'))
 def test_wrong_day_rejected(self):
  with self.assertRaisesRegex(SystemExit,'PRIOR_MODEL_ONLY_BINDING'):self.run_case('wrong_day')
 def test_model_mismatch_rejected(self):
  with self.assertRaisesRegex(SystemExit,'PRIOR_MODEL_ONLY_BINDING'):self.run_case('model_mismatch')
 def test_lock_without_manifest_rejected(self):
  with self.assertRaisesRegex(SystemExit,'PRIOR_LOCKS_WITHOUT_MANIFEST'):self.run_case('lock_without_manifest')
 def test_corrupt_pin_rejected(self):
  with self.assertRaisesRegex(ValueError,'LIFECYCLE_SHA'):self.run_case('corrupt')
 def test_missing_pin_rejected(self):
  with self.assertRaises(FileNotFoundError):self.run_case('missing_pin')
 def test_archive_symlink_rejected(self):
  with self.assertRaisesRegex(SystemExit,'PRIOR_LOCKS_UNSAFE_ARCHIVE'):self.run_case('symlink')
if __name__=='__main__':unittest.main()
