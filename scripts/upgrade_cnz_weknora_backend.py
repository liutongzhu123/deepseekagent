#!/usr/bin/env python3
"""Build pinned WeKnora source and replace only the CNZ app on testkb.

Failure paths:
F1 wrong target or dirty local checkout: refuse before SSH/build.
F2 archive, manifest, patch, or migration baseline mismatch: no remote mutation.
F3 missing/invalid backup, runtime drift, unhealthy service, or non-138 DB: no switch.
F4 transfer/staging failure: remove only the unactivated incoming directory.
F5 app build or health failure: restore the previous app image and runtime Compose project.
F6 post-start schema, sibling container, volume, API/UI, or isolation mismatch: rollback app.
F7 missing completion markers: report failure instead of claiming deployment.
"""
from __future__ import annotations
import argparse, importlib.util, json, re, shlex, subprocess, sys, tarfile, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REMOTE_ROOT="/opt/cnz-weknora"
SCHEMA="138|f"
TARGETS={"yunan-101"}
SHA_RE=re.compile(r"^[0-9a-f]{40}$")
SPEC=importlib.util.spec_from_file_location("deploy_cnz",ROOT/"scripts/deploy_cnz_weknora_testkb.py")
assert SPEC and SPEC.loader
base=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(base)

PREFLIGHT=r'''set -Eeuo pipefail
sha="$1"; fingerprint="$2"; backup="$3"; root=/opt/cnz-weknora
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$fingerprint" =~ ^[0-9a-f]{64}$ ]]
case "$backup" in /var/backups/cnz-weknora/*) ;; *) exit 31 ;; esac
backup="$(realpath -e -- "$backup")"
case "$backup" in /var/backups/cnz-weknora/*) ;; *) exit 31 ;; esac
test -d "$backup" && test ! -L "$backup" && test ! -e "$backup/INCOMPLETE"
test -f "$backup/SHA256SUMS" && test -f "$backup/database.dump" && test -s "$backup/database.contents.txt"
(cd "$backup" && sha256sum -c SHA256SUMS >/dev/null)
test "$(stat -c %a "$backup")" = 700
test -z "$(find "$backup" -type f ! -perm 0600 -print -quit)"
active="$(readlink -f "$root/active-release")"
case "$active" in "$root"/releases/*/source/WeKnora-0.9.1) ;; *) exit 32 ;; esac
active_release="$(dirname "$(dirname "$active")")"; active_sha="$(basename "$active_release")"
marker_ok() {
  if [ -f "$2/DEPLOYED_FROM_CODE" ]; then grep -Fxq "$1" "$2/DEPLOYED_FROM_CODE"
  elif [ -f "$2/DEPLOYED_FROM_MAIN" ]; then grep -Fxq "$1" "$2/DEPLOYED_FROM_MAIN"
  else return 1; fi
}
test -f "$active/.env" && test ! -L "$active/.env"
marker_ok "$active_sha" "$active_release"
grep -Fxq "active_source_sha=$active_sha" "$backup/METADATA.txt"
test "$(stat -c %a "$active/.env")" = 600
runtime_project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' cnz-weknora-app)"
if [[ "$runtime_project" =~ ^$root/releases/[0-9a-f]{40}/source/WeKnora-0\.9\.1$ ]]; then :; else exit 33; fi
runtime_sha="$(printf '%s' "$runtime_project" | sed -nE 's|.*/releases/([0-9a-f]{40})/source/.*|\1|p')"
runtime_release="$root/releases/$runtime_sha"
test -f "$runtime_project/.env" && test ! -L "$runtime_project/.env"
marker_ok "$runtime_sha" "$runtime_release"
grep -Fxq "runtime_source_sha=$runtime_sha" "$backup/METADATA.txt"
test "$(stat -c %a "$runtime_project/.env")" = 600
cmp -s "$active/.env" "$runtime_project/.env"
runtime_config="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/config/config.yaml"}}{{.Source}}{{end}}{{end}}' cnz-weknora-app)"
test "$runtime_config" = "$runtime_project/config/config.yaml" && test -f "$runtime_config" && test ! -L "$runtime_config"
expected_containers="$(printf '%s\n' cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis | sort)"
test "$(docker ps -a --filter name='^cnz-weknora-' --format '{{.Names}}' | sort)" = "$expected_containers"
for c in cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis; do
  test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")" = cnz-weknora
  docker inspect -f '{{json .NetworkSettings.Networks}}' "$c" | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
done
docker inspect -f '{{json .Mounts}}' cnz-weknora-app | python3 -c 'import json,sys; got=sorted([[m.get("Type"),m.get("Destination"),m.get("Name") if m.get("Type")=="volume" else m.get("Source"),m.get("RW")] for m in json.load(sys.stdin)]); expected=sorted([["volume","/data/files","cnz-weknora_data-files",True],["volume","/tmp/docreader","cnz-weknora_docreader-tmp",False],["bind","/app/config/config.yaml",sys.argv[1],True]]); assert got==expected,got' "$runtime_config"
for c in cnz-weknora-app cnz-weknora-postgres cnz-weknora-redis; do test "$(docker inspect -f '{{.State.Health.Status}}' "$c")" = healthy; done
for c in cnz-weknora-frontend cnz-weknora-docreader; do test "$(docker inspect -f '{{.State.Status}}' "$c")" = running; done
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:39301/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18082/)" = 200
expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data | sort)"
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$expected_volumes"
for volume in $expected_volumes; do
  logical="$(printf '%s' "$volume" | sed 's/^cnz-weknora_//')"
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume")" = cnz-weknora
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' "$volume")" = "$logical"
done
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(df -Pk /opt | awk 'NR==2 {print $4}')" -ge 10485760
schema="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
test "$schema" = 138\|f
grep -Fxq 'database_schema=138|f' "$backup/METADATA.txt"
old_image="$(docker inspect -f '{{.Image}}' cnz-weknora-app)"
test "$(docker inspect -f '{{.Config.Image}}' cnz-weknora-app)" = cnz-weknora-app:dev
printf 'BACKEND_PREFLIGHT_OK\nACTIVE_SHA=%s\nRUNTIME_SHA=%s\nOLD_IMAGE=%s\n' "$active_sha" "$runtime_sha" "$old_image"
'''

