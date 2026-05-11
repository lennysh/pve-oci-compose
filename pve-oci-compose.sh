#!/usr/bin/env bash
# Declarative OCI LXC compose helper for Proxmox VE: read compose.yaml, drive
# oci_ct_create and oci_ct_rootfs_refresh. YAML is loaded with Python + PyYAML
# (stdlib has no YAML); on Debian/PVE: apt install python3-yaml
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Worker scripts live beside this script: oci_ct_create/, oci_ct_rootfs_refresh/
REPO_ROOT="$SCRIPT_DIR"
CREATE_SH="${REPO_ROOT}/oci_ct_create/oci-ct-create-from-registry.sh"
REFRESH_SH="${REPO_ROOT}/oci_ct_rootfs_refresh/oci-ct-refresh-rootfs.sh"

COMPOSE_FILE="${COMPOSE_FILE:-${PWD}/compose.yaml}"
ADOPT=0
FORCE_REFRESH=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: pve-oci-compose.sh [options] <command>

Commands:
  plan      Show create / refresh intent per service (no changes).
  apply     Create CTs that are missing (pct create via oci-ct-create-from-registry.sh).
  refresh   Run oci-ct-refresh-rootfs.sh when the compose image ref differs from the
            description marker (see below), or with --force.
  pull      Pull OCI templates only (--pull-only on create script) for each service.

Options:
  -f, --file PATH   Compose file (default: ./compose.yaml or $COMPOSE_FILE)
  --adopt           Allow refresh on CTs whose description is not yet marked by this tool
                    (sets marker after a successful refresh).
  --force           refresh: run refresh even when stored ref matches compose image.
  -n, --dry-run     Print commands only (apply / refresh / pull).

Description marker (pct --description) after create / refresh:
  pve-oci-compose service=<name> ref=<image>

Requirements:
  - Run on a PVE node as root; jq; python3; PyYAML (python3-yaml package).
  - Same expectations as oci_ct_create / oci_ct_rootfs_refresh (pvesh, pct, skopeo, …).

Compose schema (per service; shallow merge from top-level "defaults"):
  vmid               (required) CT VMID
  image or reference (required) OCI ref for create / refresh (same as existing scripts)
  rootfs             (required for apply) e.g. local-zfs:8
  template_storage   vztmpl storage id for oci-registry-pull (optional; see create script)
  hostname, net0, node, memory, cores, ostype, unprivileged, features, onboot
  mounts             list of strings "STORAGE:GiB:/path" passed as --mp to create
EOF
  exit 1
}

die() { echo "pve-oci-compose: $*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "run as root on a Proxmox node"
}

