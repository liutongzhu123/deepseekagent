#!/usr/bin/env python3
"""Initial main-only deployment of CNZ WeKnora to the isolated testkb stack.

F1 fetch/main/clean-tree error: no SSH.
F2 pinned upstream hash, manifest, or patch failure: no SSH.
F3 backup/persistence/disk/resource preflight failure: no upload.
F4 release already exists: never overwrite it.
F5 transfer/extraction failure: no build/start.
F6 Compose isolation contract fails: no build/start.
F7 image build failure: no startup.
F8 startup health fails: stop only this project with compose down, preserving volumes.
F9 active pointer is changed only after all checks pass.
Unit tests must cover each failure. First-install only: existing CNZ containers or
volumes abort; database upgrade/rollback is intentionally out of scope.
"""
from __future__ import annotations
import argparse, hashlib, os, re, stat, subprocess, sys, tarfile, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_URL = "https://github.com/Tencent/WeKnora/archive/refs/tags/v0.9.1.tar.gz"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SERVICES = {"app", "frontend", "docreader", "postgres", "redis"}
CONTAINERS = {"app":"cnz-weknora-app", "frontend":"cnz-weknora-frontend",
              "docreader":"cnz-weknora-docreader", "postgres":"cnz-weknora-postgres",
              "redis":"cnz-weknora-redis"}
REMOTE_ROOT = "/opt/cnz-weknora"

PREFLIGHT = r'''set -euo pipefail
sha="$1"; source_fingerprint="$2"; root=/opt/cnz-weknora; release="$root/releases/$sha"; incoming="$root/.incoming-$sha"
fingerprint_source() {
  python3 - "$1" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
h = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
    rel = path.relative_to(root).as_posix()
    if rel == ".env" or ".git" in path.relative_to(root).parts:
        continue
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_symlink(): kind, payload = b"L", os.readlink(path).encode()
    elif path.is_dir(): kind, payload = b"D", b""
    elif path.is_file(): kind, payload = b"F", path.read_bytes()
    else: raise SystemExit(f"unsupported source entry: {rel}")
    h.update(kind + mode.to_bytes(4, "big") + rel.encode() + b"\0")
    h.update(len(payload).to_bytes(8, "big"))
    h.update(payload)
print(h.hexdigest())
PY
}
test -f /opt/cnz-weknora/active/WeKnora-0.9.1/.env
test "$(stat -c %a /opt/cnz-weknora/active/WeKnora-0.9.1/.env)" = 600
(cd /var/backups/testkb-yunan-persist-20261124 && sha256sum -c SHA256SUMS >/dev/null)
test "$(docker ps -aq --filter name='^cnz-weknora-' | wc -l)" -eq 0
test ! -e "$root/active-release" && test ! -L "$root/active-release"
test ! -e "$incoming" && test ! -L "$incoming"
if [ -e "$release" ] || [ -L "$release" ]; then
  test -d "$release" && test ! -L "$release"
  grep -Fxq "$sha" "$release/DEPLOYED_FROM_MAIN"
  test "$(stat -c %a "$release/source/WeKnora-0.9.1/.env")" = 600
  test "$(fingerprint_source "$release/source/WeKnora-0.9.1")" = "$source_fingerprint"
  echo RELEASE_PRESENT
else
  echo RELEASE_NEW
fi
expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data | sort)"
existing_volumes="$(docker volume ls -q --filter name=cnz-weknora | sort)"
if [ -n "$existing_volumes" ]; then
  test "$existing_volumes" = "$expected_volumes"
  matching_release=0
  for previous in "$root"/releases/*; do
    [ -d "$previous" ] && [ ! -L "$previous" ] || continue
    [ -d "$previous/source/WeKnora-0.9.1" ] && [ ! -L "$previous/source/WeKnora-0.9.1" ] || continue
    [ -f "$previous/DEPLOYED_FROM_MAIN" ] && [ ! -L "$previous/DEPLOYED_FROM_MAIN" ] || continue
    previous_sha="$(basename "$previous")"
    grep -Fxq "$previous_sha" "$previous/DEPLOYED_FROM_MAIN" || continue
    [ "$(stat -c %a "$previous/source/WeKnora-0.9.1/.env")" = 600 ] || continue
    if [ "$(fingerprint_source "$previous/source/WeKnora-0.9.1")" = "$source_fingerprint" ]; then
      matching_release=1
      break
    fi
  done
  test "$matching_release" -eq 1
  while IFS= read -r volume; do
    logical="${volume#cnz-weknora_}"
    test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume")" = cnz-weknora
    test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' "$volume")" = "$logical"
  done <<< "$existing_volumes"
  echo PROJECT_VOLUMES_REUSED
fi
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(df -Pk /opt | awk 'NR==2 {print $4}')" -ge 31457280
docker compose version >/dev/null
echo PREFLIGHT_OK
'''

