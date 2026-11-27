#!/usr/bin/env python3
"""Create a verified, CNZ-only WeKnora recovery snapshot on testkb.

Failure paths:
F1 unsupported SSH target: refuse before connecting.
F2 release, topology, health, backup capacity, or DB migration preflight: no stop.
F3 backup paths are atomically allocated; symlinked roots are refused.
F4 dump/archive/checksum error: mark snapshot incomplete and restart stopped CNZ services.
F5 service restart or health error: report separately; never touch yunan-persist.
F6 a temporary bootstrap app is accepted only when it is the known loopback-only sidecar; it is quiesced with the main app.
F7 the active .env is not copied; database and volume archives may contain persisted secrets.
Snapshot directory is mode 0700 and every snapshot artifact is mode 0600.

The brief outage is limited to the isolated CNZ app, docreader, and Redis containers.
PostgreSQL remains online for a consistent pg_dump; the app is stopped first to quiesce writes.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "deploy_cnz_weknora_testkb", ROOT / "scripts/deploy_cnz_weknora_testkb.py"
)
assert _SPEC and _SPEC.loader
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)

TARGETS = {"yunan-101"}
REMOTE_BACKUP = r'''set -Eeuo pipefail
fingerprint_source() {
  python3 - "$1" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1]); digest = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
    rel = path.relative_to(root).as_posix()
    if rel == ".env" or ".git" in path.relative_to(root).parts: continue
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_symlink(): kind, payload = b"L", os.readlink(path).encode()
    elif path.is_dir(): kind, payload = b"D", b""
    elif path.is_file(): kind, payload = b"F", path.read_bytes()
    else: raise SystemExit(f"unsupported source entry: {rel}")
    digest.update(kind + mode.to_bytes(4, "big") + rel.encode() + b"\0")
    digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
print(digest.hexdigest())
PY
}
verify_release_marker() {
  expected="$1"; directory="$2"
  if [ -f "$directory/DEPLOYED_FROM_CODE" ]; then
    grep -Fxq "$expected" "$directory/DEPLOYED_FROM_CODE"
  elif [ -f "$directory/DEPLOYED_FROM_MAIN" ]; then
    grep -Fxq "$expected" "$directory/DEPLOYED_FROM_MAIN"
  else
    return 1
  fi
}
stage=preflight
trap 'echo "CNZ_BACKUP_FAILED_STAGE=$stage"' ERR
root=/opt/cnz-weknora
exec 9>"$root/.deploy.lock"
flock -n 9 || { echo CNZ_DEPLOY_LOCKED; exit 40; }
backup_root=/var/backups/cnz-weknora
active="$(readlink -f "$root/active-release")"
case "$active" in "$root"/releases/*/source/WeKnora-0.9.1) ;; *) echo F2_INVALID_ACTIVE_RELEASE; exit 22 ;; esac
release="$(dirname "$(dirname "$active")")"
source_sha="$(basename "$release")"
verify_release_marker "$source_sha" "$release"
test "$(stat -c %a "$active/.env")" = 600
runtime_config="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/config/config.yaml"}}{{.Source}}{{end}}{{end}}' cnz-weknora-app)"
[[ "$runtime_config" =~ ^$root/releases/([[:xdigit:]]{40})/source/WeKnora-0\.9\.1/config/config\.yaml$ ]]
runtime_source_sha="${BASH_REMATCH[1]}"
runtime_release="$root/releases/$runtime_source_sha"
runtime_source="$runtime_release/source/WeKnora-0.9.1"
verify_release_marker "$runtime_source_sha" "$runtime_release"
test -f "$runtime_config" && test ! -L "$runtime_config"
test "$(stat -c %a "$runtime_source/.env")" = 600
cmp -s "$active/.env" "$runtime_source/.env"
test "$(docker ps --filter name='^cnz-weknora-app$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-postgres$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-redis$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
ollama_present=0
if docker inspect cnz-weknora-ollama >/dev/null 2>&1; then ollama_present=1; fi
ollama_volume_present=0
if docker volume inspect cnz-weknora_ollama-data >/dev/null 2>&1; then ollama_volume_present=1; fi
if [ "$ollama_present" -eq 1 ]; then
  test "$(docker ps --filter name='^cnz-weknora-ollama$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
  expected_containers="$(printf '%s\n' cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis cnz-weknora-ollama | sort)"
else
  expected_containers="$(printf '%s\n' cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis | sort)"
fi
bootstrap_present=0
bootstrap_source_sha=none
bootstrap_config=none
bootstrap_config_sha256=none
if docker inspect cnz-weknora-bootstrap >/dev/null 2>&1; then
  bootstrap_present=1
  test "$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-bootstrap)" = healthy
  test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' cnz-weknora-bootstrap)" = cnz-weknora
  test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' cnz-weknora-bootstrap)" = bootstrap-app
  test "$(docker inspect -f '{{.Image}}' cnz-weknora-bootstrap)" = "$(docker inspect -f '{{.Image}}' cnz-weknora-app)"
  bootstrap_project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' cnz-weknora-bootstrap)"
  [[ "$bootstrap_project" =~ ^$root/releases/([[:xdigit:]]{40})/source/WeKnora-0\.9\.1$ ]]
  bootstrap_source_sha="${BASH_REMATCH[1]}"
  bootstrap_release="$root/releases/$bootstrap_source_sha"
  test "$bootstrap_source_sha" = "$source_sha"
  verify_release_marker "$bootstrap_source_sha" "$bootstrap_release"
  test -f "$bootstrap_project/.env" && test ! -L "$bootstrap_project/.env"
  test "$(stat -c %a "$bootstrap_project/.env")" = 600
  cmp -s "$active/.env" "$bootstrap_project/.env"
  bootstrap_config="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/config/config.yaml"}}{{.Source}}{{end}}{{end}}' cnz-weknora-bootstrap)"
  test "$bootstrap_config" = "$bootstrap_project/config/config.yaml" && test -f "$bootstrap_config" && test ! -L "$bootstrap_config"
  bootstrap_config_sha256="$(sha256sum "$bootstrap_config" | cut -d' ' -f1)"
  docker inspect -f '{{json .NetworkSettings.Ports}}' cnz-weknora-bootstrap | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p=={"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"39302"}]},p'
  docker inspect -f '{{json .NetworkSettings.Networks}}' cnz-weknora-bootstrap | python3 -c 'import json,sys; assert set(json.load(sys.stdin))=={"cnz-weknora_WeKnora-network"}'
  for expected in 'DISABLE_REGISTRATION=false' 'OIDC_AUTH_ENABLE=false'; do
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' cnz-weknora-bootstrap | grep -Fxq "$expected"
  done
  curl -fsS --max-time 4 http://127.0.0.1:39302/api/v1/auth/config | python3 -c 'import json,sys; assert json.load(sys.stdin).get("registration_mode")=="self_serve"'
  expected_containers="$(printf '%s\n' "$expected_containers" cnz-weknora-bootstrap | sort)"
fi
if [ "$ollama_volume_present" -eq 1 ]; then
  expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data cnz-weknora_ollama-data | sort)"
else
  expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data | sort)"
fi
test "$(docker ps -a --filter name='^cnz-weknora-' --format '{{.Names}}' | sort)" = "$expected_containers"
for c in cnz-weknora-frontend cnz-weknora-docreader; do test "$(docker inspect -f '{{.State.Status}}' "$c")" = running; done
test "$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-docreader)" = healthy
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:39301/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18082/)" = 200
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$expected_volumes"
for volume in $expected_volumes; do
  logical="${volume#cnz-weknora_}"
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume")" = cnz-weknora
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' "$volume")" = "$logical"
done
for container in $expected_containers; do
  test "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container")" = cnz-weknora
done
assert_mounts() {
  container="$1"; expected="$2"
  docker inspect -f '{{json .Mounts}}' "$container" | python3 -c 'import json,sys; mounts=json.load(sys.stdin); actual=[]
for m in mounts: actual.append([m.get("Type"),m.get("Destination"),m.get("Name") if m.get("Type")=="volume" else m.get("Source")])
assert sorted(actual)==sorted(json.loads(sys.argv[2])), (sys.argv[1],actual)' "$container" "$expected"
}
app_mounts="$(python3 -c 'import json,sys; config=sys.argv[1]; print(json.dumps([["volume","/data/files","cnz-weknora_data-files"],["volume","/tmp/docreader","cnz-weknora_docreader-tmp"],["bind","/app/config/config.yaml",config]]))' "$runtime_config")"
assert_mounts cnz-weknora-app "$app_mounts"
if [ "$bootstrap_present" -eq 1 ]; then
  bootstrap_mounts="$(python3 -c 'import json,sys; config=sys.argv[1]; print(json.dumps([["volume","/data/files","cnz-weknora_data-files"],["volume","/tmp/docreader","cnz-weknora_docreader-tmp"],["bind","/app/config/config.yaml",config]]))' "$bootstrap_config")"
  assert_mounts cnz-weknora-bootstrap "$bootstrap_mounts"
fi
assert_mounts cnz-weknora-docreader '[["volume","/tmp/docreader","cnz-weknora_docreader-tmp"]]'
assert_mounts cnz-weknora-postgres '[["volume","/var/lib/postgresql/data","cnz-weknora_postgres-data"]]'
assert_mounts cnz-weknora-redis '[["volume","/data","cnz-weknora_redis-data"]]'
if [ "$ollama_volume_present" -eq 1 ] && [ "$ollama_present" -eq 1 ]; then
  assert_mounts cnz-weknora-ollama '[["volume","/root/.ollama","cnz-weknora_ollama-data"]]'
fi
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(df -Pk /var/backups | awk 'NR==2 {print $4}')" -ge 10485760
schema="$(docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select version,dirty from schema_migrations order by version desc limit 1"')"
[[ "$schema" =~ ^[0-9]+\|f$ ]]
test ! -L "$backup_root"
install -d -m 0700 "$backup_root"
test ! -L "$backup_root"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$(mktemp -d "$backup_root/${stamp}-${source_sha}.XXXXXX")"
chmod 0700 "$backup"
umask 077
app_stopped=0; bootstrap_stopped=0; docreader_stopped=0; redis_stopped=0; ollama_stopped=0
backup_complete=0
restart_services() {
  failures=0
  if [ "$ollama_stopped" -eq 1 ]; then
    if docker start cnz-weknora-ollama >/dev/null; then ollama_stopped=0; else failures=1; echo CNZ_RESTART_START_FAILED=ollama; fi
  fi
  if [ "$redis_stopped" -eq 1 ]; then
    if docker start cnz-weknora-redis >/dev/null; then redis_stopped=0; else failures=1; echo CNZ_RESTART_START_FAILED=redis; fi
  fi
  if [ "$docreader_stopped" -eq 1 ]; then
    if docker start cnz-weknora-docreader >/dev/null; then docreader_stopped=0; else failures=1; echo CNZ_RESTART_START_FAILED=docreader; fi
  fi
  if [ "$app_stopped" -eq 1 ]; then
    if docker start cnz-weknora-app >/dev/null; then app_stopped=0; else failures=1; echo CNZ_RESTART_START_FAILED=app; fi
  fi
  if [ "$bootstrap_stopped" -eq 1 ]; then
    if docker start cnz-weknora-bootstrap >/dev/null; then bootstrap_stopped=0; else failures=1; echo CNZ_RESTART_START_FAILED=bootstrap; fi
  fi
  ready=0
  for _ in $(seq 1 60); do
    app="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-app 2>/dev/null || true)"
    redis="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-redis 2>/dev/null || true)"
    ollama="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-ollama 2>/dev/null || true)"
    doc="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-docreader 2>/dev/null || true)"
    bootstrap="$(docker inspect -f '{{.State.Health.Status}}' cnz-weknora-bootstrap 2>/dev/null || true)"
    api="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:39301/health 2>/dev/null || true)"
    ui="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:18082/ 2>/dev/null || true)"
    if { [ "$ollama_present" -eq 0 ] || [ "$ollama" = healthy ]; } && { [ "$bootstrap_present" -eq 0 ] || [ "$bootstrap" = healthy ]; } && [ "$app" = healthy ] && [ "$redis" = healthy ] && [ "$doc" = healthy ] && [ "$api" = 200 ] && [ "$ui" = 200 ]; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then failures=1; fi
  if [ "$bootstrap_present" -eq 1 ] && ! curl -fsS --max-time 4 http://127.0.0.1:39302/api/v1/auth/config | python3 -c 'import json,sys; assert json.load(sys.stdin).get("registration_mode")=="self_serve"'; then failures=1; echo CNZ_RESTART_START_FAILED=bootstrap_config; fi
  return "$failures"
}
on_exit() {
  code=$?; trap - EXIT; set +e
  if [ "$code" -eq 0 ] && [ "$backup_complete" -ne 1 ]; then
    echo CNZ_BACKUP_NOT_COMPLETE
    code=91
  fi
  if [ "$code" -ne 0 ] && [ -d "$backup" ] && [ ! -L "$backup" ]; then
    touch "$backup/INCOMPLETE"; chmod 0600 "$backup/INCOMPLETE"
  fi
  restart_services
  restarted=$?
  if [ "$restarted" -ne 0 ]; then echo CNZ_BACKUP_SERVICE_RESTART_FAILED; exit 90; fi
  if [ "$code" -ne 0 ]; then echo "CNZ_BACKUP_FAILED_STAGE=$stage"; exit "$code"; fi
  echo "CNZ_BACKUP_OK=$backup"
}
trap on_exit EXIT
stage=quiesce
if [ "$bootstrap_present" -eq 1 ]; then bootstrap_stopped=1; docker stop --time 60 cnz-weknora-bootstrap >/dev/null; fi
app_stopped=1; docker stop --time 60 cnz-weknora-app >/dev/null
docreader_stopped=1; docker stop --time 60 cnz-weknora-docreader >/dev/null
redis_stopped=1; docker stop --time 60 cnz-weknora-redis >/dev/null
if [ "$ollama_present" -eq 1 ]; then ollama_stopped=1; docker stop --time 60 cnz-weknora-ollama >/dev/null; fi
stage=database_dump
app_image="$(docker inspect -f '{{.Config.Image}}' cnz-weknora-app)"
pg_image="$(docker inspect -f '{{.Config.Image}}' cnz-weknora-postgres)"
docreader_image="$(docker inspect -f '{{.Config.Image}}' cnz-weknora-docreader)"
redis_image="$(docker inspect -f '{{.Config.Image}}' cnz-weknora-redis)"
ollama_image=none
if [ "$ollama_present" -eq 1 ]; then ollama_image="$(docker inspect -f '{{.Config.Image}}' cnz-weknora-ollama)"
elif [ "$ollama_volume_present" -eq 1 ]; then ollama_image='ollama/ollama@sha256:b2f0122c476bc9fe7ce679d8bb4bd555351e25e2554eefb156f61cb0a89cf429'; fi
docker exec cnz-weknora-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup/database.dump"
docker run --rm --network none --mount type=volume,source=cnz-weknora_data-files,target=/data/files,readonly --entrypoint /usr/bin/tar "$app_image" -czf - -C /data/files . > "$backup/files.tar.gz"
docker run --rm --network none --mount type=volume,source=cnz-weknora_docreader-tmp,target=/tmp/docreader,readonly --entrypoint /usr/bin/tar "$docreader_image" -czf - -C /tmp/docreader . > "$backup/docreader-tmp.tar.gz"
docker run --rm --network none --mount type=volume,source=cnz-weknora_redis-data,target=/data,readonly --entrypoint /bin/tar "$redis_image" -czf - -C /data . > "$backup/redis-volume.tar.gz"
if [ "$ollama_volume_present" -eq 1 ]; then
  docker run --rm --network none --mount type=volume,source=cnz-weknora_ollama-data,target=/root/.ollama,readonly --entrypoint /bin/tar "$ollama_image" -czf - -C /root/.ollama . > "$backup/ollama-data.tar.gz"
fi
if [ -f "$release/DEPLOYED_FROM_CODE" ]; then
  cp -- "$release/DEPLOYED_FROM_CODE" "$backup/DEPLOYED_FROM_CODE"
else
  cp -- "$release/DEPLOYED_FROM_MAIN" "$backup/DEPLOYED_FROM_MAIN"
fi
chmod 0600 "$backup"/*
stage=verify_archives
test -s "$backup/database.dump" && test -s "$backup/files.tar.gz" && test -s "$backup/docreader-tmp.tar.gz" && test -s "$backup/redis-volume.tar.gz"
gzip -t "$backup/files.tar.gz" "$backup/docreader-tmp.tar.gz" "$backup/redis-volume.tar.gz"
tar -tzf "$backup/files.tar.gz" >/dev/null
tar -tzf "$backup/docreader-tmp.tar.gz" >/dev/null
tar -tzf "$backup/redis-volume.tar.gz" >/dev/null
if [ "$ollama_volume_present" -eq 1 ]; then
  test -s "$backup/ollama-data.tar.gz"
  gzip -t "$backup/ollama-data.tar.gz"
  tar -tzf "$backup/ollama-data.tar.gz" >/dev/null
fi
docker run --rm --network none --mount type=bind,source="$backup",target=/backup,readonly --entrypoint pg_restore "$pg_image" --list /backup/database.dump > "$backup/database.contents.txt"
test -s "$backup/database.contents.txt"
printf 'active_source_sha=%s\nactive_source_fingerprint=%s\nruntime_source_sha=%s\nbootstrap_source_sha=%s\nruntime_config_path=%s\nruntime_config_sha256=%s\nbootstrap_config_path=%s\nbootstrap_config_sha256=%s\ndatabase_schema=%s\napp_image=%s\npostgres_image=%s\ndocreader_image=%s\nredis_image=%s\nollama_image=%s\n' \
  "$source_sha" "$(fingerprint_source "$active")" "$runtime_source_sha" "$bootstrap_source_sha" "$runtime_config" "$(sha256sum "$runtime_config" | cut -d' ' -f1)" "$bootstrap_config" "$bootstrap_config_sha256" "$schema" "$app_image" "$pg_image" "$docreader_image" "$redis_image" "$ollama_image" > "$backup/METADATA.txt"
printf 'active_env_path=%s\nactive_env_mode=%s\nruntime_env_path=%s\nruntime_env_mode=%s\n' \
  "$active/.env" "$(stat -c %a "$active/.env")" "$runtime_source/.env" "$(stat -c %a "$runtime_source/.env")" >> "$backup/METADATA.txt"
chmod 0600 "$backup/METADATA.txt" "$backup/database.contents.txt"
find "$backup" -maxdepth 1 -type f ! -name SHA256SUMS ! -name INCOMPLETE -print0 | sort -z | xargs -0 sha256sum > "$backup/SHA256SUMS"
chmod 0600 "$backup/SHA256SUMS"
(cd "$backup" && sha256sum -c SHA256SUMS >/dev/null)
test "$(stat -c %a "$backup")" = 700
test -z "$(find "$backup" -type f ! -perm 0600 -print -quit)"
backup_complete=1
stage=restart
'''


def backup(target: str) -> str:
    if target not in TARGETS:
        raise ValueError("F1: only the explicitly authorized testkb alias yunan-101 is supported")
    result = base.remote(target, REMOTE_BACKUP, "cnz-backup", 1800)
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    if result.returncode != 0 or "CNZ_BACKUP_OK=" not in output:
        match = re.search(r"CNZ_BACKUP_FAILED_STAGE=([a-z_]+)", output)
        stage = match.group(1) if match else "unknown"
        if "F2_INVALID_ACTIVE_RELEASE" in output:
            stage = "invalid_active_release"
        if "CNZ_BACKUP_SERVICE_RESTART_FAILED" in output:
            stage = "service_restart"
        raise RuntimeError(f"F2/F3/F4/F5: CNZ backup stopped safely at stage {stage}")
    line = next(line for line in output.splitlines() if line.startswith("CNZ_BACKUP_OK="))
    return line.split("=", 1)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="authorized testkb SSH alias (yunan-101 only)")
    args = parser.parse_args()
    try:
        path = backup(args.target)
    except (OSError, RuntimeError, ValueError, StopIteration) as exc:
        print(f"CNZ backup stopped safely: {exc}", file=sys.stderr)
        return 1
    print(f"CNZ backup verified at {path}; only CNZ services/volumes were touched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
