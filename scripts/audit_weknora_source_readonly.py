"""Read-only source comparison against the locked official v0.9.0 manifest.

Only code/source directories are inventoried for added files; no .env, runtime
data, credentials or user documents are collected. Remote filesystem is not
modified. A manifest match proves source equality, not image build provenance.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import argparse

PIN = '4cb9cb25cea93c2d26c55497935abc2e4c7c2d93c675b4b7858f45fe7518ac48'
REMOTE = r'''
import hashlib,json,subprocess,sys
from pathlib import Path
from collections import Counter
m=json.load(sys.stdin);root=Path('/opt/weknora')
changed=[];missing=[];same=[];unsafe=[]
for rel,meta in m['files'].items():
 p=root/rel
 if p.is_symlink():unsafe.append(rel);continue
 if not p.is_file():missing.append(rel);continue
 data=p.read_bytes()
 if hashlib.sha256(data).hexdigest()==meta['sha256']:same.append(rel)
 else:changed.append(rel)
added=[]
roots=['internal','frontend/src','config','migrations','cmd','client','docreader','pkg']
for name in roots:
 for p in (root/name).rglob('*'):
  if not p.is_file() or p.is_symlink():continue
  rel=p.relative_to(root).as_posix()
  if any(x in p.parts for x in ('node_modules','__pycache__','.git','.venv')):continue
  if p.suffix not in ('.go','.ts','.vue','.js','.mjs','.py','.yaml','.yml','.sql','.json','.toml','.proto'):continue
  if rel not in m['files']:added.append(rel)
def group(paths):return dict(sorted(Counter(p.split('/')[0] for p in paths).items()))
out={'baseline_commit':m['commit'],'baseline_archive_sha256':m['archive_sha256'],
 'upstream_file_count':len(m['files']),'identical':len(same),'changed':len(changed),
 'missing':len(missing),'symlinks_not_followed':unsafe,
 'changed_by_root':group(changed),'missing_by_root':group(missing),
 'added_code_count':len(added),'added_code_by_root':group(added),
 'changed_files':changed,'missing_files':missing,'added_code_files':sorted(added),
 'working_tree_head':subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip(),
 'scope':'Official manifest paths plus selected added code only; not an image-to-source attestation'}
images={}
for ref in ['yunan/kb-app:cn-prompt-5','wechatopenai/weknora-app:v0.9.0','yunan/kb-ui:deepknow-26']:
 d=json.loads(subprocess.check_output(['docker','image','inspect',ref],text=True))[0]
 images[ref]={'id':d['Id'],'layers':d['RootFS']['Layers']}
a=images['yunan/kb-app:cn-prompt-5']['layers'];b=images['wechatopenai/weknora-app:v0.9.0']['layers']
out['backend_layer_lineage']={'official_layers':len(b),'deployed_layers':len(a),'official_prefix_identical':a[:len(b)]==b}
out['image_ids']={k:v['id'] for k,v in images.items()}
out['backend_executable_sha256']=subprocess.check_output(['docker','exec','WeKnora-app','sha256sum','/app/WeKnora'],text=True).split()[0]
out['runtime_prompt_hashes']={}
for rel in ['config/prompt_templates/system_prompt.yaml','config/prompt_templates/agent_system_prompt.yaml','config/builtin_agents.yaml']:
 data=subprocess.check_output(['docker','exec','WeKnora-app','cat','/app/'+rel])
 out['runtime_prompt_hashes'][rel]=hashlib.sha256(data).hexdigest()
out['backend_custom_history']=subprocess.check_output(['docker','history','--no-trunc','--format','{{.CreatedBy}}','yunan/kb-app:cn-prompt-5'],text=True).splitlines()[:4]
result=subprocess.check_output(['docker','exec','WeKnora-frontend','sh','-c','find /usr/share/nginx/html -type f -exec sha256sum {} \\;'],text=True)
runtime={line[66:].removeprefix('/usr/share/nginx/html/'):line[:64] for line in result.splitlines()}
overlay=Path('/tmp/kbui-build26/html')
copied=['index.html','brand.css','brand-login.css','brand-login.js','favicon.svg','favicon.ico']
copied += [p.relative_to(overlay).as_posix() for p in (overlay/'assets-v2').rglob('*') if p.is_file()]
bad=[rel for rel in copied if runtime.get(rel)!=hashlib.sha256((overlay/rel).read_bytes()).hexdigest()]
out['frontend_overlay_readback']={'build_recipe':'/tmp/kbui-build26/Dockerfile','copied_files':len(copied),'matches':len(copied)-len(bad),'mismatches':bad,'source_level_changes_reconstructed':False}
print(json.dumps(out,ensure_ascii=False,indent=2))
'''
parser = argparse.ArgumentParser()
parser.add_argument('--manifest', type=Path,
                    default=Path(__file__).resolve().parents[1] / 'ci/upstream-v0.9.0.json',
                    help='locked upstream v0.9.0 file manifest')
parser.add_argument(
    '--ssh-target',
    default=os.getenv('CNZ_WEKNORA_AUDIT_TARGET', ''),
    help='SSH target or configured alias; may be set with CNZ_WEKNORA_AUDIT_TARGET',
)
parser.add_argument('output', type=Path, help='path for the local JSON result')
args = parser.parse_args()
if not args.ssh_target.strip():
    parser.error('provide --ssh-target or CNZ_WEKNORA_AUDIT_TARGET')
raw = args.manifest.read_bytes()
if hashlib.sha256(raw).hexdigest() != PIN:
    raise SystemExit('Locked upstream manifest hash mismatch')

cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',args.ssh_target,
 'python3 -c '+shlex.quote(REMOTE)]
result=subprocess.run(cmd,input=raw,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
if result.returncode:
    sys.stderr.write(result.stderr.decode(errors='replace'))
    raise SystemExit(result.returncode)
obj=json.loads(result.stdout)
out=args.output;out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in obj.items() if not k.endswith('_files')},ensure_ascii=False,indent=2))