CONFIGURE = r'''set -euo pipefail
sha="$1"; root=/opt/cnz-weknora; incoming="$root/.incoming-$sha"; release="$root/releases/$sha"
project="$incoming/source/WeKnora-0.9.1"; env_file="$project/.env"
test -f "$project/docker-compose.yml"
test -f "$incoming/docker-compose.testkb.override.yml"
grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_MAIN"
cp -- /opt/cnz-weknora/active/WeKnora-0.9.1/.env "$env_file"
chmod 0600 "$env_file"
test "$(grep -c '^WEKNORA_VERSION=' "$env_file")" -eq 1
sed -i "s/^WEKNORA_VERSION=.*/WEKNORA_VERSION=cnz-main-$sha/" "$env_file"
if grep -q '^RUSTUP_MIRROR_ARG=' "$env_file"; then
  sed -i 's@^RUSTUP_MIRROR_ARG=.*@RUSTUP_MIRROR_ARG=https://rsproxy.cn@' "$env_file"
else
  printf '\nRUSTUP_MIRROR_ARG=https://rsproxy.cn\n' >> "$env_file"
fi
set_env() {
  key="$1"; value="$2"
  if grep -q "^$key=" "$env_file"; then
    sed -i "s@^$key=.*@$key=$value@" "$env_file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}
set_env VERSION_ARG "cnz-main-$sha"
set_env COMMIT_ID_ARG "$sha"
set_env BUILD_TIME_ARG "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set_env GOPROXY_ARG "https://goproxy.cn"
set_env CNZ_DOCKERHUB_MIRROR_ARG "docker.m.daocloud.io"
test "$(stat -c %a "$env_file")" = 600
mv -T "$incoming" "$release"
echo RELEASE_EXTRACTED
'''

