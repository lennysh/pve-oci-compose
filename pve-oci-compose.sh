#!/usr/bin/env bash
# Declarative OCI LXC compose helper for Proxmox VE: read compose.yaml, run OCI
# vztmpl pull + pct create and rootfs refresh (logic in lib/*.inc.sh). YAML is
# loaded with Python + PyYAML (on Debian/PVE: apt install python3-yaml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.inc.sh disable=SC1091
source "${SCRIPT_DIR}/lib/common.inc.sh"
# shellcheck source=lib/ui.inc.sh disable=SC1091
source "${SCRIPT_DIR}/lib/ui.inc.sh"
# shellcheck source=lib/oci-create.inc.sh disable=SC1091
source "${SCRIPT_DIR}/lib/oci-create.inc.sh"
# shellcheck source=lib/oci-refresh.inc.sh disable=SC1091
source "${SCRIPT_DIR}/lib/oci-refresh.inc.sh"

COMPOSE_FILE="${COMPOSE_FILE:-${PWD}/compose.yaml}"
ADOPT=0
FORCE_REFRESH=0
DRY_RUN=0
WRITE_COMPOSE_VMID=1

usage() {
  cat <<'EOF'
Usage: pve-oci-compose.sh [options] <command>

Commands:
  plan      Show create / refresh intent per service (no changes).
  apply     Create missing CTs (oci-registry-pull + pct create; see lib/oci-create.inc.sh).
  refresh   Replace rootfs when compose image ref differs from the guest marker JSON
            (or with --force); see lib/oci-refresh.inc.sh.
  pull      Pull OCI templates only (--pull-only) for each service.

Options:
  -f, --file PATH   Compose file (default: ./compose.yaml or $COMPOSE_FILE)
  --adopt           Refresh an unmanaged CT: no guest marker JSON yet (manual guest);
                    marker file + sentinel tag applied after success. Path: see
                    PVE_OCI_ROOTFS_MARKER (default /etc/pve-oci-compose.json).
  --force           refresh: run refresh even when stored ref matches compose image.
  -n, --dry-run     Print commands only (apply / refresh / pull).
  --no-write-compose After apply with vmid: next (or auto / null), do not rewrite the compose
                    file with the allocated id (default is to update the YAML).

vmid in compose:
  Use a fixed number, or allocate the cluster next free id with one of:
    vmid: next     vmid: auto     vmid: null   or omit vmid (same as next).
  After a successful apply, the compose file is updated in-place: only the first vmid: line
  under that service is changed (comments and blank lines are kept). The service key must be
  a plain name matching ^[a-zA-Z0-9_-]+$ and the file must already contain vmid: under the service.
  Use --no-write-compose to skip this step.

Compose marker after create / refresh:
  Sentinel tag **pve-oci-compose** (short UI hint) merged with existing tags,
  JSON file inside the CT rootfs (**/etc/pve-oci-compose.json** by default — **PVE_OCI_ROOTFS_MARKER**)
  holds canonical **service** + **ref** for plan/refresh; it is part of pct snapshots.

Resource pools (UI grouping):
  If compose has **name:** or **project:** (same value surfaced as **Project:** in plan), that string
  is the default **pct --pool** id. Missing pools are **auto-created** via **pvesh create /pools**
  (comment *pve-oci-compose (auto-created)*). Set **PVE_OCI_POOL_NO_AUTOCREATE=1** to require pre-created pools only.

Requirements:
  - Run on a PVE node as root; jq; python3; PyYAML (python3-yaml package).
  - pvesh, pct, skopeo, rsync, perl (PVE::Storage) as required by the inlined workflows.
  - Optional: env PVE_OCI_VERBOSE=1 — print extra hints and full pct/skopeo command lines during apply/pull/refresh.

Compose schema (per service; shallow merge from top-level "defaults"):
  vmid               Fixed CT VMID, or next / auto / null / omitted = allocate at apply (refresh needs a number).
  image or reference (required) OCI ref for create / refresh (same as existing scripts)
  rootfs             (required for apply) e.g. local-zfs:8
  template_storage   vztmpl storage id for oci-registry-pull (optional; see create script)
  hostname, net0 … net7   pct --netN (any net keys net0, net1, …); default net0 if none set
  node, memory, swap, cores, cpulimit, cpuunits, ostype, arch, unprivileged, features, onboot
  nameserver, searchdomain, entrypoint, env (map or list of KEY=val), description, guest_ports, about
  tags (pct UI tags). Top-level about (string) is optional stack notes merged into the built description.
  Top-level repo: optional string URL for the Source footer (default https://github.com/lennysh/pve-oci-compose);
  use repo: false to omit that footer from composed descriptions.
  timezone, password, ssh_public_keys (host file path), start, startup, hookscript, protection
  ha_managed, cmode, console, tty, ignore_unpack_errors, pct_debug, bwlimit
  lxc_dev            list of pct --dev0 … device specs (order = list order)
  unused_disks       list of pct --unused0 … volume specs (advanced)
  mounts             list of strings "STORAGE:GiB:/path" passed as --mp to create
  pool               optional; Datacenter resource pool id. Defaults to top-level name/project
                     (stack). Use pool: "" or pool: null in defaults to disable. Missing pools are
                     auto-created unless PVE_OCI_POOL_NO_AUTOCREATE=1.
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

stack_about = doc.get("about")
if stack_about is not None and not isinstance(stack_about, str):
    sys.stderr.write("pve-oci-compose: top-level 'about' must be a string (multiline text) if set\n")
    sys.exit(1)

repo = doc.get("repo")
if repo is not None and not isinstance(repo, (str, bool)):
    sys.stderr.write("pve-oci-compose: top-level 'repo' must be a string (URL), false, or omitted\n")
    sys.exit(1)

out = {
    "project": doc.get("name") or doc.get("project"),
    "stack_about": stack_about,
    "repo": repo,
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

service_image() {
  jq -r '.image // .reference // empty' <<<"$1"
}

# Echo "next" or a numeric vmid string (YAML null / missing vmid → next).
vmid_spec_from_json() {
  jq -r '
    .vmid as $v
    | if $v == null then "next"
      elif ($v | type) == "number" then ($v | tostring)
      elif ($v | type) == "string" then
        if (($v | ascii_downcase) == "next" or ($v | ascii_downcase) == "auto") then "next"
        elif ($v | test("^[0-9]+$")) then $v
        else "invalid:\($v)" end
      else "next" end
  ' <<<"$1"
}

validate_service() {
  local json="$1" svc="$2"
  local spec image rootfs sk
  spec="$(vmid_spec_from_json "$json")"
  image="$(service_image "$json")"
  rootfs="$(jq -r '.rootfs // empty' <<<"$json")"
  [[ "$spec" != invalid:* ]] || die "service $svc: vmid must be a number, next, auto, or null (got ${spec#invalid:})"
  [[ -n "$image" ]] || die "service $svc: missing image (or reference)"
  [[ -n "$rootfs" ]] || die "service $svc: missing rootfs"
  sk="$(jq -r '.ssh_public_keys // empty | if type == "string" then . else empty end' <<<"$json")"
  [[ -z "$sk" ]] || [[ -f "$sk" ]] || die "service $svc: ssh_public_keys must be a readable host file (got: $sk)"
  case "$(jq -r '.guest_ports | if . == null then "ok" elif type == "array" then "ok" else "bad" end' <<<"$json")" in
    bad) die "service $svc: guest_ports must be a YAML list" ;;
  esac
  case "$(jq -r '.about | if . == null then "ok" elif type == "string" then "ok" else "bad" end' <<<"$json")" in
    bad) die "service $svc: about must be a string if set" ;;
  esac
}

validate_service_refresh() {
  local json="$1" svc="$2"
  local spec
  spec="$(vmid_spec_from_json "$json")"
  [[ "$spec" != next ]] || die "service $svc: refresh needs a numeric vmid (still 'next' — run apply first or set vmid)"
  validate_service "$json" "$svc"
}

# Rewrite only the first `vmid:` line under services.<svc> (preserves comments, blank lines, order).
# Requires: a `services:` block, a direct child key matching <svc>, and a `vmid:` line indented deeper than that key.
compose_write_service_vmid() {
  local path="$1" svc="$2" vmid="$3"
  local tmp ec
  [[ -f "$path" ]] || die "compose file not found: $path"
  [[ "$vmid" =~ ^[0-9]+$ ]] || die "internal: vmid must be numeric for writeback ($vmid)"
  [[ "$svc" =~ ^[a-zA-Z0-9_-]+$ ]] \
    || die "pve-oci-compose: service '$svc' — vmid writeback only supports names matching ^[a-zA-Z0-9_-]+\$ (use an unquoted mapping key under services:)."

  tmp="$(mktemp "${TMPDIR:-/tmp}/pve-oci-compose.XXXXXX")" || die "mktemp failed"
  ec=0
  awk -v svc="$svc" -v newvm="$vmid" '
    function leadlen(s,    t) {
      if (match(s, /^[[:space:]]*/))
        return RLENGTH
      return 0
    }
    BEGIN { in_services = 0; in_target = 0; replaced = 0; found = 0; sind = -1 }
    /^[[:space:]]*services:[[:space:]]*(#|$)/ {
      in_services = 1
      print
      next
    }
    !in_services {
      print
      next
    }
    {
      l = leadlen($0)
      if (in_target) {
        if ($0 ~ /^[[:space:]]*$/ || $0 ~ /^[[:space:]]*#/) {
          print
          next
        }
        if (l > sind && match($0, /^[[:space:]]+vmid[[:space:]]*:/)) {
          pre = substr($0, 1, RLENGTH)
          rest = substr($0, RLENGTH + 1)
          sub(/^[[:space:]]+/, "", rest)
          nc = index(rest, "#")
          if (nc > 0) {
            cmt = substr(rest, nc)
            print pre " " newvm " " cmt
          } else {
            print pre " " newvm
          }
          replaced = 1
          next
        }
        if (l == sind) {
          if (!replaced) {
            print "pve-oci-compose: services[\"" svc "\"] has no vmid: line under that service; add e.g.  vmid: next" > "/dev/stderr"
            exit 2
          }
          in_target = 0
          print
          next
        }
        if (l < sind) {
          if (!replaced) {
            print "pve-oci-compose: services[\"" svc "\"] has no vmid: line under that service" > "/dev/stderr"
            exit 2
          }
          in_target = 0
          print
          next
        }
        print
        next
      }
      if ($0 ~ "^[[:space:]]*" svc ":[[:space:]]*($|#)") {
        found = 1
        in_target = 1
        sind = l
        print
        next
      }
      print
      next
    }
    END {
      if (!found) {
        print "pve-oci-compose: no services." svc " in compose (expected a mapping key under services:)" > "/dev/stderr"
        exit 3
      }
      if (in_target && !replaced) {
        print "pve-oci-compose: services[\"" svc "\"] has no vmid: line to update; add e.g.  vmid: next" > "/dev/stderr"
        exit 2
      }
    }
  ' "$path" >"$tmp" || ec=$?
  if [[ "$ec" -ne 0 ]]; then
    rm -f "$tmp"
    die "pve-oci-compose: failed to update vmid in $path (awk exit $ec)"
  fi
  chmod --reference="$path" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$path" || {
    rm -f "$tmp"
    die "pve-oci-compose: mv failed writing $path"
  }
  echo "pve-oci-compose: updated vmid for service '$svc' → $vmid in $path"
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
  local json sname svc spec vmid image rootfs tags stored exists hint mr stack effpool sa desc_preview
  json="$(compose_json)"
  stack="$(jq -r '.project // empty' <<<"$json")"
  sa="$(jq -r 'if (.stack_about | type) == "string" then .stack_about else "" end' <<<"$json")"
  echo "Compose file: $COMPOSE_FILE"
  jq -r '.project // empty' <<<"$json" | sed '/^$/d' | sed 's/^/Project: /' || true
  echo

  while IFS= read -r sname; do
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service "$svc" "$sname"
    spec="$(vmid_spec_from_json "$svc")"
    image="$(service_image "$svc")"
    rootfs="$(jq -r '.rootfs' <<<"$svc")"
    if [[ "$spec" == next ]]; then
      hint="$(pve_oci_next_cluster_id 2>/dev/null)" || hint="(query failed — need pvesh on a PVE node)"
      vmid="next (next free now: $hint)"
      echo "=== service: $sname (vmid $vmid) ==="
      echo "  image (file):  $image"
      echo "  rootfs:        $rootfs"
      echo "  plan apply:    would allocate next free vmid and create from $image"
      echo "  plan refresh:  n/a until vmid is fixed in the file (run apply to write it)"
      desc_preview="$(printf '%s' "$svc" | pve_oci_compose_pct_description "$stack" "$sa" "$(jq -c '.repo' <<<"$json")" 2>/dev/null || true)"
      if [[ -n "$desc_preview" ]]; then
        echo "  pct description (preview):"
        printf '%s\n' "$desc_preview" | sed 's/^/    /'
      fi
      echo
      continue
    fi

    vmid="$spec"
    mr="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"
    effpool="$(pve_oci_effective_pool_for_service "$svc" "$stack")"
    tags="$(pve_oci_pct_tags "$vmid")"
    stored="$(pve_oci_stored_ref "$vmid")"
    if pct config "$vmid" &>/dev/null; then
      exists=yes
    else
      exists=no
    fi

    echo "=== service: $sname (vmid $vmid) ==="
    echo "  exists:        $exists"
    echo "  image (file):  $image"
    echo "  rootfs:        $rootfs"
    ns_plan="$(jq -r '
      (.nameserver // empty)
      | if type == "array" then map(tostring) | join(", ")
        elif type == "number" then tostring
        elif type == "string" then .
        else empty end
    ' <<<"$svc")"
    sd_plan="$(jq -r '
      (.searchdomain // empty)
      | if type == "array" then map(tostring) | join(" ")
        elif type == "number" then tostring
        elif type == "string" then .
        else empty end
    ' <<<"$svc")"
    [[ -n "$ns_plan" ]] && echo "  nameserver:    $ns_plan"
    [[ -n "$sd_plan" ]] && echo "  searchdomain:  $sd_plan"
    ep_plan="$(jq -r 'if (.entrypoint | type) == "string" then .entrypoint else empty end' <<<"$svc")"
    [[ -n "$ep_plan" ]] && echo "  entrypoint:    $ep_plan"
    sw_plan="$(jq -r '.swap // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svc")"
    [[ -n "$sw_plan" ]] && echo "  swap (MB):     $sw_plan"
    echo "  marker (path): ${mr}"
    echo "  pool (target): ${effpool:-<none>}"
    echo "  tags:          ${tags:-<none>}"
    desc_preview="$(printf '%s' "$svc" | pve_oci_compose_pct_description "$stack" "$sa" "$(jq -c '.repo' <<<"$json")" 2>/dev/null || true)"
    if [[ -n "$desc_preview" ]]; then
      echo "  pct description (preview):"
      printf '%s\n' "$desc_preview" | sed 's/^/    /'
    fi
    if [[ "$exists" == yes ]]; then
      if [[ -z "$stored" ]]; then
        echo "  plan apply:    no-op (already exists)"
        echo "  plan refresh:  blocked (guest marker unreadable — use --adopt if intentional)"
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
  local resolved_vmid="$2"
  local stack_default="${3:-}"
  local stack_about="${4:-}"
  local compose_repo_json="${5:-null}"
  local ts ref vmid hostname node mem cores ostype arch feats v pool nk nv j desc_built

  OCI_CREATE_ARGS=()
  ts="$(jq -r '.template_storage // .storage // empty' <<<"$svcjson")"
  ref="$(service_image "$svcjson")"
  vmid="$resolved_vmid"
  hostname="$(jq -r '.hostname // empty' <<<"$svcjson")"
  node="$(jq -r '.node // empty' <<<"$svcjson")"
  mem="$(jq -r '.memory // empty' <<<"$svcjson")"
  cores="$(jq -r '.cores // empty' <<<"$svcjson")"
  ostype="$(jq -r '.ostype // empty' <<<"$svcjson")"
  arch="$(jq -r '.arch // empty' <<<"$svcjson")"
  feats="$(jq -r '.features // empty' <<<"$svcjson")"

  [[ -n "$ref" ]] || die "internal: empty image"
  pool="$(pve_oci_effective_pool_for_service "$svcjson" "$stack_default")"
  OCI_CREATE_ARGS+=(--reference "$ref" --rootfs "$(jq -r '.rootfs' <<<"$svcjson")" --vmid "$vmid")
  [[ -n "$ts" ]] && OCI_CREATE_ARGS+=(--storage "$ts")
  [[ -n "$pool" ]] && OCI_CREATE_ARGS+=(--pool "$pool")
  [[ -n "$hostname" ]] && OCI_CREATE_ARGS+=(--hostname "$hostname")

  while IFS= read -r nk; do
    [[ -z "$nk" ]] && continue
    nv="$(jq -r --arg k "$nk" '.[$k] | tostring' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--"$nk" "$nv")
  done < <(jq -r '
    to_entries
    | map(select(.key | test("^net[0-9]+$")))
    | sort_by(.key | ltrimstr("net") | tonumber)
    | .[].key
  ' <<<"$svcjson")

  v="$(jq -r '.entrypoint // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--entrypoint "$v")
  while IFS= read -r ev; do
    [[ -z "$ev" ]] && continue
    OCI_CREATE_ARGS+=(--env "$ev")
  done < <(jq -r '
    (.env // empty)
    | if type == "object" then to_entries[] | "\(.key)=\(.value | tostring)"
      elif type == "array" then .[] | tostring
      elif type == "string" then .
      else empty end
    ' <<<"$svcjson")

  while IFS= read -r ns; do
    [[ -z "$ns" ]] && continue
    OCI_CREATE_ARGS+=(--nameserver "$ns")
  done < <(jq -r '
    (.nameserver // empty)
    | if type == "array" then .[] | tostring
      elif type == "number" then tostring
      elif type == "string" then
        [ splits("[,[:space:]]+") ] | map(select(length > 0)) | .[]
      else empty end
    ' <<<"$svcjson")
  sd="$(jq -r '
    (.searchdomain // empty)
    | if type == "array" then map(tostring) | join(" ")
      elif type == "number" then tostring
      elif type == "string" then .
      else empty end
    ' <<<"$svcjson")"
  [[ -n "$sd" ]] && OCI_CREATE_ARGS+=(--searchdomain "$sd")
  [[ -n "$node" ]] && OCI_CREATE_ARGS+=(--node "$node")
  [[ -n "$mem" ]] && OCI_CREATE_ARGS+=(--memory "$mem")
  [[ -n "$cores" ]] && OCI_CREATE_ARGS+=(--cores "$cores")
  [[ -n "$ostype" ]] && OCI_CREATE_ARGS+=(--ostype "$ostype")
  [[ -n "$arch" ]] && OCI_CREATE_ARGS+=(--arch "$arch")
  [[ -n "$feats" ]] && OCI_CREATE_ARGS+=(--features "$feats")

  v="$(jq -r '.swap // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--swap "$v")
  v="$(jq -r '.cpulimit // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--cpulimit "$v")
  v="$(jq -r '.cpuunits // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--cpuunits "$v")
  desc_built="$(printf '%s' "$svcjson" | pve_oci_compose_pct_description "${stack_default:-}" "${stack_about:-}" "${compose_repo_json}")"
  [[ -n "$desc_built" ]] && OCI_CREATE_ARGS+=(--description "$desc_built")
  v="$(jq -r '.tags // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--tags "$v")
  v="$(jq -r '.timezone // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--timezone "$v")
  v="$(jq -r '.password // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--password "$v")
  v="$(jq -r '.ssh_public_keys // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--ssh-public-keys "$v")
  v="$(jq -r '.startup // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--startup "$v")
  v="$(jq -r '.hookscript // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--hookscript "$v")
  v="$(jq -r '.cmode // empty | if type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--cmode "$v")
  v="$(jq -r '.console // empty | if type == "boolean" then (if . then "1" else "0" end) elif type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--console "$v")
  v="$(jq -r '.tty // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--tty "$v")
  v="$(jq -r '.bwlimit // empty | if type == "number" then tostring elif type == "string" then . else empty end' <<<"$svcjson")"
  [[ -n "$v" ]] && OCI_CREATE_ARGS+=(--bwlimit "$v")

  j=0
  while IFS= read -r ds; do
    [[ -z "$ds" ]] && continue
    OCI_CREATE_ARGS+=(--"dev${j}" "$ds")
    j=$((j + 1))
  done < <(jq -r '.lxc_dev[]? | strings' <<<"$svcjson")
  j=0
  while IFS= read -r us; do
    [[ -z "$us" ]] && continue
    OCI_CREATE_ARGS+=(--"unused${j}" "$us")
    j=$((j + 1))
  done < <(jq -r '.unused_disks[]? | strings' <<<"$svcjson")

  if jq -e '.onboot != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.onboot | type) == "boolean" then (if .onboot then "1" else "0" end) else "\(.onboot)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--onboot "$v")
  fi
  if jq -e '.unprivileged != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.unprivileged | type) == "boolean" then (if .unprivileged then "1" else "0" end) else "\(.unprivileged)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--unprivileged "$v")
  fi
  if jq -e '.start != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.start | type) == "boolean" then (if .start then "1" else "0" end) else "\(.start)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--start "$v")
  fi
  if jq -e '.protection != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.protection | type) == "boolean" then (if .protection then "1" else "0" end) else "\(.protection)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--protection "$v")
  fi
  if jq -e '.ha_managed != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.ha_managed | type) == "boolean" then (if .ha_managed then "1" else "0" end) else "\(.ha_managed)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--ha-managed "$v")
  fi
  if jq -e '.ignore_unpack_errors != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.ignore_unpack_errors | type) == "boolean" then (if .ignore_unpack_errors then "1" else "0" end) else "\(.ignore_unpack_errors)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--ignore-unpack-errors "$v")
  fi
  if jq -e '.pct_debug != null' <<<"$svcjson" >/dev/null 2>&1; then
    v="$(jq -r 'if (.pct_debug | type) == "boolean" then (if .pct_debug then "1" else "0" end) else "\(.pct_debug)" end' <<<"$svcjson")"
    OCI_CREATE_ARGS+=(--debug "$v")
  fi

  while IFS= read -r mp; do
    [[ -z "$mp" ]] && continue
    OCI_CREATE_ARGS+=(--mp "$mp")
  done < <(jq -r '.mounts[]? | strings' <<<"$svcjson")
}

OCI_CREATE_ARGS=()

cmd_apply() {
  local json sname svc spec resolved image merged stack effpool sa rj
  json="$(compose_json)"
  stack="$(jq -r '.project // empty' <<<"$json")"
  sa="$(jq -r 'if (.stack_about | type) == "string" then .stack_about else "" end' <<<"$json")"
  rj="$(jq -c '.repo' <<<"$json")"
  while IFS= read -r sname; do
    unset PVE_OCI_POOL_JUST_AUTOCREATED 2>/dev/null || true
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service "$svc" "$sname"
    spec="$(vmid_spec_from_json "$svc")"
    image="$(service_image "$svc")"
    if [[ "$spec" == next ]]; then
      resolved="$(pve_oci_next_cluster_id)" || die "apply: [$sname] could not read cluster nextid (pvesh / jq?)"
    else
      resolved="$spec"
    fi

    if pct config "$resolved" &>/dev/null; then
      echo "apply: [$sname] vmid $resolved already exists — skip create"
      effpool="$(pve_oci_effective_pool_for_service "$svc" "$stack")"
      if [[ -n "$effpool" && "$DRY_RUN" -eq 0 ]]; then
        pve_oci_pool_ensure_lxc_member "$effpool" "$resolved" \
          || die "apply: [$sname] could not add CT $resolved to pool '$effpool'"
        echo "apply: [$sname] pool membership → ${effpool}"
      elif [[ -n "$effpool" && "$DRY_RUN" -eq 1 ]]; then
        echo "apply: [$sname] DRY-RUN: would ensure CT $resolved in pool '${effpool}' (pvesh set /pools/…)"
      fi
      continue
    fi

    echo "apply: [$sname] creating vmid $resolved from $image"
    effpool="$(pve_oci_effective_pool_for_service "$svc" "$stack")"
    if [[ -n "$effpool" && "$DRY_RUN" -eq 0 ]]; then
      pve_oci_pool_ensure_exists "$effpool" \
        || die "apply: [$sname] could not ensure resource pool '$effpool' exists (required before pct create --pool)"
    elif [[ -n "$effpool" && "$DRY_RUN" -eq 1 ]]; then
      echo "apply: [$sname] DRY-RUN: would ensure pool '$effpool' exists before pct create --pool"
    fi
    fill_create_args "$svc" "$resolved" "$stack" "$sa" "$rj"
    run_or_print oci_create_main "${OCI_CREATE_ARGS[@]}"

    if [[ "$DRY_RUN" -eq 1 ]]; then
      merged="$(pve_oci_tags_merge_sentinel_only "$(pve_oci_pct_tags "$resolved")")"
      printf 'DRY-RUN:'
      printf ' %q' pct set "$resolved" --tags "$merged"
      printf '\n'
      echo "apply: [$sname] would write guest ${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json} (pct mount briefly)"
    else
      pve_oci_set_managed_marker "$resolved" "$sname" "$image"
      echo "apply: [$sname] sentinel tag + guest marker file (${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json})"
      if [[ -n "$effpool" ]]; then
        if [[ "$DRY_RUN" -eq 0 ]]; then
          pve_oci_pool_ensure_lxc_member "$effpool" "$resolved" \
            || die "apply: [$sname] could not add CT $resolved to pool '$effpool'"
          echo "apply: [$sname] pool membership → ${effpool}"
        else
          echo "apply: [$sname] DRY-RUN: would pass --pool '${effpool}' to pct create (and pvesh would no-op if already a member)"
        fi
      fi
      if [[ "$spec" == next && "$WRITE_COMPOSE_VMID" -eq 1 ]]; then
        compose_write_service_vmid "$COMPOSE_FILE" "$sname" "$resolved"
      elif [[ "$spec" == next && "$WRITE_COMPOSE_VMID" -eq 0 ]]; then
        echo "apply: [$sname] vmid was 'next' — set vmid: $resolved in $COMPOSE_FILE (or re-run apply without --no-write-compose)"
      fi
    fi
  done < <(jq -r '.services | keys[]' <<<"$json")
}

cmd_refresh() {
  local json sname svc vmid image merged stored ts stack effpool
  json="$(compose_json)"
  stack="$(jq -r '.project // empty' <<<"$json")"
  while IFS= read -r sname; do
    unset PVE_OCI_POOL_JUST_AUTOCREATED 2>/dev/null || true
    svc="$(jq -c --arg n "$sname" '.services[$n]' <<<"$json")"
    validate_service_refresh "$svc" "$sname"
    vmid="$(vmid_spec_from_json "$svc")"
    image="$(service_image "$svc")"
    unset OCI_REFRESH_TEMPLATE_STORAGE || true
    ts="$(jq -r '.template_storage // .storage // empty' <<<"$svc")"
    [[ -n "$ts" ]] && export OCI_REFRESH_TEMPLATE_STORAGE="$ts"
    unset PVE_OCI_COMPOSE_SERVICE || true
    unset PVE_OCI_COMPOSE_REF || true
    export PVE_OCI_COMPOSE_SERVICE="$sname"
    export PVE_OCI_COMPOSE_REF="$image"

    pct config "$vmid" &>/dev/null || die "refresh: [$sname] vmid $vmid does not exist"

    stored="$(pve_oci_stored_ref "$vmid")"

    if [[ -z "$stored" ]]; then
      if [[ "$ADOPT" -ne 1 ]]; then
        echo "refresh: [$sname] CT $vmid has no readable guest marker — skip (use --adopt for unmanaged CTs)"
        continue
      fi
      echo "refresh: [$sname] --adopt: treating unmanaged CT $vmid as $sname"
    fi

    if [[ "$FORCE_REFRESH" -eq 0 && -n "$stored" && "$stored" == "$image" ]]; then
      echo "refresh: [$sname] image unchanged — skip"
      continue
    fi

    echo "refresh: [$sname] vmid $vmid → $image"
    run_or_print oci_refresh_main "$vmid" "$image"

    if [[ "$DRY_RUN" -eq 1 ]]; then
      merged="$(pve_oci_tags_merge_sentinel_only "$(pve_oci_pct_tags "$vmid")")"
      printf 'DRY-RUN:'
      printf ' %q' pct set "$vmid" --tags "$merged"
      printf '\n'
      effpool="$(pve_oci_effective_pool_for_service "$svc" "$stack")"
      [[ -n "$effpool" ]] && echo "refresh: [$sname] DRY-RUN: would ensure pool '${effpool}' for CT ${vmid}"
      echo "refresh: [$sname] DRY-RUN: guest marker JSON not written; tag merge shown above only"
    else
      pve_oci_set_managed_marker "$vmid" "$sname" "$image"
      effpool="$(pve_oci_effective_pool_for_service "$svc" "$stack")"
      if [[ -n "$effpool" ]]; then
        pve_oci_pool_ensure_lxc_member "$effpool" "$vmid" \
          || die "refresh: [$sname] could not add CT $vmid to pool '$effpool'"
        echo "refresh: [$sname] pool membership → ${effpool}"
      fi
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
    run_or_print oci_create_main "${pull_args[@]}"
  done < <(jq -r '.services | keys[]' <<<"$json")
}

CMD=""
[[ "${PVE_OCI_COMPOSE_NO_WRITE:-}" == 1 ]] && WRITE_COMPOSE_VMID=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file)       COMPOSE_FILE="${2:?}"; shift 2 ;;
    --adopt)         ADOPT=1; shift ;;
    --force)         FORCE_REFRESH=1; shift ;;
    -n|--dry-run)    DRY_RUN=1; shift ;;
    --no-write-compose) WRITE_COMPOSE_VMID=0; shift ;;
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