CONFIGURE=r'''set -euo pipefail
sha="$1"; root=/opt/cnz-weknora; incoming="$root/.incoming-$sha"; release="$root/releases/$sha"
active="$(readlink -f "$root/active-release")"; project="$incoming/source/WeKnora-0.9.1"
test -f "$project/docker-compose.yml" && test -f "$incoming/docker-compose.testkb.override.yml"
grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_CODE"
test ! -e "$release" && test ! -L "$release"
cp -- "$active/.env" "$project/.env"; chmod 0600 "$project/.env"
test "$(stat -c %a "$project/.env")" = 600
mv -T "$incoming" "$release"
echo BACKEND_RELEASE_READY
'''

UPGRADE=r'''set -Eeuo pipefail
sha="$1"; runtime_sha="$2"; active_sha="$3"; old_image="$4"; fingerprint="$5"; backup="$6"; root=/opt/cnz-weknora
exec 9>"$root/.deploy.lock"
flock -n 9 || { echo BACKEND_DEPLOY_LOCKED; exit 40; }
new_release="$root/releases/$sha"; new_project="$new_release/source/WeKnora-0.9.1"
old_release="$root/releases/$runtime_sha"; old_project="$old_release/source/WeKnora-0.9.1"
old_active="$root/releases/$active_sha/source/WeKnora-0.9.1"
rollback_tag="cnz-weknora-app:rollback-$sha"
fingerprint_source() {
 python3 - "$1" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
root=Path(sys.argv[1]); h=hashlib.sha256()
for p in sorted(root.rglob("*"),key=lambda x:x.relative_to(root).as_posix()):
 rel=p.relative_to(root).as_posix()
 if rel==".env" or ".git" in p.relative_to(root).parts: continue
 mode=stat.S_IMODE(p.lstat().st_mode)
 if p.is_symlink(): kind,data=b"L",os.readlink(p).encode()
 elif p.is_dir(): kind,data=b"D",b""
 elif p.is_file(): kind,data=b"F",p.read_bytes()
 else: raise SystemExit("unsupported source entry")
 h.update(kind+mode.to_bytes(4,"big")+rel.encode()+b"\0")
 h.update(len(data).to_bytes(8,"big")); h.update(data)
print(h.hexdigest())
PY
}
compose_new() { docker compose -p cnz-weknora --project-directory "$new_project" --env-file "$new_project/.env" -f "$new_project/docker-compose.yml" -f "$new_release/docker-compose.testkb.override.yml" "$@"; }
compose_old() { docker compose -p cnz-weknora --project-directory "$old_project" --env-file "$old_project/.env" -f "$old_project/docker-compose.yml" -f "$old_release/docker-compose.testkb.override.yml" "$@"; }
check_compose() {
 project_config="$1"; shift
 "$@" config --format json | python3 -c '
import json, os, sys
c=json.load(sys.stdin); s=c["services"]
names={"app":"cnz-weknora-app","frontend":"cnz-weknora-frontend","docreader":"cnz-weknora-docreader","postgres":"cnz-weknora-postgres","redis":"cnz-weknora-redis"}
assert set(s)==set(names) and all(s[k]["container_name"]==v for k,v in names.items())
assert set(c["volumes"])=={"data-files","docreader-tmp","postgres-data","redis-data"}
for logical, definition in c["volumes"].items():
    assert not definition.get("external",False), (logical,definition)
    assert definition.get("driver","local")=="local", (logical,definition)
    assert not definition.get("driver_opts"), (logical,definition)
    assert definition.get("name") in (None,"cnz-weknora_"+logical), (logical,definition)
assert set(c["networks"])=={"WeKnora-network"}
network=c["networks"]["WeKnora-network"]
assert network.get("driver","bridge")=="bridge" and not network.get("external",False)
assert all(set(v.get("networks",{}))=={"WeKnora-network"} for v in s.values())
assert all("/var/run/docker.sock" not in str(m) for x in s.values() for m in x.get("volumes",[]))
app_mounts={(m["type"],m["source"],m["target"],bool(m.get("read_only",False))) for m in s["app"]["volumes"]}
expected={("volume","data-files","/data/files",False),("volume","docreader-tmp","/tmp/docreader",True),("bind",os.path.abspath(sys.argv[1]),"/app/config/config.yaml",False)}
assert app_mounts==expected,(app_mounts,expected)
assert s["app"]["environment"]["DISABLE_REGISTRATION"]=="true"
assert s["app"]["environment"]["OIDC_AUTH_ENABLE"]=="false"
assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==80 and p.get("published")=="18082" for p in s["frontend"].get("ports",[]))
assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==8080 and p.get("published")=="39301" for p in s["app"].get("ports",[]))
 ' "$project_config"
}
wait_app() {
 expected="$1"; ready=0
 for _ in $(seq 1 60); do
  image="$(docker inspect -f '{{.Image}}' cnz-weknora-app 2>/dev/null || true)"
  health="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-app 2>/dev/null || true)"
  api="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:39301/health 2>/dev/null || true)"
  if [ "$image" = "$expected" ] && [ "$health" = healthy ] && [ "$api" = 200 ]; then ready=1; break; fi
  sleep 2
 done
 test "$ready" -eq 1
}
frontend_id="$(docker inspect -f '{{.Id}}' cnz-weknora-frontend)"
docreader_id="$(docker inspect -f '{{.Id}}' cnz-weknora-docreader)"
postgres_id="$(docker inspect -f '{{.Id}}' cnz-weknora-postgres)"
redis_id="$(docker inspect -f '{{.Id}}' cnz-weknora-redis)"
volumes_before="$(docker volume ls -q --filter name=cnz-weknora | sort)"
test "$(readlink -f "$root/active-release")" = "$old_active"
schema_before="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
test "$schema_before" = 138\|f && grep -Fxq "database_schema=$schema_before" "$backup/METADATA.txt"
test "$(fingerprint_source "$new_project")" = "$fingerprint"
check_compose "$new_project/config/config.yaml" compose_new
check_compose "$old_project/config/config.yaml" compose_old
test "$(docker inspect -f '{{.Image}}' cnz-weknora-app)" = "$old_image"
if docker image inspect "$rollback_tag" >/dev/null 2>&1; then
 test "$(docker image inspect -f '{{.Id}}' "$rollback_tag")" = "$old_image"
else docker image tag "$old_image" "$rollback_tag"; fi
phase=app_build; switch_started=0
rollback() {
 code=$?; trap - EXIT; set +e
 if [ "$code" -ne 0 ]; then echo "BACKEND_UPGRADE_FAILED_PHASE=$phase"; fi
 if [ "$code" -ne 0 ] && [ "$switch_started" -eq 1 ]; then
  docker image tag "$old_image" cnz-weknora-app:dev || { echo BACKEND_ROLLBACK_IMAGE_TAG_FAILED; exit 90; }
  compose_old up -d --no-deps app || { echo BACKEND_ROLLBACK_RECREATE_FAILED; exit 90; }
  wait_app "$old_image" || { echo BACKEND_ROLLBACK_HEALTH_FAILED; exit 90; }
  current_active="$(readlink -f "$root/active-release" 2>/dev/null || true)"
  if [ "$current_active" = "$new_project" ]; then
   ln -s "$old_active" "$root/active-release.rollback.$sha" || { echo BACKEND_ROLLBACK_POINTER_LINK_FAILED; exit 90; }
   mv -Tf "$root/active-release.rollback.$sha" "$root/active-release" || { echo BACKEND_ROLLBACK_POINTER_SWITCH_FAILED; exit 90; }
  elif [ "$current_active" != "$old_active" ]; then echo BACKEND_ROLLBACK_POINTER_DRIFT; exit 90; fi
  test "$(readlink -f "$root/active-release")" = "$old_active" || { echo BACKEND_ROLLBACK_POINTER_VERIFY_FAILED; exit 90; }
  echo BACKEND_ROLLBACK_OK_NON_APP_CONTAINERS_UNTOUCHED
 fi
 exit "$code"
}
trap rollback EXIT
check_compose "$new_project/config/config.yaml" compose_new
compose_new build app
new_image="$(docker image inspect -f '{{.Id}}' cnz-weknora-app:dev)"
test -n "$new_image" && test "$new_image" != "$old_image"
phase=app_replace; switch_started=1
compose_new up -d --no-deps app
phase=app_health; wait_app "$new_image"
schema_after="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
test "$schema_after" = "$schema_before"
test "$(docker inspect -f '{{.Id}}' cnz-weknora-frontend)" = "$frontend_id"
test "$(docker inspect -f '{{.Id}}' cnz-weknora-docreader)" = "$docreader_id"
test "$(docker inspect -f '{{.Id}}' cnz-weknora-postgres)" = "$postgres_id"
test "$(docker inspect -f '{{.Id}}' cnz-weknora-redis)" = "$redis_id"
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$volumes_before"
test "$(docker inspect -f '{{.Config.Env}}' cnz-weknora-app | grep -o 'OIDC_AUTH_ENABLE=false')" = OIDC_AUTH_ENABLE=false
docker inspect -f '{{json .Mounts}}' cnz-weknora-app | python3 -c 'import json,sys; got=sorted([[m.get("Type"),m.get("Destination"),m.get("Name") if m.get("Type")=="volume" else m.get("Source"),m.get("RW")] for m in json.load(sys.stdin)]); expected=sorted([["volume","/data/files","cnz-weknora_data-files",True],["volume","/tmp/docreader","cnz-weknora_docreader-tmp",False],["bind","/app/config/config.yaml",sys.argv[1],True]]); assert got==expected,got' "$new_project/config/config.yaml"
for c in cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis; do
  docker inspect -f '{{json .NetworkSettings.Networks}}' "$c" | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
done
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18082/)" = 200
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(readlink -f "$root/active-release")" = "$old_active" || { echo BACKEND_ACTIVE_POINTER_CHANGED; exit 1; }
test ! -e "$root/active-release.new.$sha" && test ! -L "$root/active-release.new.$sha"
ln -s "$new_project" "$root/active-release.new.$sha"
mv -Tf "$root/active-release.new.$sha" "$root/active-release"
test "$(readlink -f "$root/active-release")" = "$new_project"
echo BACKEND_UPGRADE_OK_APP_ONLY
echo "ACTIVE_RELEASE=$sha"
echo "APP_IMAGE=$new_image"
echo "DATABASE_SCHEMA=$schema_after"
echo "OIDC_AUTH_ENABLE=false"
trap - EXIT
'''