DEPLOY = r'''set -Eeuo pipefail
sha="$1"; release="/opt/cnz-weknora/releases/$sha"; project="$release/source/WeKnora-0.9.1"
compose() { docker compose -p cnz-weknora --project-directory "$project" --env-file "$project/.env" -f "$project/docker-compose.yml" -f "$release/docker-compose.testkb.override.yml" "$@"; }
STARTED=0
phase=compose_contract
rollback() {
  code=$?
  if [ "$code" -ne 0 ]; then
    echo "DEPLOY_FAILED_PHASE=$phase"
    if [ "$STARTED" = 1 ]; then
      compose down --remove-orphans >/dev/null 2>&1 || true
      echo ROLLBACK_STOPPED_CNZ_VOLUMES_PRESERVED
    fi
  fi
  exit "$code"
}
trap rollback EXIT
compose config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); s=c["services"]; e={"app","frontend","docreader","postgres","redis"}; assert set(s)==e; assert all(s[k]["container_name"]==v for k,v in {"app":"cnz-weknora-app","frontend":"cnz-weknora-frontend","docreader":"cnz-weknora-docreader","postgres":"cnz-weknora-postgres","redis":"cnz-weknora-redis"}.items()); assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==80 and p.get("published")=="18082" for p in s["frontend"].get("ports",[])); assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==8080 and p.get("published")=="39301" for p in s["app"].get("ports",[])); assert set(c["volumes"])=={"data-files","docreader-tmp","postgres-data","redis-data"}; assert all("/var/run/docker.sock" not in str(m) for x in s.values() for m in x.get("volumes",[])); assert all(s[k]["build"]["args"]["CNZ_DOCKERHUB_MIRROR_ARG"]=="docker.m.daocloud.io" for k in ("app","frontend","docreader")); assert s["app"]["build"]["args"]["GOPROXY_ARG"]=="https://goproxy.cn"; assert s["app"]["environment"]["DISABLE_REGISTRATION"]=="true"; assert s["app"]["environment"]["OIDC_AUTH_ENABLE"]=="false"; assert s["redis"]["healthcheck"]["test"]==["CMD","redis-cli","ping"]'
echo COMPOSE_CONTRACT_OK
phase=image_build
compose build app frontend docreader
echo IMAGES_BUILT
STARTED=1
phase=service_start
compose up -d
phase=health_check
ready=0
for _ in $(seq 1 90); do
  app="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-app 2>/dev/null || true)"
  pg="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-postgres 2>/dev/null || true)"
  redis="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-redis 2>/dev/null || true)"
  api="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:39301/health 2>/dev/null || true)"
  ui="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:18082/ 2>/dev/null || true)"
  if [ "$app" = healthy ] && [ "$pg" = healthy ] && [ "$redis" = healthy ] && [ "$api" = 200 ] && [ "$ui" = 200 ]; then ready=1; break; fi
  sleep 2
done
test "$ready" -eq 1
for c in cnz-weknora-frontend cnz-weknora-docreader; do test "$(docker inspect -f '{{.State.Status}}' "$c")" = running; done
ln -s "$project" "/opt/cnz-weknora/active-release.new.$sha"
mv -Tf "/opt/cnz-weknora/active-release.new.$sha" /opt/cnz-weknora/active-release
echo HEALTH_OK
echo "ACTIVE_RELEASE=$sha"
STARTED=0
trap - EXIT
'''

def run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

def source_fingerprint(root: Path) -> str:
    digest=hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p:p.relative_to(root).as_posix()):
        rel=path.relative_to(root)
        if rel.as_posix()==".env" or ".git" in rel.parts: continue
        mode=stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink(): kind,payload=b"L",os.readlink(path).encode()
        elif path.is_dir(): kind,payload=b"D",b""
        elif path.is_file(): kind,payload=b"F",path.read_bytes()
        else: raise ValueError(f"unsupported source entry: {rel}")
        digest.update(kind+mode.to_bytes(4,"big")+rel.as_posix().encode()+b"\0")
        digest.update(len(payload).to_bytes(8,"big")); digest.update(payload)
    return digest.hexdigest()

def main_sha(repo: Path = ROOT) -> str:
    branch=run(["git","-C",str(repo),"branch","--show-current"])
    if branch.returncode or branch.stdout.strip()!="main":
        raise RuntimeError("F1: deployment must run from the main branch")
    fetch=run(["git","-C",str(repo),"fetch","origin","main"],120)
    head=run(["git","-C",str(repo),"rev-parse","HEAD"])
    if fetch.returncode:
        remote=run(["git","-C",str(repo),"remote","get-url","origin"])
        match=re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$",remote.stdout.strip()) if remote.returncode==0 else None
        if not match:
            raise RuntimeError("F1: fetch failed and GitHub API fallback is unavailable")
        api=run(["gh","api",f"repos/{match.group(1)}/commits/main","--jq","[.sha, .commit.tree.sha] | @tsv"],120)
        local_tree=run(["git","-C",str(repo),"rev-parse","HEAD^{tree}"])
        parts=api.stdout.strip().split("\t")
        if api.returncode or len(parts)!=2 or local_tree.returncode or parts[1]!=local_tree.stdout.strip():
            raise RuntimeError("F1: GitHub API main tree does not match local checkout")
        main_sha=parts[0]
    else:
        main=run(["git","-C",str(repo),"rev-parse","origin/main"])
        if main.returncode:
            raise RuntimeError("F1: fetched origin/main cannot be resolved")
        main_sha=main.stdout.strip()
    dirty=run(["git","-C",str(repo),"status","--porcelain","--untracked-files=normal"])
    if head.returncode or (not fetch.returncode and head.stdout.strip()!=main_sha) or dirty.returncode or dirty.stdout.strip():
        raise RuntimeError("F1: require clean main tree matching current GitHub main")
    if not SHA_RE.fullmatch(main_sha): raise RuntimeError("F1: invalid SHA")
    return main_sha

