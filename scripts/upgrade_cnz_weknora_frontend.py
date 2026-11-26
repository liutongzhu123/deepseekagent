#!/usr/bin/env python3
"""Safely replace only the CNZ WeKnora frontend on the isolated testkb stack.

The existing app, database, Redis, docreader, and named volumes are not
recreated. The old frontend image and release are retained for rollback.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_ROOT = "/opt/cnz-weknora"
ARCHIVE_URL = "https://github.com/Tencent/WeKnora/archive/refs/tags/v0.9.1.tar.gz"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_BASE_SPEC = importlib.util.spec_from_file_location(
    "deploy_cnz_weknora_testkb", ROOT / "scripts/deploy_cnz_weknora_testkb.py"
)
assert _BASE_SPEC and _BASE_SPEC.loader
base = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(base)

UPGRADE_PREFLIGHT = r'''set -euo pipefail
sha="$1"; source_fingerprint="$2"; root=/opt/cnz-weknora
incoming="$root/.incoming-$sha"; release="$root/releases/$sha"
[[ "$source_fingerprint" =~ ^[0-9a-f]{64}$ ]]
active="$(readlink -f "$root/active-release")"
case "$active" in "$root"/releases/*/source/WeKnora-0.9.1) ;; *) exit 31 ;; esac
old_release="$(dirname "$(dirname "$active")")"
old_sha="$(basename "$old_release")"
test "$old_sha" != "$sha"
if [ -f "$old_release/DEPLOYED_FROM_CODE" ]; then
  grep -Fxq "$old_sha" "$old_release/DEPLOYED_FROM_CODE"
else
  grep -Fxq "$old_sha" "$old_release/DEPLOYED_FROM_MAIN"
fi
test "$(stat -c %a "$active/.env")" = 600
test ! -e "$release" && test ! -L "$release"
test ! -e "$incoming" && test ! -L "$incoming"
(cd /var/backups/testkb-yunan-persist-20261124 && sha256sum -c SHA256SUMS >/dev/null)
test "$(docker ps --filter name='^cnz-weknora-app$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-postgres$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
test "$(docker ps --filter name='^cnz-weknora-redis$' --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 1
expected_containers="$(printf '%s\n' cnz-weknora-app cnz-weknora-frontend cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis | sort)"
test "$(docker ps -a --filter name='^cnz-weknora-' --format '{{.Names}}' | sort)" = "$expected_containers"
for c in cnz-weknora-frontend cnz-weknora-docreader; do test "$(docker inspect -f '{{.State.Status}}' "$c")" = running; done
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:39301/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:18082/)" = 200
expected_volumes="$(printf '%s\n' cnz-weknora_data-files cnz-weknora_docreader-tmp cnz-weknora_postgres-data cnz-weknora_redis-data | sort)"
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$expected_volumes"
for volume in $expected_volumes; do
  logical="${volume#cnz-weknora_}"
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume")" = cnz-weknora
  test "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.volume" }}' "$volume")" = "$logical"
done
test "$(docker ps --filter name=yunan-persist --format '{{.Status}}' | grep -c '(healthy)' || true)" -eq 5
test "$(df -Pk /opt | awk 'NR==2 {print $4}')" -ge 10485760
old_image="$(docker inspect -f '{{.Image}}' cnz-weknora-frontend)"
test -n "$old_image"
printf 'UPGRADE_PREFLIGHT_OK\nOLD_SHA=%s\nOLD_IMAGE=%s\n' "$old_sha" "$old_image"
'''

CONFIGURE_UPGRADE = r'''set -euo pipefail
sha="$1"; root=/opt/cnz-weknora; incoming="$root/.incoming-$sha"; release="$root/releases/$sha"
active="$(readlink -f "$root/active-release")"
project="$incoming/source/WeKnora-0.9.1"
test -f "$project/docker-compose.yml"
test -f "$incoming/docker-compose.testkb.override.yml"
grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_MAIN"
test ! -e "$release" && test ! -L "$release"
cp -- "$active/.env" "$project/.env"
chmod 0600 "$project/.env"
test "$(stat -c %a "$project/.env")" = 600
mv -T "$incoming" "$release"
echo FRONTEND_RELEASE_READY
'''

UPGRADE = r'''set -Eeuo pipefail
sha="$1"; old_sha="$2"; old_image="$3"; source_fingerprint="$4"; root=/opt/cnz-weknora
exec 9>"$root/.deploy.lock"
flock -n 9 || { echo CNZ_DEPLOY_LOCKED; exit 40; }
new_release="$root/releases/$sha"; old_release="$root/releases/$old_sha"
new_project="$new_release/source/WeKnora-0.9.1"
old_project="$old_release/source/WeKnora-0.9.1"
rollback_tag="cnz-weknora-ui:rollback-$old_sha"
fingerprint_source() {
  python3 - "$1" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1]); h = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
    rel = path.relative_to(root).as_posix()
    if rel == ".env" or ".git" in path.relative_to(root).parts: continue
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_symlink(): kind, payload = b"L", os.readlink(path).encode()
    elif path.is_dir(): kind, payload = b"D", b""
    elif path.is_file(): kind, payload = b"F", path.read_bytes()
    else: raise SystemExit(f"unsupported source entry: {rel}")
    h.update(kind + mode.to_bytes(4, "big") + rel.encode() + b"\0")
    h.update(len(payload).to_bytes(8, "big")); h.update(payload)
print(h.hexdigest())
PY
}
compose_new() { docker compose -p cnz-weknora --project-directory "$new_project" --env-file "$new_project/.env" -f "$new_project/docker-compose.yml" -f "$new_release/docker-compose.testkb.override.yml" "$@"; }
compose_old() { docker compose -p cnz-weknora --project-directory "$old_project" --env-file "$old_project/.env" -f "$old_project/docker-compose.yml" -f "$old_release/docker-compose.testkb.override.yml" "$@"; }
assert_compose() {
  "$@" config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); s=c["services"]; assert set(s)=={"app","frontend","docreader","postgres","redis"}; assert all(s[k]["container_name"]==v for k,v in {"app":"cnz-weknora-app","frontend":"cnz-weknora-frontend","docreader":"cnz-weknora-docreader","postgres":"cnz-weknora-postgres","redis":"cnz-weknora-redis"}.items()); assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==80 and p.get("published")=="18082" for p in s["frontend"].get("ports",[])); assert any(p.get("host_ip")=="127.0.0.1" and p.get("target")==8080 and p.get("published")=="39301" for p in s["app"].get("ports",[])); assert set(c["volumes"])=={"data-files","docreader-tmp","postgres-data","redis-data"}; assert all(not v.get("external",False) and v.get("driver","local")=="local" and not v.get("driver_opts") and v.get("name") in (None,"cnz-weknora_"+k) for k,v in c["volumes"].items()); assert all("/var/run/docker.sock" not in str(m) for x in s.values() for m in x.get("volumes",[])); assert s["app"]["environment"]["DISABLE_REGISTRATION"]=="true"; assert s["app"]["environment"]["OIDC_AUTH_ENABLE"]=="false"'
}
wait_frontend() {
  expected_image="$1"; ready=0
  for _ in $(seq 1 45); do
    current="$(docker inspect -f '{{.Image}}' cnz-weknora-frontend 2>/dev/null || true)"
    status="$(docker inspect -f '{{.State.Status}}' cnz-weknora-frontend 2>/dev/null || true)"
    ui="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:18082/ 2>/dev/null || true)"
    api="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:39301/health 2>/dev/null || true)"
    if [ "$current" = "$expected_image" ] && [ "$status" = running ] && [ "$ui" = 200 ] && [ "$api" = 200 ]; then ready=1; break; fi
    sleep 2
  done
  test "$ready" -eq 1
}
frontend_switch_started=0; phase=compose_contract
rollback() {
  code=$?
  trap - EXIT
  set +e
  if [ "$code" -ne 0 ]; then echo "DEPLOY_FAILED_PHASE=$phase"; fi
  if [ "$code" -ne 0 ] && [ "$frontend_switch_started" -eq 1 ]; then
    echo FRONTEND_UPGRADE_FAILED_ROLLING_BACK
    docker image tag "$old_image" cnz-weknora-ui:dev || { echo FRONTEND_ROLLBACK_FAILED_IMAGE_TAG; exit "$code"; }
    compose_old up -d --no-deps frontend || { echo FRONTEND_ROLLBACK_FAILED_RECREATE; exit "$code"; }
    wait_frontend "$old_image" || { echo FRONTEND_ROLLBACK_FAILED_HEALTH; exit "$code"; }
    if [ "$(readlink -f "$root/active-release")" = "$new_project" ]; then
      ln -s "$old_project" "$root/active-release.rollback.$sha" || { echo FRONTEND_ROLLBACK_FAILED_POINTER_LINK; exit "$code"; }
      mv -Tf "$root/active-release.rollback.$sha" "$root/active-release" || { echo FRONTEND_ROLLBACK_FAILED_POINTER_SWITCH; exit "$code"; }
    fi
    echo FRONTEND_ROLLBACK_OK_APP_AND_VOLUMES_UNTOUCHED
  fi
  exit "$code"
}
trap rollback EXIT
test "$(readlink -f "$root/active-release")" = "$old_project" || { echo FRONTEND_ACTIVE_POINTER_CHANGED; exit 1; }
assert_compose compose_new
assert_compose compose_old
test "$(docker inspect -f '{{.Image}}' cnz-weknora-frontend)" = "$old_image"
test "$(fingerprint_source "$new_project")" = "$source_fingerprint"
other_containers=(cnz-weknora-app cnz-weknora-docreader cnz-weknora-postgres cnz-weknora-redis)
other_container_ids=()
for c in "${other_containers[@]}"; do
  other_container_ids+=("$(docker inspect -f '{{.Id}}' "$c")")
done
before_volumes="$(docker volume ls -q --filter name=cnz-weknora | sort)"
if docker image inspect "$rollback_tag" >/dev/null 2>&1; then
  test "$(docker image inspect -f '{{.Id}}' "$rollback_tag")" = "$old_image"
else
  docker image tag "$old_image" "$rollback_tag"
fi
phase=frontend_build
compose_new build frontend
new_image="$(docker image inspect -f '{{.Id}}' cnz-weknora-ui:dev)"
test -n "$new_image" && test "$new_image" != "$old_image"
phase=frontend_replace; frontend_switch_started=1
compose_new up -d --no-deps frontend
phase=frontend_health
wait_frontend "$new_image"
for i in "${!other_containers[@]}"; do
  c="${other_containers[$i]}"
  test "$(docker inspect -f '{{.Id}}' "$c")" = "${other_container_ids[$i]}"
done
test "$(docker volume ls -q --filter name=cnz-weknora | sort)" = "$before_volumes"
ln -s "$new_project" "$root/active-release.new.$sha"
mv -Tf "$root/active-release.new.$sha" "$root/active-release"
test "$(readlink -f "$root/active-release")" = "$new_project"
echo FRONTEND_UPGRADE_OK_APP_AND_VOLUMES_UNTOUCHED
echo "ACTIVE_RELEASE=$sha"
echo "ROLLBACK_IMAGE=$rollback_tag"
trap - EXIT
'''


def upgrade(target: str, upstream: Path | None = None) -> None:
    sha = base.main_sha()
    if not SHA_RE.fullmatch(sha):
        raise RuntimeError("F1: invalid main SHA")
    with tempfile.TemporaryDirectory(prefix="cnz-weknora-ui-") as temp_name:
        work = Path(temp_name)
        if upstream is None:
            upstream = work / "weknora-v0.9.1.tar.gz"
            downloaded = base.run([
                "curl", "--fail", "--location", "--silent", "--show-error",
                "--connect-timeout", "15", "--max-time", "300", ARCHIVE_URL,
                "--output", str(upstream),
            ], 320)
            if downloaded.returncode:
                raise RuntimeError("F2: official source download failed")
        bundle, fingerprint = base.make_bundle(sha, upstream, work)
        checked = base.remote(target, UPGRADE_PREFLIGHT, sha, 60, fingerprint)
        if checked.returncode or b"UPGRADE_PREFLIGHT_OK" not in checked.stdout:
            raise RuntimeError("F3: upgrade preflight rejected the target")
        match_sha = re.search(rb"OLD_SHA=([0-9a-f]{40})", checked.stdout)
        match_image = re.search(rb"OLD_IMAGE=(sha256:[0-9a-f]{64})", checked.stdout)
        if not match_sha or not match_image:
            raise RuntimeError("F3: preflight did not return the current release and image")
        old_sha, old_image = match_sha.group(1).decode(), match_image.group(1).decode()
        incoming = f"{TARGET_ROOT}/.incoming-{sha}"
        create = f"set -euo pipefail; test ! -e {incoming!r} && test ! -L {incoming!r}; install -d -m 0755 {incoming!r}"
        if base.remote(target, create, sha, 60).returncode:
            raise RuntimeError("F4: incoming release path already exists")
        if base.stream_bundle(target, sha, bundle).returncode:
            base.cleanup_incoming(target, sha)
            raise RuntimeError("F4: source transfer failed")
        configured = base.remote(target, CONFIGURE_UPGRADE, sha, 60)
        if configured.returncode or b"FRONTEND_RELEASE_READY" not in configured.stdout:
            base.cleanup_incoming(target, sha)
            raise RuntimeError("F4: release verification or environment copy failed")
        switched = remote_upgrade(target, sha, old_sha, old_image, fingerprint)
        if switched.returncode:
            match = re.search(rb"DEPLOY_FAILED_PHASE=([a-z_]+)", switched.stdout + switched.stderr)
            phase = match.group(1).decode("ascii") if match else "unknown"
            raise RuntimeError(f"F5: frontend update failed in {phase}; automatic rollback attempted")
        expected = (b"FRONTEND_UPGRADE_OK_APP_AND_VOLUMES_UNTOUCHED", f"ACTIVE_RELEASE={sha}".encode())
        if any(marker not in switched.stdout for marker in expected):
            raise RuntimeError("F6: frontend update completion marker missing")
        print(f"upgraded frontend main={sha}; previous release and rollback image retained")


def remote_upgrade(target: str, sha: str, old_sha: str, old_image: str, fingerprint: str):
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
        "bash", "-s", "--", sha, old_sha, old_image, fingerprint,
    ]
    return subprocess.run(
        args, input=UPGRADE.encode(), capture_output=True, timeout=900, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="authorized testkb SSH alias")
    parser.add_argument("--upstream-archive", type=Path)
    args = parser.parse_args()
    try:
        upgrade(args.target, args.upstream_archive)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"frontend upgrade stopped safely: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