require_tools() {
  command -v jq >/dev/null 2>&1 || die "jq is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  python3 <<'PY' || die "PyYAML is required (apt install python3-yaml)"
import sys
try:
    import yaml  # noqa: F401
except ImportError:
    sys.stderr.write("pve-oci-compose: cannot import yaml — install PyYAML, e.g. apt install python3-yaml\n")
    sys.exit(1)
PY
  [[ -f "$CREATE_SH" ]] || die "missing create script: $CREATE_SH"
  [[ -f "$REFRESH_SH" ]] || die "missing refresh script: $REFRESH_SH"
  [[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"
}

compose_json() {
  python3 - "$COMPOSE_FILE" <<'PY'
import json, pathlib, sys

try:
    import yaml
except ImportError:
    sys.stderr.write("pve-oci-compose: cannot import yaml — apt install python3-yaml\n")
    sys.exit(1)

path = pathlib.Path(sys.argv[1])
try:
    raw = path.read_text(encoding="utf-8")
except OSError as e:
    sys.stderr.write(f"pve-oci-compose: read {path}: {e}\n")
    sys.exit(1)

doc = yaml.safe_load(raw)
if doc is None:
    doc = {}
if not isinstance(doc, dict):
    sys.stderr.write("pve-oci-compose: root of compose file must be a mapping\n")
    sys.exit(1)

defaults = doc.get("defaults") or {}
if defaults is not None and not isinstance(defaults, dict):
    sys.stderr.write("pve-oci-compose: 'defaults' must be a mapping\n")
    sys.exit(1)
defaults = defaults or {}

services = doc.get("services") or {}
if not isinstance(services, dict):
    sys.stderr.write("pve-oci-compose: 'services' must be a mapping\n")
    sys.exit(1)

out = {
    "project": doc.get("name") or doc.get("project"),
    "services": {},
}
for sname, svc in services.items():
    if svc is None:
        continue
    if not isinstance(svc, dict):
        sys.stderr.write(f"pve-oci-compose: service {sname!r} must be a mapping\n")
        sys.exit(1)
    merged = {**defaults, **svc}
    merged["_service"] = sname
    out["services"][sname] = merged

print(json.dumps(out))
PY
}

pct_description() {
  local vmid="$1"
  pct config "$vmid" 2>/dev/null | sed -n 's/^description: //p' | head -1 || true
}

expected_description() {
  local svc="$1" ref="$2"
  printf 'pve-oci-compose service=%s ref=%s\n' "$svc" "$ref"
}

extract_managed_ref() {
  local desc="$1"
  [[ "$desc" == pve-oci-compose* ]] || { printf '%s\n' ""; return 0; }
  if [[ "$desc" =~ ref=(.*)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf '%s\n' ""
  fi
}

service_image() {
  jq -r '.image // .reference // empty' <<<"$1"
}

validate_service() {
  local json="$1" svc="$2"
  local vmid image rootfs
  vmid="$(jq -r '.vmid // empty' <<<"$json")"
  image="$(service_image "$json")"
  rootfs="$(jq -r '.rootfs // empty' <<<"$json")"
  [[ -n "$vmid" ]] || die "service $svc: missing vmid"
  [[ "$vmid" =~ ^[0-9]+$ ]] || die "service $svc: vmid must be an integer"
  [[ -n "$image" ]] || die "service $svc: missing image (or reference)"
  [[ -n "$rootfs" ]] || die "service $svc: missing rootfs"
}

validate_service_pull() {
  local json="$1" svc="$2"
  local image
  image="$(service_image "$json")"
  [[ -n "$image" ]] || die "service $svc: missing image (or reference)"
}

run_or_print() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

cmd_plan() {
  local json sname svc vmid image rootfs desc stored exists
  json="$(compose_json)"
  echo "Compose file: $COMPOSE_FILE"
  jq -r '.project // empty' <<<"$json" | sed '/^$/d' | sed 's/^/Project: /' || true
  echo

  while IFS= read -r sname; do
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service "$svc" "$sname"
    vmid="$(jq -r '.vmid' <<<"$svc")"
    image="$(service_image "$svc")"
    rootfs="$(jq -r '.rootfs' <<<"$svc")"
    desc="$(pct_description "$vmid")"
    stored="$(extract_managed_ref "$desc")"

    if pct config "$vmid" &>/dev/null; then
      exists=yes
    else
      exists=no
    fi

    echo "=== service: $sname (vmid $vmid) ==="
    echo "  exists:        $exists"
    echo "  image (file):  $image"
    echo "  rootfs:        $rootfs"
    echo "  description:   ${desc:-<none>}"
    if [[ "$exists" == yes ]]; then
      if [[ -z "$stored" ]]; then
        echo "  plan apply:    no-op (already exists)"
        echo "  plan refresh:  blocked (not managed) — use --adopt on refresh"
      else
        echo "  stored ref:    $stored"
        if [[ "$stored" == "$image" ]]; then
          echo "  plan refresh:  skip (ref matches)"
        else
          echo "  plan refresh:  would run refresh → $image"
        fi
        echo "  plan apply:    no-op (already exists)"
      fi
    else
      echo "  plan apply:    would create from $image"
      echo "  plan refresh:  n/a (CT missing)"
    fi
    echo
  done < <(jq -r '.services | keys[]' <<<"$json")
}

fill_create_args() {
  local svcjson="$1"
  local ts ref vmid hostname net0 node mem cores ostype feats v

  OCI_CREATE_ARGS=()
  ts="$(jq -r '.template_storage // .storage // empty' <<<"$svcjson")"
  ref="$(service_image "$svcjson")"
  vmid="$(jq -r '.vmid' <<<"$svcjson")"
  hostname="$(jq -r '.hostname // empty' <<<"$svcjson")"
  net0="$(jq -r '.net0 // empty' <<<"$svcjson")"
  node="$(jq -r '.node // empty' <<<"$svcjson")"
  mem="$(jq -r '.memory // empty' <<<"$svcjson")"
  cores="$(jq -r '.cores // empty' <<<"$svcjson")"
  ostype="$(jq -r '.ostype // empty' <<<"$svcjson")"
  feats="$(jq -r '.features // empty' <<<"$svcjson")"

  [[ -n "$ref" ]] || die "internal: empty image"
  OCI_CREATE_ARGS+=(--reference "$ref" --rootfs "$(jq -r '.rootfs' <<<"$svcjson")" --vmid "$vmid")
  [[ -n "$ts" ]] && OCI_CREATE_ARGS+=(--storage "$ts")
  [[ -n "$hostname" ]] && OCI_CREATE_ARGS+=(--hostname "$hostname")
  [[ -n "$net0" ]] && OCI_CREATE_ARGS+=(--net0 "$net0")
  [[ -n "$node" ]] && OCI_CREATE_ARGS+=(--node "$node")
  [[ -n "$mem" ]] && OCI_CREATE_ARGS+=(--memory "$mem")
  [[ -n "$cores" ]] && OCI_CREATE_ARGS+=(--cores "$cores")
  [[ -n "$ostype" ]] && OCI_CREATE_ARGS+=(--ostype "$ostype")
  [[ -n "$feats" ]] && OCI_CREATE_ARGS+=(--features "$feats")

  if jq -e '.onboot != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.onboot | type) == "boolean" then (if .onboot then "1" else "0" end) else "\(.onboot)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--onboot "$v")
  fi
  if jq -e '.unprivileged != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.unprivileged | type) == "boolean" then (if .unprivileged then "1" else "0" end) else "\(.unprivileged)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--unprivileged "$v")
  fi

  while IFS= read -r mp; do
    [[ -z "$mp" ]] && continue
    OCI_CREATE_ARGS+=(--mp "$mp")
  done < <(jq -r '.mounts[]? | strings' <<<"$svcjson")
}

OCI_CREATE_ARGS=()

cmd_apply() {
  local json sname svc vmid image
  json="$(compose_json)"
  while IFS= read -r sname; do
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service "$svc" "$sname"
    vmid="$(jq -r '.vmid' <<<"$svc")"
    image="$(service_image "$svc")"

    if pct config "$vmid" &>/dev/null; then
      echo "apply: [$sname] vmid $vmid already exists — skip create"
      continue
    fi

    echo "apply: [$sname] creating vmid $vmid from $image"
    fill_create_args "$svc"
    run_or_print "$CREATE_SH" "${OCI_CREATE_ARGS[@]}"

    if [[ "$DRY_RUN" -eq 0 ]]; then
      pct set "$vmid" --description "$(expected_description "$sname" "$image")"
      echo "apply: [$sname] set description marker for refresh tracking"
    fi
  done < <(jq -r '.services | keys[]' <<<"$json")
}

cmd_refresh() {
  local json sname svc vmid image desc stored want
  json="$(compose_json)"
  while IFS= read -r sname; do
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service "$svc" "$sname"
    vmid="$(jq -r '.vmid' <<<"$svc")"
    image="$(service_image "$svc")"

    pct config "$vmid" &>/dev/null || die "refresh: [$sname] vmid $vmid does not exist"

    desc="$(pct_description "$vmid")"
    stored="$(extract_managed_ref "$desc")"

    if [[ -z "$stored" ]]; then
      if [[ "$ADOPT" -ne 1 ]]; then
        echo "refresh: [$sname] CT $vmid not managed (no pve-oci-compose description) — skip (use --adopt)"
        continue
      fi
      echo "refresh: [$sname] --adopt: treating unmanaged CT $vmid as $sname"
    fi

    if [[ "$FORCE_REFRESH" -eq 0 && -n "$stored" && "$stored" == "$image" ]]; then
      echo "refresh: [$sname] image unchanged — skip"
      continue
    fi

    echo "refresh: [$sname] vmid $vmid → $image"
    run_or_print "$REFRESH_SH" "$vmid" "$image"

    if [[ "$DRY_RUN" -eq 0 ]]; then
      pct set "$vmid" --description "$(expected_description "$sname" "$image")"
    fi
  done < <(jq -r '.services | keys[]' <<<"$json")
}

cmd_pull() {
  local json sname svc ts ref
  local -a pull_args
  json="$(compose_json)"
  while IFS= read -r sname; do
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service_pull "$svc" "$sname"
    ref="$(service_image "$svc")"
    ts="$(jq -r '.template_storage // .storage // empty' <<<"$svc")"
    echo "pull: [$sname] $ref"
    pull_args=(--pull-only --reference "$ref")
    [[ -n "$ts" ]] && pull_args=(--storage "$ts" "${pull_args[@]}")
    run_or_print "$CREATE_SH" "${pull_args[@]}"
  done < <(jq -r '.services | keys[]' <<<"$json")
}

CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file)       COMPOSE_FILE="${2:?}"; shift 2 ;;
    --adopt)         ADOPT=1; shift ;;
    --force)         FORCE_REFRESH=1; shift ;;
    -n|--dry-run)    DRY_RUN=1; shift ;;
    -h|--help)       usage ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      [[ -z "$CMD" ]] || die "extra argument: $1"
      CMD="$1"
      shift
      ;;
  esac
done

[[ -n "$CMD" ]] || usage

case "$CMD" in
  plan)
    require_root
    require_tools
    cmd_plan
    ;;
  apply|refresh|pull)
    require_root
    require_tools
    "cmd_${CMD}"
    ;;
  *)
    die "unknown command: $CMD (try plan, apply, refresh, pull)"
    ;;
esac