def validate_compose(config: dict) -> None:
    s=config.get("services",{})
    if set(s)!=SERVICES: raise ValueError("F6: unexpected services")
    if any(s[k].get("container_name")!=v for k,v in CONTAINERS.items()): raise ValueError("F6: unexpected container")
    def has_port(service,target,published):
        return any(p.get("host_ip")=="127.0.0.1" and int(p.get("target",0))==target and str(p.get("published"))==published for p in s[service].get("ports",[]))
    if not has_port("frontend",80,"18082") or not has_port("app",8080,"39301"): raise ValueError("F6: port mapping")
    if set(config.get("volumes",{}))!={"data-files","docreader-tmp","postgres-data","redis-data"}: raise ValueError("F6: volume mapping")
    if any("/var/run/docker.sock" in str(m) for svc in s.values() for m in svc.get("volumes",[])): raise ValueError("F6: docker socket forbidden")
    for service in ("app", "frontend", "docreader"):
        args=s[service].get("build",{}).get("args",{})
        if isinstance(args,list): args=dict(item.split("=",1) for item in args)
        if args.get("CNZ_DOCKERHUB_MIRROR_ARG") != "docker.m.daocloud.io": raise ValueError("F6: Docker Hub mirror build arg")
    app_build_args=s["app"].get("build",{}).get("args",{})
    if isinstance(app_build_args,list): app_build_args=dict(item.split("=",1) for item in app_build_args)
    if app_build_args.get("GOPROXY_ARG") != "https://goproxy.cn": raise ValueError("F6: Go module proxy build arg")
    app_env=s["app"].get("environment",{})
    if app_env.get("DISABLE_REGISTRATION") != "true" or app_env.get("OIDC_AUTH_ENABLE") != "false":
        raise ValueError("F6: registration must be disabled and OIDC must stay off")
    if s["redis"].get("healthcheck",{}).get("test") != ["CMD", "redis-cli", "ping"]:
        raise ValueError("F6: Redis healthcheck is required")

def remote(target,script,sha,timeout=300,source_fingerprint_value=None):
    args=["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",target,"bash","-s","--",sha]
    if source_fingerprint_value is not None: args.append(source_fingerprint_value)
    return subprocess.run(args,
                          input=script.encode(),capture_output=True,timeout=timeout,check=False)

def make_bundle(sha,upstream,work) -> tuple[Path, str]:
    prepared=work/"prepared"
    p=run([sys.executable,str(ROOT/"scripts/prepare_weknora_source.py"),str(upstream),str(prepared)],600)
    if p.returncode: raise RuntimeError("F2: pinned upstream verification or project patch failed")
    source=prepared/"WeKnora-0.9.1"
    if (source/".env").exists(): raise RuntimeError("F2: unexpected .env in prepared source")
    meta=work/"DEPLOYED_FROM_MAIN"; meta.write_text(sha+"\n",encoding="ascii")
    result=work/"release.tar.gz"
    def exclude_git(entry):
        return None if ".git" in Path(entry.name).parts else entry
    with tarfile.open(result,"w:gz") as tar:
        tar.add(source,arcname="source/WeKnora-0.9.1",filter=exclude_git)
        tar.add(ROOT/"weknora/docker-compose.testkb.override.yml",arcname="docker-compose.testkb.override.yml")
        tar.add(meta,arcname="DEPLOYED_FROM_MAIN")
    return result, source_fingerprint(source)