def remote(target, script, *args, timeout=300):
 remote_command=shlex.join(["bash","-s","--",*(str(arg) for arg in args)])
 cmd=["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",target,remote_command]
 return subprocess.run(cmd,input=script.encode(),capture_output=True,timeout=timeout,check=False)

def make_bundle(commit, archive, work):
 prepared=work/"prepared"
 res=base.run([sys.executable,str(ROOT/"scripts/prepare_weknora_source.py"),str(archive),str(prepared)],600)
 if res.returncode: raise RuntimeError("F2: pinned archive, manifest, or patch verification failed")
 source=prepared/"WeKnora-0.9.1"
 versions=[int(m.group(1)) for p in (source/"migrations/versioned").glob("*.up.sql") if (m:=re.match(r"^(\d+)_",p.name))]
 if not versions or max(versions)!=138: raise RuntimeError("F2: source SQL migrations exceed schema 138")
 marker=work/"DEPLOYED_FROM_CODE"; marker.write_text(commit+"\n",encoding="ascii")
 upstream=json.loads((ROOT/"ci/upstream-v0.9.1.json").read_text())["commit"]
 uf=work/"UPSTREAM_COMMIT"; uf.write_text(upstream+"\n",encoding="ascii")
 packed=work/"release.tar.gz"
 with tarfile.open(packed,"w:gz") as tar:
  tar.add(source,arcname="source/WeKnora-0.9.1",filter=lambda item: None if ".git" in Path(item.name).parts else item)
  tar.add(ROOT/"weknora/docker-compose.testkb.override.yml",arcname="docker-compose.testkb.override.yml")
  tar.add(marker,arcname="DEPLOYED_FROM_CODE"); tar.add(uf,arcname="UPSTREAM_COMMIT")
 return packed,base.source_fingerprint(source)

