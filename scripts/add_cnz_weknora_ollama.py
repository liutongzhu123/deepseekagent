#!/usr/bin/env python3
"""Append an isolated Ollama runtime to the existing CNZ testkb stack.

F1 wrong target, dirty/stale main, or unsafe backup path: no SSH mutation.
F2 runtime, backup, schema, topology, health, or disk preflight fails: no upload.
F3 staging/verification fails: clean only the incoming release.
F4 Compose contract fails: do not start Ollama.
F5 Ollama health/post-check fails: remove only its container; keep all volumes.
F6 switch active-release only after all old-container and isolation checks pass.
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"yunan-101"}
REMOTE_ROOT = "/opt/cnz-weknora"
BACKUP_RE = re.compile(r"^/var/backups/cnz-weknora/[A-Za-z0-9._-]+$")
IMAGE = "ollama/ollama@sha256:b2f0122c476bc9fe7ce679d8bb4bd555351e25e2554eefb156f61cb0a89cf429"

import importlib.util
_SPEC = importlib.util.spec_from_file_location(
    "deploy_cnz_weknora_testkb", ROOT / "scripts/deploy_cnz_weknora_testkb.py"
)
assert _SPEC and _SPEC.loader
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)

PREFLIGHT = r'''set -Eeuo pipefail
sha="$1"; fingerprint="$2"; backup="$3"; root=/opt/cnz-weknora
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$fingerprint" =~ ^[0-9a-f]{64}$ ]]
case "$backup" in /var/backups/cnz-weknora/*) ;; *) echo F2_BACKUP_PATH; exit 21 ;; esac
backup="$(realpath -e -- "$backup")"
case "$backup" in /var/backups/cnz-weknora/*) ;; *) echo F2_BACKUP_PATH; exit 21 ;; esac
test -d "$backup" && test ! -L "$backup" && test ! -e "$backup/INCOMPLETE"
test "$(stat -c %a "$backup")" = 700
test -f "$backup/SHA256SUMS" && test -f "$backup/database.dump" && test -s "$backup/database.contents.txt"
(cd "$backup" && sha256sum -c SHA256SUMS >/dev/null)
test -z "$(find "$backup" -type f ! -perm 0600 -print -quit)"
active="$(readlink -f "$root/active-release")"
case "$active" in "$root"/releases/*/source/WeKnora-0.9.1) ;; *) echo F2_ACTIVE_RELEASE; exit 22 ;; esac
old_release="$(dirname "$(dirname "$active")")"; old_sha="$(basename "$old_release")"
test "$old_sha" != "$sha"
if [ -f "$old_release/DEPLOYED_FROM_CODE" ]; then grep -Fxq "$old_sha" "$old_release/DEPLOYED_FROM_CODE"; else grep -Fxq "$old_sha" "$old_release/DEPLOYED_FROM_MAIN"; fi
grep -Fxq "active_source_sha=$old_sha" "$backup/METADATA.txt"
test "$(stat -c %a "$active/.env")" = 600
runtime_config="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/config/config.yaml"}}{{.Source}}{{end}}{{end}}' cnz-weknora-app)"
case "$runtime_config" in "$root"/releases/[0-9a-f][0-9a-f]*/source/WeKnora-0.9.1/config/config.yaml) ;; *) echo F2_RUNTIME_CONFIG; exit 23 ;; esac
runtime_sha="$(printf '%s' "$runtime_config" | sed -nE "s|^$root/releases/([0-9a-f]{40})/source/WeKnora-0[.]9[.]1/config/config[.]yaml$|\\1|p")"
test "${#runtime_sha}" -eq 40
runtime_project="$(realpath -e "$(dirname "$runtime_config")/..")"
test "$runtime_project" = "$root/releases/$runtime_sha/source/WeKnora-0.9.1"
runtime_release="$root/releases/$runtime_sha"
if [ -f "$runtime_release/DEPLOYED_FROM_CODE" ]; then grep -Fxq "$runtime_sha" "$runtime_release/DEPLOYED_FROM_CODE"; else grep -Fxq "$runtime_sha" "$runtime_release/DEPLOYED_FROM_MAIN"; fi
grep -Fxq "runtime_source_sha=$runtime_sha" "$backup/METADATA.txt"
test "$(stat -c %a "$runtime_project/.env")" = 600
cmp -s "$active/.env" "$runtime_project/.env"
test "$(docker ps --filter name='^cnz-weknora-app$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-postgres$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-redis$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
expected_containers="$(printf '%s\n' cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis | sort)"
test "$(docker ps -a --filter name='^cnz-weknora-' --format '{{.Names}}' | sort)" = "$expected_containers"
for c in $expected_containers; do
  test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")" = cnz-weknora
  docker inspect -f '{{json .NetworkSettings.Networks}}' "$c" | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
done
for c in cnz-weknora-frontend cnz-weknora-docreader; do test "$(docker inspect -f '{{.State.Status}}' "$c")" = running; done
test "$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-docreader)" = healthy
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:39301/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18082/)" = 200
expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data | sort)"
current_volumes="$(docker volume ls -q --filter name=cnz-weknora | sort)"
if docker volume inspect cnz-weknora_ollama-data >/dev/null 2>&1; then
  test "$current_volumes" = "$(printf '%s\n' $expected_volumes cnz-weknora_ollama-data | sort)"
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' cnz-weknora_ollama-data)" = cnz-weknora
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' cnz-weknora_ollama-data)" = ollama-data
else
  test "$current_volumes" = "$expected_volumes"
fi
for volume in $expected_volumes; do
  logical="${volume#cnz-weknora_}"
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume")" = cnz-weknora
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' "$volume")" = "$logical"
done
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(df -Pk /opt | awk 'NR==2 {print $4}')" -ge 10485760
schema="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
test "$schema" = 138\|f
grep -Fxq 'database_schema=138|f' "$backup/METADATA.txt"
docker image inspect ''' + IMAGE + r''' >/dev/null
test ! -e "$root/releases/$sha" && test ! -L "$root/releases/$sha"
test ! -e "$root/.incoming-$sha" && test ! -L "$root/.incoming-$sha"
printf 'OLLAMA_PREFLIGHT_OK\nOLD_SHA=%s\nRUNTIME_SHA=%s\n' "$old_sha" "$runtime_sha"
'''

CONFIGURE = r'''set -euo pipefail
sha="$1"; root=/opt/cnz-weknora; incoming="$root/.incoming-$sha"; release="$root/releases/$sha"
active="$(readlink -f "$root/active-release")"; project="$incoming/source/WeKnora-0.9.1"
phase=verify_release_inputs
trap 'code=$?; if [ "$code" -ne 0 ]; then echo "OLLAMA_CONFIGURE_FAILED_PHASE=$phase"; fi; exit "$code"' EXIT
test -f "$project/docker-compose.yml" && test -f "$incoming/docker-compose.testkb.override.yml"
grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_MAIN"
test ! -e "$release" && test ! -L "$release"
phase=copy_runtime_environment
cp -- "$active/.env" "$project/.env"; chmod 0600 "$project/.env"
test "$(stat -c %a "$project/.env")" = 600
phase=activate_staged_release
mv -T "$incoming" "$release"
echo OLLAMA_RELEASE_READY
trap - EXIT
'''

INSTALL = r'''set -Eeuo pipefail
sha="$1"; old_sha="$2"; fingerprint="$3"; root=/opt/cnz-weknora
exec 9>"$root/.deploy.lock"
flock -n 9 || { echo CNZ_DEPLOY_LOCKED; exit 40; }
new_release="$root/releases/$sha"; project="$new_release/source/WeKnora-0.9.1"
old_project="$root/releases/$old_sha/source/WeKnora-0.9.1"
compose_new() { docker compose -p cnz-weknora --project-directory "$project" --env-file "$project/.env" -f "$project/docker-compose.yml" -f "$new_release/docker-compose.testkb.override.yml" "$@"; }
fingerprint_source() {
 python3 - "$1" <<'PY'
import hashlib,os,stat,sys
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
compose_new config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); s=c["services"]; n={"app":"cnz-weknora-app","frontend":"cnz-weknora-frontend","docreader":"cnz-weknora-docreader","postgres":"cnz-weknora-postgres","redis":"cnz-weknora-redis","ollama":"cnz-weknora-ollama"}; assert set(s)==set(n) and all(s[k]["container_name"]==v for k,v in n.items()); assert set(c["volumes"])=={"data-files","docreader-tmp","postgres-data","redis-data","ollama-data"}; assert set(c["networks"])=={"WeKnora-network"}; assert all(set(v.get("networks",{}))=={"WeKnora-network"} for v in s.values()); assert all("/var/run/docker.sock" not in str(m) for x in s.values() for m in x.get("volumes",[])); assert s["ollama"]["image"]=="''' + IMAGE + r'''"; assert not s["ollama"].get("ports"); assert s["ollama"]["environment"]["OLLAMA_HOST"]=="0.0.0.0:11434"; assert s["ollama"]["healthcheck"]["test"]==["CMD","ollama","list"]; assert [(v["type"],v["source"],v["target"],bool(v.get("read_only",False))) for v in s["ollama"]["volumes"]]==[("volume","ollama-data","/root/.ollama",False)]; assert all(not v.get("external",False) and v.get("driver","local")=="local" and not v.get("driver_opts") and v.get("name") in (None,"cnz-weknora_"+k) for k,v in c["volumes"].items())'
test "$(readlink -f "$root/active-release")" = "$old_project"
test "$(fingerprint_source "$project")" = "$fingerprint"
old_ids=()
for c in cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis; do old_ids+=("$(docker inspect -f '{{.Id}}' "$c")"); done
volumes_before="$(docker volume ls -q --filter name=cnz-weknora | sort)"
for v in cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data; do printf '%s\n' "$volumes_before" | grep -Fxq "$v"; done
schema_before="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
test "$schema_before" = 138\|f
phase=start_ollama; started=0
rollback() {
 code=$?; trap - EXIT; set +e
 if [ "$code" -ne 0 ]; then
   echo "OLLAMA_INSTALL_FAILED_PHASE=$phase"
   if [ "$started" -eq 1 ]; then docker rm -f cnz-weknora-ollama >/dev/null 2>&1; echo OLLAMA_CONTAINER_REMOVED_VOLUME_PRESERVED; fi
 fi
 exit "$code"
}
trap rollback EXIT
started=1
compose_new up -d --no-deps ollama
phase=ollama_health; ready=0
for _ in $(seq 1 90); do
 state="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-ollama 2>/dev/null || true)"
 if [ "$state" = healthy ]; then ready=1; break; fi
 sleep 2
done
test "$ready" -eq 1
phase=post_checks
test "$(docker inspect -f '{{.Config.Image}}' cnz-weknora-ollama)" = ''' + IMAGE + r'''
test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' cnz-weknora-ollama)" = cnz-weknora
docker inspect -f '{{json .NetworkSettings.Networks}}' cnz-weknora-ollama | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
docker inspect -f '{{json .Mounts}}' cnz-weknora-ollama | python3 -c 'import json,sys; a=[[m.get("Type"),m.get("Destination"),m.get("Name")] for m in json.load(sys.stdin)]; assert a==[["volume","/root/.ollama","cnz-weknora_ollama-data"]],a'
expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data cnz-weknora_ollama-data | sort)"
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$expected_volumes"
test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' cnz-weknora_ollama-data)" = cnz-weknora
test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' cnz-weknora_ollama-data)" = ollama-data
i=0
for c in cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis; do
  test "$(docker inspect -f '{{.Id}}' "$c")" = "${old_ids[$i]}"; i=$((i+1))
done
for v in cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data; do docker volume inspect "$v" >/dev/null; done
test "$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')" = "$schema_before"
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:39301/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18082/)" = 200
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
for c in cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis cnz-weknora-ollama; do
  docker inspect -f '{{json .NetworkSettings.Networks}}' "$c" | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
done
test "$(readlink -f "$root/active-release")" = "$old_project"
ln -s "$project" "$root/active-release.new.$sha"
mv -Tf "$root/active-release.new.$sha" "$root/active-release"
test "$(readlink -f "$root/active-release")" = "$project"
echo OLLAMA_RUNTIME_OK
echo "ACTIVE_RELEASE=$sha"
echo "SCHEMA=$schema_before"
trap - EXIT
'''


def remote(target: str, script: str, *args: str, timeout: int = 300):
    command = shlex.join(["bash", "-s", "--", *(str(a) for a in args)])
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
        input=script.encode(), capture_output=True, timeout=timeout, check=False,
    )


def install(target: str, backup: str, upstream: Path | None = None) -> None:
    if target not in TARGETS:
        raise ValueError("F1: only yunan-101 is authorized")
    if not BACKUP_RE.fullmatch(backup):
        raise ValueError("F2: backup must be a direct child under /var/backups/cnz-weknora")
    sha = base.main_sha()
    with tempfile.TemporaryDirectory(prefix="cnz-ollama-") as name:
        work = Path(name)
        if upstream is None:
            upstream = work / "weknora-v0.9.1.tar.gz"
            result = base.run(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "15", "--max-time", "300", base.ARCHIVE_URL, "--output", str(upstream)], 320)
            if result.returncode:
                raise RuntimeError("F2: official source download failed")
        packed, fingerprint = base.make_bundle(sha, upstream, work)
        result = remote(target, PREFLIGHT, sha, fingerprint, backup, timeout=90)
        if result.returncode or b"OLLAMA_PREFLIGHT_OK" not in result.stdout:
            raise RuntimeError("F2: remote preflight rejected; no source uploaded")
        old = re.search(rb"^OLD_SHA=([0-9a-f]{40})$", result.stdout, re.M)
        runtime = re.search(rb"^RUNTIME_SHA=([0-9a-f]{40})$", result.stdout, re.M)
        if not old or not runtime or old.group(1) != runtime.group(1):
            raise RuntimeError("F2: active/runtime release identity mismatch")
        old_sha = old.group(1).decode()
        incoming = f"{REMOTE_ROOT}/.incoming-{sha}"
        create = f"set -euo pipefail; test ! -e {incoming!r} && test ! -L {incoming!r}; install -d -m 0755 {incoming!r}"
        if remote(target, create, sha, timeout=30).returncode:
            raise RuntimeError("F3: incoming release already exists")
        if base.stream_bundle(target, sha, packed).returncode:
            base.cleanup_incoming(target, sha)
            raise RuntimeError("F3: source transfer failed")
        configured = remote(target, CONFIGURE, sha, timeout=60)
        if configured.returncode or b"OLLAMA_RELEASE_READY" not in configured.stdout:
            base.cleanup_incoming(target, sha)
            match = re.search(rb"OLLAMA_CONFIGURE_FAILED_PHASE=([a-z_]+)", configured.stdout + configured.stderr)
            phase = match.group(1).decode() if match else "unknown"
            raise RuntimeError(f"F3: release setup failed during {phase}; runtime remains unchanged")
        deployed = remote(target, INSTALL, sha, old_sha, fingerprint, timeout=600)
        if deployed.returncode:
            match = re.search(rb"OLLAMA_INSTALL_FAILED_PHASE=([a-z_]+)", deployed.stdout + deployed.stderr)
            phase = match.group(1).decode() if match else "unknown"
            raise RuntimeError(f"F4/F5: runtime failed during {phase}; existing data volumes preserved")
        if b"OLLAMA_RUNTIME_OK" not in deployed.stdout or f"ACTIVE_RELEASE={sha}".encode() not in deployed.stdout:
            raise RuntimeError("F6: completion marker missing")
        print(f"Ollama isolated runtime installed on {target}; previous services and volumes verified")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="authorized testkb SSH alias (yunan-101 only)")
    parser.add_argument("--backup", required=True, help="verified CNZ backup directory made before install")
    parser.add_argument("--upstream-archive", type=Path)
    args = parser.parse_args()
    try:
        install(args.target, args.backup, args.upstream_archive)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"CNZ Ollama add-on stopped safely: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