def stream_bundle(target,sha,path):
    incoming=f"{REMOTE_ROOT}/.incoming-{sha}"
    command=f"tar -xzf - -C {incoming!r} --no-same-owner"
    args=["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",target,command]
    proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): proc.stdin.write(chunk)
        proc.stdin.close(); proc.stdin=None; out,err=proc.communicate(timeout=600)
    except Exception:
        proc.kill(); proc.communicate(); raise
    return subprocess.CompletedProcess(args,proc.returncode,out,err)

def cleanup_incoming(target,sha):
    incoming=f"{REMOTE_ROOT}/.incoming-{sha}"
    script=f"set -euo pipefail; if [ -d {incoming!r} ] && [ ! -L {incoming!r} ]; then rm -rf -- {incoming!r}; fi"
    remote(target,script,sha,60)

def deploy(target,upstream=None):
    sha=main_sha()
    with tempfile.TemporaryDirectory(prefix="cnz-weknora-") as name:
        work=Path(name)
        if upstream is None:
            upstream=work/"weknora-v0.9.1.tar.gz"
            dl=run(["curl","--fail","--location","--silent","--show-error","--connect-timeout","15","--max-time","300",ARCHIVE_URL,"--output",str(upstream)],320)
            if dl.returncode: raise RuntimeError("F2: official source download failed")
        packed,source_fingerprint_value=make_bundle(sha,upstream,work)
        check=remote(target,PREFLIGHT,sha,60,source_fingerprint_value)
        if check.returncode or b"PREFLIGHT_OK" not in check.stdout: raise RuntimeError("F3/F4: safety preflight failed")
        if b"PREFLIGHT_OK" not in check.stdout: raise RuntimeError("F3: preflight completion marker missing")
        if b"RELEASE_PRESENT" not in check.stdout:
            incoming=f"{REMOTE_ROOT}/.incoming-{sha}"
            create=f"set -euo pipefail; test ! -e {incoming!r} && test ! -L {incoming!r}; install -d -m 0755 {incoming!r}"
            if remote(target,create,sha,60).returncode: raise RuntimeError("F4: incoming release path already exists")
            if stream_bundle(target,sha,packed).returncode:
                cleanup_incoming(target,sha)
                raise RuntimeError("F5: archive transfer failed")
            configured=remote(target,CONFIGURE,sha,60)
            if configured.returncode or b"RELEASE_EXTRACTED" not in configured.stdout:
                cleanup_incoming(target,sha)
                raise RuntimeError("F5: release verification/environment setup failed")
        done=remote(target,DEPLOY,sha,7200)
        if done.returncode:
            output=done.stdout+done.stderr
            match=re.search(rb"DEPLOY_FAILED_PHASE=([a-z_]+)",output)
            phase=match.group(1).decode("ascii") if match else "unknown"
            raise RuntimeError(f"F6/F7/F8: deployment failed during {phase}; volumes preserved")
        markers=(b"COMPOSE_CONTRACT_OK",b"IMAGES_BUILT",b"HEALTH_OK",f"ACTIVE_RELEASE={sha}".encode())
        if any(m not in done.stdout for m in markers): raise RuntimeError("F9: completion marker missing")
        print(f"deployed main={sha} target={target}; app/UI/PostgreSQL/Redis healthy")

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target",required=True,help="authorized testkb SSH alias")
    parser.add_argument("--upstream-archive",type=Path)
    args=parser.parse_args()
    try: deploy(args.target,args.upstream_archive)
    except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired,tarfile.TarError) as exc:
        print(f"deployment stopped safely: {exc}",file=sys.stderr); return 1
    return 0

if __name__=="__main__": raise SystemExit(main())