def upgrade(target, archive, backup):
 if target not in TARGETS: raise ValueError("F1: only yunan-101 is authorized")
 if not archive.is_file(): raise ValueError("F2: pinned upstream archive is missing")
 branch=base.run(["git","branch","--show-current"],10)
 if branch.returncode or branch.stdout.strip()!="main": raise RuntimeError("F1: deployment must run from main")
 fetched=base.run(["git","fetch","--no-tags","origin","main"],120)
 if fetched.returncode: raise RuntimeError("F1: cannot verify origin/main; deployment stopped")
 resolved=base.run(["git","rev-parse","HEAD"],10)
 if resolved.returncode: raise RuntimeError("F1: local commit cannot be resolved")
 commit=resolved.stdout.strip()
 remote_main=base.run(["git","rev-parse","refs/remotes/origin/main"],10)
 if remote_main.returncode or remote_main.stdout.strip()!=commit:
  raise RuntimeError("F1: local HEAD is not the verified origin/main commit")
 if not SHA_RE.fullmatch(commit): raise RuntimeError("F1: invalid local commit")
 state=base.run(["git","status","--porcelain"],10)
 if state.returncode or state.stdout.strip(): raise RuntimeError("F1: commit intended changes before deployment")
 backup_match=re.fullmatch(r"/var/backups/cnz-weknora/([A-Za-z0-9_.-]+)",backup)
 if not backup_match or backup_match.group(1) in {".",".."}:
  raise ValueError("F3: backup must be a direct child of CNZ backup root")
 with tempfile.TemporaryDirectory(prefix="cnz-weknora-backend-") as tmp:
  work=Path(tmp); packed,fingerprint=make_bundle(commit,archive,work)
  checked=remote(target,PREFLIGHT,commit,fingerprint,backup,timeout=90)
  if checked.returncode or b"BACKEND_PREFLIGHT_OK" not in checked.stdout: raise RuntimeError("F3: backend preflight rejected target")
  r=re.search(rb"RUNTIME_SHA=([0-9a-f]{40})",checked.stdout)
  a=re.search(rb"ACTIVE_SHA=([0-9a-f]{40})",checked.stdout)
  i=re.search(rb"OLD_IMAGE=(sha256:[0-9a-f]{64})",checked.stdout)
  if not r or not a or not i: raise RuntimeError("F3: active/runtime release or image missing")
  runtime_sha,active_sha,old_image=r.group(1).decode(),a.group(1).decode(),i.group(1).decode()
  incoming=f"{REMOTE_ROOT}/.incoming-{commit}"
  create=f"set -euo pipefail; test ! -e {incoming!r} && test ! -L {incoming!r}; install -d -m 0755 {incoming!r}"
  if remote(target,create,commit,timeout=60).returncode: raise RuntimeError("F4: incoming release exists")
  if base.stream_bundle(target,commit,packed).returncode:
   base.cleanup_incoming(target,commit); raise RuntimeError("F4: source transfer failed")
  ready=remote(target,CONFIGURE,commit,timeout=60)
  if ready.returncode or b"BACKEND_RELEASE_READY" not in ready.stdout:
   base.cleanup_incoming(target,commit); raise RuntimeError("F4: release staging failed")
  result=remote(target,UPGRADE,commit,runtime_sha,active_sha,old_image,fingerprint,backup,timeout=3600)
  if result.returncode:
   match=re.search(rb"BACKEND_UPGRADE_FAILED_PHASE=([a-z_]+)",result.stdout+result.stderr)
   phase=match.group(1).decode() if match else "unknown"
   if b"BACKEND_ROLLBACK_" in result.stdout+result.stderr: phase += "; rollback attention required"
   raise RuntimeError(f"F5: app-only upgrade failed at {phase}")
  expected=(b"BACKEND_UPGRADE_OK_APP_ONLY",f"ACTIVE_RELEASE={commit}".encode(),b"DATABASE_SCHEMA=138|f",b"OIDC_AUTH_ENABLE=false")
  if any(x not in result.stdout for x in expected): raise RuntimeError("F6: completion marker missing")
  print(f"CNZ WeKnora app upgraded at {commit}; schema 138 unchanged; OIDC stays disabled")

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--target",required=True)
 parser.add_argument("--upstream-archive",type=Path,required=True)
 parser.add_argument("--backup-path",required=True)
 args=parser.parse_args()
 try: upgrade(args.target,args.upstream_archive,args.backup_path)
 except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired,tarfile.TarError) as exc:
  print(f"CNZ backend upgrade stopped safely: {exc}",file=sys.stderr); return 1
 return 0

if __name__=="__main__": raise SystemExit(main())

