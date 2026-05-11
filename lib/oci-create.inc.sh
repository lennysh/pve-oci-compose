# shellcheck shell=bash
# oci-create.inc.sh — OCI vztmpl pull + pct create (inlined from oci-ct-create-from-registry.sh)
# Sourced by pve-oci-compose.sh. Defines oci_create_main "$@".
# Uses die() from the caller (pve-oci-compose.sh) once that script has defined it.

oci_create_usage() {
  cat <<'EOF'
Usage: (internal) oci create [options]

Required (omit when using --list-template-storages):
  --storage ID          Optional. Datacenter → Storage id with **vztmpl** (dir/nfs/cifs).
                        If omitted: auto-picks when exactly one candidate; else lists and exits 2.
  --reference REF       OCI image reference (e.g. docker.io/library/nginx:latest)
  --rootfs SPEC         New CT root disk: STORAGE:GiB_integer (e.g. Storage:8)

Common options:
  --vmid ID             CT VMID (default: cluster next free)
  --hostname NAME       pct --hostname (default: oci-ct-<vmid>)
  --netN SPEC           pct --netN for N=0.. (default net0: OCI_CT_CREATE_NET0 or name=eth0,bridge=vmbr0,ip=dhcp)
  --nameserver ADDR     pct --nameserver (repeatable; omit with searchdomain to inherit host resolvers)
  --searchdomain LIST   pct --searchdomain (space-separated domains if multiple)
  --node NAME           PVE node name (default: pvecm nodename / hostname -s)
  --memory MB           pct --memory
  --swap MB             pct --swap
  --cores N             pct --cores
  --cpulimit N          pct --cpulimit (0 = unlimited)
  --cpuunits N          pct --cpuunits
  --ostype TYPE         pct --ostype
  --unprivileged 0|1    pct --unprivileged (default: 1)
  --features SPEC       pct --features
  --onboot 0|1          pct --onboot (default: 0)
  --arch ARCH           pct --arch (optional; e.g. amd64)
  --pool ID             pct --pool (Datacenter resource pool; must exist)
  --mp SPEC             Repeatable: STORAGE:GiB:/path
  --entrypoint CMD      pct --entrypoint (OCI / init override)
  --env KEY=val         pct --env (repeatable; same form as pct(1))
  --description TEXT    pct --description
  --tags TAGS           pct --tags (semicolon-separated; apply still merges pve-oci-compose sentinel after create)
  --timezone SPEC       pct --timezone (e.g. host or Europe/Berlin)
  --password PASS       pct --password (avoid secrets in shell history; prefer pct set later)
  --ssh-public-keys PATH  pct --ssh-public-keys (host path to authorized_keys file)
  --start 0|1           pct --start (start CT when create finishes)
  --startup SPEC        pct --startup (order=,up=,down=)
  --hookscript PATH     pct --hookscript (e.g. local:snippets/hook.sh)
  --protection 0|1      pct --protection
  --ha-managed 0|1      pct --ha-managed
  --cmode MODE          pct --cmode (tty|console|shell)
  --console 0|1         pct --console
  --tty N               pct --tty
  --ignore-unpack-errors 0|1  pct --ignore-unpack-errors
  --debug 0|1           pct --debug
  --bwlimit N           pct --bwlimit (KiB/s)
  --devN SPEC           pct --devN (host device pass-through; see pct(1))
  --unusedN SPEC        pct --unusedN (advanced; see pct(1))

Pull behaviour:
  --skip-pull               Do not call oci-registry-pull
  --reuse-local-template    Reuse existing normalized .tar if present
  --list-template-storages  List vztmpl storages; exits 0.
  --pull-only               Download template only (no pct create)

Floating :latest: resolved via skopeo unless OCI_CT_CREATE_NO_RESOLVE_LATEST=1

After create: CT is left stopped unless you run pct start manually.

Env (optional):
  PVE_OCI_CREATE_QUIET=1   Fewer banners when this worker is reused for a disposable
                           scratch CT (e.g. refresh); normal apply output unchanged if unset.

After a successful pull path, exports PVE_OCI_LAST_PULL_REFERENCE (resolved ref, e.g. digest or
non-latest tag) and PVE_OCI_LAST_REFERENCE_INPUT (compose/CLI ref) for pve-oci-compose to refresh
the composed CT description.
EOF
  exit 1
}

# After a failed pct create: drop partial CT, remove empty pool only if this run auto-created it
# (see PVE_OCI_POOL_JUST_AUTOCREATED in lib/common.inc.sh), and hint on unsupported OCI media types.
oci_create_pct_failure_cleanup() {
  local vmid="${1:?}" pool="${2:-}" errfile="${3:-}"
  local blob
  [[ -f "$errfile" ]] && blob="$(cat "$errfile" 2>/dev/null)" || blob=""

  if pct config "$vmid" &>/dev/null; then
    echo "pve-oci-compose: removing partial CT ${vmid} after failed pct create …" >&2
    pct destroy "$vmid" --purge 2>/dev/null || pct destroy "$vmid" 2>/dev/null || true
  fi

  if [[ -n "${PVE_OCI_POOL_JUST_AUTOCREATED:-}" && -n "$pool" \
    && "${PVE_OCI_POOL_JUST_AUTOCREATED}" == "$pool" ]]; then
    if pvesh delete /pools --poolid "$pool" 2>/dev/null || pvesh delete "/pools/${pool}" 2>/dev/null; then
      echo "pve-oci-compose: removed auto-created empty resource pool '${pool}' (pct create failed)." >&2
    else
      echo "pve-oci-compose: auto-created pool '${pool}' was left in place (not empty or delete denied)." >&2
    fi
  fi
  unset PVE_OCI_POOL_JUST_AUTOCREATED 2>/dev/null || true

  if [[ "$blob" == *'Wrong media type'* || "$blob" == *'Error while parsing OCI image'* ]]; then
    echo "pve-oci-compose: Proxmox rejected this OCI archive (unsupported manifest/layer media types for LXC vztmpl)." >&2
    echo "pve-oci-compose: Use an image format Proxmox supports as an OCI template, or a classic vztmpl tarball — not every container registry image is compatible." >&2
  fi
}

oci_create_main() {
  # Consumed by pve-oci-compose after create/refresh to refresh CT description (resolved ref + time).
  unset PVE_OCI_LAST_PULL_REFERENCE PVE_OCI_LAST_REFERENCE_INPUT 2>/dev/null || true

node_name() {
  if command -v pvecm &>/dev/null; then
    local n
    n="$(pvecm nodename 2>/dev/null)" || true
    [[ -n "$n" ]] && { printf '%s\n' "$n"; return; }
  fi
  hostname -s
}

list_template_storages() {
  local json
  if [[ -n "${STORAGE_JSON_CACHED:-}" ]]; then
    json="$STORAGE_JSON_CACHED"
  else
    json=$(pvesh get "/nodes/${NODE}/storage" --output-format json 2>/dev/null) || die "pvesh get /nodes/${NODE}/storage failed"
  fi
  if ! printf '%s\n' "$json" | jq -e . >/dev/null 2>&1; then
    echo "Storage list response was not valid JSON (first 400 chars):" >&2
    printf '%s\n' "$json" | head -c 400 >&2
    echo >&2
    die "Cannot parse /nodes/${NODE}/storage output."
  fi
  cat <<EOF
Storages on node '${NODE}' whose **content** includes **vztmpl** (Container template — same as Datacenter → Storage in the UI):

  Use the **storage id** printed below as --storage. That is the same id the UI uses when it stores
  an OCI image under your Container templates path.  oci-registry-pull **yes** means Proxmox can
  write the .tar there (dir / nfs / cifs — same API as the UI).  **no** means pick another id (e.g. your zfspool cannot host OCI pulls).

EOF
  printf '%s\n' "$json" | jq -r '
    def unwrap: if type == "string" and test("^\\s*\\{") then fromjson else . end;
    def pve_array:
      if type == "array" then .
      elif type == "object" and (.data != null) then
        (.data | if type == "array" then . else [.] end)
      else [] end;
    def has_vztmpl: (.content // "") | test("vztmpl");
    unwrap | pve_array
    | map(select(has_vztmpl))
    | sort_by(.storage // .storeid // .id)
    | .[]
    | (.storage // .storeid // .id) as $id
    | (.type // "?") as $t
    | (
        if (.path // "") != "" then .path
        elif ((.server // .address // "") | length) > 0 then
          "nfs " + (.server // .address) + " " + (.export // .exportpath // "")
        else "—"
        end
      ) as $hint
    | (if ($t == "dir" or $t == "nfs" or $t == "cifs") then "yes" else "no" end) as $ok
    | "storage id: \($id)\n  type: \($t)\n  path / export: \($hint)\n  oci-registry-pull (same rule as UI): \($ok)\n"
  '
}

STORAGE=""
REFERENCE=""
ROOTFS_SPEC=""
VMID=""
HOSTNAME=""
NODE=""
MEMORY=""
SWAP=""
CORES=""
CPULIMIT=""
CPUUNITS=""
OSTYPE=""
ARCH=""
UNPRIV="1"
FEATURES=""
ONBOOT="0"
POOL=""
NAMESERVERS=()
SEARCHDOMAIN=""
ENTRYPOINT=""
ENVS=()
DESCRIPTION=""
TAGS_CREATE=""
TIMEZONE=""
PASSWORD=""
SSH_PUBLIC_KEYS=""
START=""
STARTUP=""
HOOKSCRIPT=""
PROTECTION=""
HA_MANAGED=""
CMODE=""
CONSOLE=""
TTY=""
IGNORE_UNPACK=""
DEBUG_CREATE=""
BWLIMIT=""
SKIP_PULL=0
REUSE_LOCAL=0
PULL_ONLY=0
LIST_TEMPLATE_STORAGES=0
STORAGE_JSON_CACHED=""
MP_SPECS=()

declare -A NET_IFACE=()
declare -A DEV_MAP=()
declare -A UNUSED_MAP=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --storage)          STORAGE="${2:?}"; shift 2 ;;
    --reference)        REFERENCE="${2:?}"; shift 2 ;;
    --rootfs)           ROOTFS_SPEC="${2:?}"; shift 2 ;;
    --vmid)             VMID="${2:?}"; shift 2 ;;
    --hostname)         HOSTNAME="${2:?}"; shift 2 ;;
    --net*)
      if [[ "$1" =~ ^--net([0-9]+)$ ]]; then
        NET_IFACE["${BASH_REMATCH[1]}"]="${2:?}"
        shift 2
      else
        die "Unknown option: $1 (expected --net0, --net1, …)"
      fi
      ;;
    --nameserver)       NAMESERVERS+=("${2:?}"); shift 2 ;;
    --searchdomain)     SEARCHDOMAIN="${2:?}"; shift 2 ;;
    --node)             NODE="${2:?}"; shift 2 ;;
    --memory)           MEMORY="${2:?}"; shift 2 ;;
    --swap)             SWAP="${2:?}"; shift 2 ;;
    --cores)            CORES="${2:?}"; shift 2 ;;
    --cpulimit)         CPULIMIT="${2:?}"; shift 2 ;;
    --cpuunits)         CPUUNITS="${2:?}"; shift 2 ;;
    --ostype)           OSTYPE="${2:?}"; shift 2 ;;
    --arch)             ARCH="${2:?}"; shift 2 ;;
    --unprivileged)     UNPRIV="${2:?}"; shift 2 ;;
    --features)         FEATURES="${2:?}"; shift 2 ;;
    --onboot)           ONBOOT="${2:?}"; shift 2 ;;
    --pool)             POOL="${2:?}"; shift 2 ;;
    --mp)               MP_SPECS+=("${2:?}"); shift 2 ;;
    --entrypoint)       ENTRYPOINT="${2:?}"; shift 2 ;;
    --env)              ENVS+=("${2:?}"); shift 2 ;;
    --description)      DESCRIPTION="${2:?}"; shift 2 ;;
    --tags)             TAGS_CREATE="${2:?}"; shift 2 ;;
    --timezone)         TIMEZONE="${2:?}"; shift 2 ;;
    --password)         PASSWORD="${2:?}"; shift 2 ;;
    --ssh-public-keys)  SSH_PUBLIC_KEYS="${2:?}"; shift 2 ;;
    --start)            START="${2:?}"; shift 2 ;;
    --startup)          STARTUP="${2:?}"; shift 2 ;;
    --hookscript)       HOOKSCRIPT="${2:?}"; shift 2 ;;
    --protection)       PROTECTION="${2:?}"; shift 2 ;;
    --ha-managed)       HA_MANAGED="${2:?}"; shift 2 ;;
    --cmode)            CMODE="${2:?}"; shift 2 ;;
    --console)          CONSOLE="${2:?}"; shift 2 ;;
    --tty)              TTY="${2:?}"; shift 2 ;;
    --ignore-unpack-errors) IGNORE_UNPACK="${2:?}"; shift 2 ;;
    --debug)            DEBUG_CREATE="${2:?}"; shift 2 ;;
    --bwlimit)          BWLIMIT="${2:?}"; shift 2 ;;
    --dev*)
      if [[ "$1" =~ ^--dev([0-9]+)$ ]]; then
        DEV_MAP["${BASH_REMATCH[1]}"]="${2:?}"
        shift 2
      else
        die "Unknown option: $1 (expected --dev0, --dev1, …)"
      fi
      ;;
    --unused*)
      if [[ "$1" =~ ^--unused([0-9]+)$ ]]; then
        UNUSED_MAP["${BASH_REMATCH[1]}"]="${2:?}"
        shift 2
      else
        die "Unknown option: $1 (expected --unused0, …)"
      fi
      ;;
    --skip-pull)        SKIP_PULL=1; shift ;;
    --reuse-local-template) REUSE_LOCAL=1; shift ;;
    --pull-only)        PULL_ONLY=1; shift ;;
    --list-template-storages) LIST_TEMPLATE_STORAGES=1; shift ;;
    -h|--help)          oci_create_usage ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
done

if [[ -z "${NET_IFACE[0]-}" ]]; then
  NET_IFACE[0]="${OCI_CT_CREATE_NET0:-name=eth0,bridge=vmbr0,ip=dhcp}"
fi

if [[ "$LIST_TEMPLATE_STORAGES" -eq 1 ]]; then
  command -v jq >/dev/null 2>&1 || die "jq is required for --list-template-storages."
  command -v pvesh >/dev/null 2>&1 || die "pvesh not found (run on a Proxmox VE node as root)."
  [[ -n "$NODE" ]] || NODE="$(node_name)"
  list_template_storages
  return 0
fi

[[ -n "$REFERENCE" ]] || die "Missing --reference"
if [[ "$PULL_ONLY" -eq 1 ]]; then
  :
elif [[ -n "$ROOTFS_SPEC" ]]; then
  :
else
  die "Missing --rootfs (required unless --pull-only)"
fi

command -v jq >/dev/null 2>&1 || die "jq is required (for pvesh JSON and task status)."
command -v pvesh >/dev/null 2>&1 || die "pvesh not found (run on a Proxmox VE node as root)."
if [[ "$PULL_ONLY" -eq 0 ]]; then
  command -v pct >/dev/null 2>&1 || die "pct not found (run on a Proxmox VE node as root)."
fi

[[ -n "$NODE" ]] || NODE="$(node_name)"

# Set by refresh when reusing this worker for a disposable temp CT (omit apply-style banners).
_PVE_OCI_CREATE_QUIET=0
case "${PVE_OCI_CREATE_QUIET:-}" in 1|yes|true|TRUE|Y) _PVE_OCI_CREATE_QUIET=1 ;; esac

load_node_storage_json() {
  STORAGE_JSON_CACHED=$(pvesh get "/nodes/${NODE}/storage" --output-format json 2>/dev/null) \
    || die "pvesh get /nodes/${NODE}/storage failed"
  if ! printf '%s\n' "$STORAGE_JSON_CACHED" | jq -e . >/dev/null 2>&1; then
    echo "Storage list was not valid JSON (first 400 chars):" >&2
    printf '%s\n' "$STORAGE_JSON_CACHED" | head -c 400 >&2
    echo >&2
    die "Cannot parse /nodes/${NODE}/storage output."
  fi
}

# True if this node has a storage entry $1 with vztmpl and a type Proxmox allows for oci-registry-pull.
template_storage_valid_for_oci_pull() {
  local sid="$1" json="$STORAGE_JSON_CACHED" n
  [[ -n "$sid" ]] || return 1
  n=$(printf '%s\n' "$json" | jq -r --arg s "$sid" '
    def unwrap: if type == "string" and test("^\\s*\\{") then fromjson else . end;
    def pve_array:
      if type == "array" then .
      elif type == "object" and (.data != null) then
        (.data | if type == "array" then . else [.] end)
      else [] end;
    def has_vztmpl: (.content // "") | test("vztmpl");
    def store_id: .storage // .storeid // .id // "";
    unwrap | pve_array
    | map(select(store_id == $s) | select(has_vztmpl)
        | select((.type // "") == "dir" or (.type // "") == "nfs" or (.type // "") == "cifs"))
    | length
  ')
  [[ "${n:-0}" =~ ^[0-9]+$ && "$n" -gt 0 ]]
}

# One storage id per line: vztmpl + type dir|nfs|cifs on this node (same rule as oci-registry-pull).
oci_pull_template_storage_ids() {
  local json="${STORAGE_JSON_CACHED:-}"
  printf '%s\n' "$json" | jq -r '
    def unwrap: if type == "string" and test("^\\s*\\{") then fromjson else . end;
    def pve_array:
      if type == "array" then .
      elif type == "object" and (.data != null) then
        (.data | if type == "array" then . else [.] end)
      else [] end;
    def has_vztmpl: (.content // "") | test("vztmpl");
    unwrap | pve_array
    | map(select(has_vztmpl)
        | select((.type // "") == "dir" or (.type // "") == "nfs" or (.type // "") == "cifs"))
    | (.[] | (.storage // .storeid // .id) // empty)
  ' | sort -u | sed '/^$/d;/^null$/d'
}

pick_template_storage_or_exit() {
  load_node_storage_json
  STORAGE="${STORAGE#"${STORAGE%%[![:space:]]*}"}"
  STORAGE="${STORAGE%"${STORAGE##*[![:space:]]}"}"
  if [[ -z "$STORAGE" ]]; then
    local -a cands=()
    mapfile -t cands < <(oci_pull_template_storage_ids)
    if [[ "${#cands[@]}" -eq 1 ]]; then
      STORAGE="${cands[0]}"
      if declare -f out_muted &>/dev/null; then out_muted "Auto-selected template storage '${STORAGE}' (only vztmpl+OCI candidate on '${NODE}')."
      else echo "Note: auto-selected --storage '${STORAGE}' …" >&2; fi
    elif [[ "${#cands[@]}" -eq 0 ]]; then
      echo "No storage on node '${NODE}' is usable for oci-registry-pull (need vztmpl + type dir, nfs, or cifs)." >&2
      echo >&2
      list_template_storages
      echo >&2
      echo "Enable **Container template** on a dir/nfs/cifs store, then re-run (or pass --storage explicitly)." >&2
      return 2
    else
      echo "--storage not set; multiple OCI template candidates on node '${NODE}' — pick one:" >&2
      echo >&2
      list_template_storages
      echo >&2
      echo "Re-run with:  --storage <storage id from above where oci-registry-pull is yes>" >&2
      return 2
    fi
  fi
  if ! template_storage_valid_for_oci_pull "$STORAGE"; then
    echo "Invalid --storage '${STORAGE}' for oci-registry-pull (not found on this node, or no vztmpl, or type is not dir/nfs/cifs)." >&2
    echo >&2
    list_template_storages
    echo >&2
    echo "Re-run with:  --storage <storage id from above where oci-registry-pull is yes>" >&2
    return 2
  fi
}

pick_template_storage_or_exit

# Match PVE::Storage::normalize_content_filename (pve-storage) used by oci-registry-pull.
normalize_content_filename() {
  perl -MPVE::Storage -e '
    print PVE::Storage::normalize_content_filename($ARGV[0]) . "\n";
  ' "$1"
}

strip_oci_scheme() {
  local r="$1"
  case "$r" in
    oci://*) r="${r#oci://}" ;;
    docker://*) r="${r#docker://}" ;;
  esac
  printf '%s\n' "$r"
}

REFERENCE="$(strip_oci_scheme "$REFERENCE")"

# True when the ref would produce normalize_content_filename ending in _latest (ambiguous tarball).
is_floating_latest_ref() {
  local ref="$1" norm
  ref="${ref,,}"
  if [[ "$ref" == *:latest ]]; then
    return 0
  fi
  norm="$(normalize_content_filename "$1")"
  [[ "$norm" =~ _latest$ ]]
}

# Resolve :latest / *_latest-style refs to a deterministic pull ref (non-latest tag or name@digest).
resolve_floating_latest_ref() {
  local ref="$1" json json_at name digest picked
  if [[ -n "${OCI_CT_CREATE_NO_RESOLVE_LATEST:-}" ]]; then
    printf '%s\n' "$ref"
    return 0
  fi
  if ! is_floating_latest_ref "$ref"; then
    printf '%s\n' "$ref"
    return 0
  fi
  command -v skopeo >/dev/null 2>&1 || die "skopeo is required to resolve :latest / *_latest refs (or set OCI_CT_CREATE_NO_RESOLVE_LATEST=1)."
  json=$(skopeo inspect "docker://${ref}" 2>/dev/null) || die "skopeo inspect failed for docker://${ref}"
  name=$(printf '%s\n' "$json" | jq -r '.Name // empty')
  digest=$(printf '%s\n' "$json" | jq -r '.Digest // empty')
  [[ -n "$name" && -n "$digest" ]] || die "skopeo inspect returned no Name/Digest for docker://${ref}"

  picked=""
  json_at=$(skopeo inspect "docker://${name}@${digest}" 2>/dev/null) || json_at=""
  if [[ -n "$json_at" ]]; then
    picked=$(printf '%s\n' "$json_at" | jq -r '(.RepoTags // [])[]' \
      | awk 'tolower($0) != "latest"' | sort -V | tail -n1)
  fi

  if [[ -n "$picked" ]]; then
    echo "Resolved floating tag to same-digest ref: ${name}:${picked}" >&2
    printf '%s\n' "${name}:${picked}"
    return 0
  fi

  echo "No non-latest RepoTag for this manifest; using digest ref (direct skopeo copy if the API rejects @sha256)." >&2
  printf '%s\n' "${name}@${digest}"
}

PULL_REFERENCE="$(resolve_floating_latest_ref "$REFERENCE")"
export PVE_OCI_LAST_PULL_REFERENCE="${PULL_REFERENCE}"
export PVE_OCI_LAST_REFERENCE_INPUT="${REFERENCE}"

# Host path under vztmpl exists only for storages with a directory-style layout (e.g. "dir", some NFS).
# ZFS pools, LVM-thin, RBD, etc. return "storage definition has no path" from get_vztmpl_dir — that is normal.
vztmpl_host_dir_for_storage() {
  local sid="$1" out
  out="$(perl -MPVE::Storage -e 'print PVE::Storage::get_vztmpl_dir(PVE::Storage::config(), $ARGV[0]) . "\n"' "$sid" 2>/dev/null)" || return 1
  out="${out//$'\r'/}"
  out="${out//$'\n'/}"
  [[ -n "$out" && -d "$out" ]] || return 1
  printf '%s\n' "$out"
}

# True if vztmpl volume id exists on this storage (works when there is no single host path).
storage_has_ostemplate_volid() {
  local json want="${STORAGE}:vztmpl/${NORM}.tar" n
  json=$(pvesh get "/nodes/${NODE}/storage/${STORAGE}/content" --output-format json 2>/dev/null) || return 1
  printf '%s\n' "$json" | jq -e . >/dev/null 2>&1 || return 1
  n=$(printf '%s\n' "$json" | jq -r --arg v "$want" '
    def unwrap: if type == "string" and test("^\\s*\\{") then fromjson else . end;
    def pve_array:
      if type == "array" then .
      elif type == "object" and (.data != null) then
        (.data | if type == "array" then . else [.] end)
      else [] end;
    try (
      unwrap | pve_array
      | map(select((.volid // "") == $v))
      | length
    ) catch empty
  ' 2>/dev/null) || return 1
  [[ "${n:-0}" =~ ^[0-9]+$ && "$n" -gt 0 ]]
}

wait_until_ostemplate_visible() {
  local i
  if [[ -n "${LOCAL_TAR:-}" && -f "$LOCAL_TAR" ]]; then
    return 0
  fi
  for ((i = 0; i < 20; i++)); do
    storage_has_ostemplate_volid && return 0
    sleep 1
  done
  return 1
}

# Next cluster VMID: pve_oci_next_cluster_id() in lib/common.inc.sh (sourced before this file).

# Strip accidental JSON/string junk from a parsed UPID (pvesh often embeds UPID in quoted JSON).
clean_parsed_upid() {
  local u="${1//$'\r'/}"
  u="${u#\"}"
  u="${u%\"}"
  u="${u%,}"
  printf '%s\n' "$u"
}

# pvesh create sometimes prints a bare UPID line or mixes stderr; avoid jq on non-JSON.
parse_upid_from_create_response() {
  local raw="$1" upid
  # Do not use [^[:space:]]+ here: a closing JSON " is not whitespace and would be captured.
  upid=$(printf '%s\n' "$raw" | grep -oE 'UPID:[^"[:space:]]+' | tail -1)
  if [[ -n "$upid" ]]; then
    clean_parsed_upid "$upid"
    return 0
  fi
  upid=$(printf '%s\n' "$raw" | jq -r '
    try (
      (if type == "string" and test("^\\s*\\{") then fromjson else . end)
      | if type == "object" and (.data != null) then .data else . end
      | if type == "string" then . elif type == "number" then tostring else empty end
    ) catch empty
  ' 2>/dev/null) || true
  if [[ -n "$upid" && "$upid" != "null" ]]; then
    clean_parsed_upid "$upid"
    return 0
  fi
  while IFS= read -r line || [[ -n "${line:-}" ]]; do
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*\{ ]] || continue
    upid=$(printf '%s\n' "$line" | jq -r '
      try (
        (if type == "string" and test("^\\s*\\{") then fromjson else . end)
        | if type == "object" and (.data != null) then .data else . end
        | if type == "string" then . elif type == "number" then tostring else empty end
      ) catch empty
    ' 2>/dev/null) || true
    if [[ -n "$upid" && "$upid" != "null" ]]; then
      clean_parsed_upid "$upid"
      return 0
    fi
  done <<< "$(printf '%s\n' "$raw")"
  return 1
}

wait_for_task() {
  local upid="$1" max="${2:-7200}" waited=0 poll_empty=0
  local status exitstatus line upid_esc

  upid_esc="$(pve_api_quote_path_segment "$upid")"

  while [[ "$waited" -lt "$max" ]]; do
    line=$(pvesh get "/nodes/${NODE}/tasks/${upid_esc}/status" --output-format json 2>/dev/null) || true
    [[ -z "$line" ]] && line=$(pvesh get "/nodes/${NODE}/tasks/${upid}/status" --output-format json 2>/dev/null) || true

    if [[ -n "${PVE_OCI_COMPOSE_TASK_DEBUG:-}" ]]; then
      printf '[task-debug] waited=%ds len=%s first=%s\n' "$waited" "${#line}" "${line:0:120}" >&2
    fi

    if [[ -n "$line" ]]; then
      poll_empty=0
      IFS=$'\t' read -r status exitstatus <<<"$(pve_task_status_from_json "$line")"
      status="${status//$'\r'/}"
      exitstatus="${exitstatus//$'\r'/}"
      if [[ "$status" == "stopped" ]]; then
        if [[ "${exitstatus^^}" == "OK" ]]; then
          return 0
        fi
        die "Task finished with exitstatus=${exitstatus:-unknown}. UPID=${upid}"
      fi
    else
      poll_empty=$((poll_empty + 2))
      if [[ "$poll_empty" -eq 30 ]]; then
        printf '%s\n' "Still waiting on task UPID=${upid} … (no JSON from pvesh get …/tasks/…/status — check node name and API; try PVE_OCI_COMPOSE_TASK_DEBUG=1)" >&2
      fi
    fi
    sleep 2
    waited=$((waited + 2))
  done
  die "Timed out waiting for task ${upid} (${max}s)"
}

# When oci-registry-pull rejects name@sha256 (API regex is :tag-only on some PVE versions), copy to the
# same path the worker would have used so OSTEMPLATE / NORM stay consistent.
skopeo_copy_digest_ref_to_local_tar() {
  local ref="$1"
  [[ -n "${LOCAL_TAR:-}" ]] || die "Internal error: skopeo_copy_digest_ref_to_local_tar without LOCAL_TAR."
  [[ "$ref" == *@sha256:* ]] || die "Internal error: skopeo_copy_digest_ref_to_local_tar expects @sha256 ref."
  local tmp="${LOCAL_TAR}.pulltmp.${BASHPID}"
  rm -f "$tmp"
  skopeo copy "docker://${ref}" "oci-archive:${tmp}"
  mv -f "$tmp" "$LOCAL_TAR"
}

oci_registry_pull() {
  local ref="$1" out upid
  if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
    out_muted "Scratch CT: template pull / reuse on ${STORAGE} (${ref})"
  else
    out_title "Registry pull (vztmpl · oci-registry-pull)"
    out_kv "Node" "${NODE}"
    out_kv "Storage" "${STORAGE}"
    if [[ "${REFERENCE}" != "${PULL_REFERENCE}" ]]; then
      out_kv "Compose / CLI ref" "${REFERENCE}"
      out_kv "Resolved pull ref" "${ref}"
    else
      out_kv "Image ref" "${ref}"
    fi
  fi

  out=$(pvesh create "/nodes/${NODE}/storage/${STORAGE}/oci-registry-pull" \
    --reference "$ref" --output-format json 2>&1) && {
    upid="$(parse_upid_from_create_response "$out" || true)"
    [[ -n "$upid" ]] || die "Could not parse UPID from pvesh output (expected JSON with .data or a UPID: line): $out"
    if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
      out_muted "Waiting on registry pull task …"
    else
      out_kv "Task UPID" "${upid}"
      out_sub "Waiting for pull task …"
    fi
    wait_for_task "$upid"
    [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]] && out_muted "Template on storage" || out_ok "Template on storage"
    return 0
  }

  echo "$out" >&2
  # skopeo refuses to overwrite an existing oci-archive; treat as success if our ostemplate is already there.
  if echo "$out" | grep -qiE 'refusing to override|existing file|file already exists|already exists'; then
    if [[ -n "${LOCAL_TAR:-}" && -f "$LOCAL_TAR" ]] || storage_has_ostemplate_volid; then
      echo "Template ${OSTEMPLATE} already on storage; skipping pull (delete or rename that .tar in vztmpl to force a re-download)." >&2
      echo >&2
      return 0
    fi
  fi

  if [[ "$ref" == *@sha256:* && -n "${LOCAL_TAR:-}" ]]; then
    if echo "$out" | grep -qiE 'parameter verification|invalid|does not match|malformed|bad request'; then
      echo "oci-registry-pull rejected digest-shaped reference; copying with skopeo to ${LOCAL_TAR} ..." >&2
      skopeo_copy_digest_ref_to_local_tar "$ref"
      echo "Direct skopeo copy completed." >&2
      echo >&2
      return 0
    fi
  fi

  if echo "$out" | grep -qiE 'not a file based storage|zfspool'; then
    echo >&2
    echo "oci-registry-pull only supports the same storages the UI can write an OCI .tar to (dir, nfs, cifs, …), not zfspool." >&2
    echo "Pass the Datacenter → Storage **id** where your Container templates path lives (vztmpl on your mount), not your CT disk pool:" >&2
    echo "  $0 --list-template-storages" >&2
      echo "Then e.g.:  --storage <that-id> --reference ${REFERENCE} --rootfs Storage:8 ..." >&2
  elif [[ "$ref" == *@sha256:* ]]; then
    echo "Digest ref pull failed and there is no host vztmpl path for a direct skopeo copy." >&2
    echo "Use an explicit version tag, a registry that lists non-latest tags for this manifest, or template storage with a resolvable directory path." >&2
  else
    echo "Hint: storage '${STORAGE}' must have vztmpl content; skopeo must exist at /usr/bin/skopeo; PVE must expose oci-registry-pull." >&2
  fi
  die "pvesh oci-registry-pull failed."
}

NORM="$(normalize_content_filename "$PULL_REFERENCE")"
OSTEMPLATE="${STORAGE}:vztmpl/${NORM}.tar"
LOCAL_TAR=""
VZTDIR=""
if VZTDIR="$(vztmpl_host_dir_for_storage "$STORAGE")"; then
  LOCAL_TAR="${VZTDIR}/${NORM}.tar"
else
  if declare -f out_detail &>/dev/null; then
    out_detail "No host vztmpl directory for storage '${STORAGE}' (normal for some backends)."
    out_detail "Polling API until volume ${OSTEMPLATE} appears …"
  else
    echo "Note: no host vztmpl path for '${STORAGE}'; polling API for ${OSTEMPLATE}" >&2
  fi
fi

if [[ "$SKIP_PULL" -eq 1 ]]; then
  if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
    out_muted "Pull skipped (--skip-pull) · ${OSTEMPLATE}"
  else
    out_sub "Pull skipped (--skip-pull)"
    out_kv "Ostemplate" "${OSTEMPLATE}"
  fi
elif [[ "$REUSE_LOCAL" -eq 1 ]]; then
  if [[ -n "$LOCAL_TAR" && -f "$LOCAL_TAR" ]]; then
    if declare -f out_muted &>/dev/null; then out_muted "Reusing template on host: ${LOCAL_TAR}"
    else echo "Reusing existing template file: ${LOCAL_TAR}" >&2; fi
  elif storage_has_ostemplate_volid; then
    if declare -f out_muted &>/dev/null; then out_muted "Reusing template on storage (${OSTEMPLATE})"
    else echo "Reusing existing template on storage (${OSTEMPLATE})." >&2; fi
  else
    oci_registry_pull "$PULL_REFERENCE"
  fi
else
  oci_registry_pull "$PULL_REFERENCE"
fi

if [[ -n "$LOCAL_TAR" && -f "$LOCAL_TAR" ]]; then
  if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
    out_muted "Template file: ${LOCAL_TAR}"
  else
    out_kv "Template file" "${LOCAL_TAR}"
    tpl_one="$(ls -lh "$LOCAL_TAR" 2>/dev/null | head -1)"
    [[ -n "$tpl_one" ]] && out_detail "${tpl_one}"
  fi
elif wait_until_ostemplate_visible; then
  [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]] && out_muted "Template: ${OSTEMPLATE}" || out_kv "Template" "${OSTEMPLATE} (listed on storage)"
else
  die "Template not found as ${OSTEMPLATE} (no file at ${LOCAL_TAR:-<no host path>} and storage content listing did not show it)."
fi

if [[ "$PULL_ONLY" -eq 1 ]]; then
  out_ok "Pull-only done"
  return 0
fi
[[ -n "$VMID" ]] || VMID="$(pve_oci_next_cluster_id)" || die "Could not get next cluster VMID (install jq or pass --vmid)"

if pct config "$VMID" &>/dev/null; then
  die "VMID ${VMID} already exists"
fi

[[ -n "$HOSTNAME" ]] || HOSTNAME="oci-ct-${VMID}"

if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
  out_muted "Unpacking vztmpl into scratch VMID ${VMID} (disposable — not your compose CT)"
else
  out_title "pct create · from vztmpl OCI tarball"
  out_kv "VMID" "${VMID}"
  out_kv "Ostemplate" "${OSTEMPLATE}"
  out_kv "Rootfs" "${ROOTFS_SPEC}"
  out_kv "Hostname" "${HOSTNAME}"
  for _n in $(printf '%s\n' "${!NET_IFACE[@]}" | sort -n); do
    out_kv "net${_n}" "${NET_IFACE[$_n]}"
  done
  [[ "${#NAMESERVERS[@]}" -gt 0 ]] && out_kv "nameserver" "${NAMESERVERS[*]}"
  [[ -n "$SEARCHDOMAIN" ]] && out_kv "searchdomain" "${SEARCHDOMAIN}"
  [[ -n "$ENTRYPOINT" ]] && out_kv "entrypoint" "${ENTRYPOINT}"
  [[ "${#ENVS[@]}" -gt 0 ]] && out_kv "env" "${ENVS[*]}"
  [[ -n "$SWAP" ]] && out_kv "swap (MB)" "${SWAP}"
  [[ -n "$CPULIMIT" ]] && out_kv "cpulimit" "${CPULIMIT}"
  [[ -n "$CPUUNITS" ]] && out_kv "cpuunits" "${CPUUNITS}"
  [[ -n "$DESCRIPTION" ]] && out_kv "description" "${DESCRIPTION}"
  [[ -n "$TAGS_CREATE" ]] && out_kv "tags" "${TAGS_CREATE}"
  [[ -n "$TIMEZONE" ]] && out_kv "timezone" "${TIMEZONE}"
  [[ -n "$SSH_PUBLIC_KEYS" ]] && out_kv "ssh-public-keys" "${SSH_PUBLIC_KEYS}"
  [[ -n "$STARTUP" ]] && out_kv "startup" "${STARTUP}"
  [[ -n "$HOOKSCRIPT" ]] && out_kv "hookscript" "${HOOKSCRIPT}"
  [[ "${#MP_SPECS[@]}" -gt 0 ]] && out_kv "Mounts (mp*)" "${MP_SPECS[*]}"
fi

cmd=(pct create "$VMID" "$OSTEMPLATE" --rootfs "$ROOTFS_SPEC" --hostname "$HOSTNAME")

for net_if in $(printf '%s\n' "${!NET_IFACE[@]}" | sort -n); do
  cmd+=(--"net${net_if}" "${NET_IFACE[$net_if]}")
done
cmd+=(--unprivileged "$UNPRIV" --onboot "$ONBOOT")

[[ -n "$ENTRYPOINT" ]] && cmd+=(--entrypoint "$ENTRYPOINT")
for _ev in "${ENVS[@]}"; do
  cmd+=(--env "$_ev")
done

[[ -n "$MEMORY" ]] && cmd+=(--memory "$MEMORY")
[[ -n "$SWAP" ]] && cmd+=(--swap "$SWAP")
[[ -n "$CORES" ]] && cmd+=(--cores "$CORES")
[[ -n "$CPULIMIT" ]] && cmd+=(--cpulimit "$CPULIMIT")
[[ -n "$CPUUNITS" ]] && cmd+=(--cpuunits "$CPUUNITS")
[[ -n "$OSTYPE" ]] && cmd+=(--ostype "$OSTYPE")
[[ -n "$ARCH" ]] && cmd+=(--arch "$ARCH")
[[ -n "$FEATURES" ]] && cmd+=(--features "$FEATURES")

[[ -n "$DESCRIPTION" ]] && cmd+=(--description "$DESCRIPTION")
[[ -n "$TAGS_CREATE" ]] && cmd+=(--tags "$TAGS_CREATE")
[[ -n "$TIMEZONE" ]] && cmd+=(--timezone "$TIMEZONE")
[[ -n "$PASSWORD" ]] && cmd+=(--password "$PASSWORD")
[[ -n "$SSH_PUBLIC_KEYS" ]] && cmd+=(--ssh-public-keys "$SSH_PUBLIC_KEYS")

for ns in "${NAMESERVERS[@]}"; do
  cmd+=(--nameserver "$ns")
done
[[ -n "$SEARCHDOMAIN" ]] && cmd+=(--searchdomain "$SEARCHDOMAIN")

[[ -n "$POOL" ]] && cmd+=(--pool "$POOL")

mp_idx=0
for mp_spec in "${MP_SPECS[@]}"; do
  if [[ ! "$mp_spec" =~ ^([^:]+):([0-9]+(\.[0-9]+)?):(/.*)$ ]]; then
    die "Invalid --mp '${mp_spec}' (expected STORAGE:SIZE:/path — absolute path inside CT; SIZE is GiB, integer or decimal e.g. 8 or 0.25)"
  fi
  mp_st="${BASH_REMATCH[1]}"
  mp_sz="${BASH_REMATCH[2]}"
  mp_path="${BASH_REMATCH[4]}"
  awk -v s="$mp_sz" 'BEGIN { exit !(s > 0) }' || die "Invalid --mp '${mp_spec}': size must be > 0"
  cmd+=( "--mp${mp_idx}" "${mp_st}:${mp_sz},mp=${mp_path}" )
  mp_idx=$((mp_idx + 1))
done
unset mp_st mp_sz mp_path mp_spec 2>/dev/null || true

for dev_if in $(printf '%s\n' "${!DEV_MAP[@]}" | sort -n); do
  cmd+=(--"dev${dev_if}" "${DEV_MAP[$dev_if]}")
done
for un_if in $(printf '%s\n' "${!UNUSED_MAP[@]}" | sort -n); do
  cmd+=(--"unused${un_if}" "${UNUSED_MAP[$un_if]}")
done

[[ -n "$STARTUP" ]] && cmd+=(--startup "$STARTUP")
[[ -n "$HOOKSCRIPT" ]] && cmd+=(--hookscript "$HOOKSCRIPT")
[[ -n "$CMODE" ]] && cmd+=(--cmode "$CMODE")
[[ -n "$CONSOLE" ]] && cmd+=(--console "$CONSOLE")
[[ -n "$TTY" ]] && cmd+=(--tty "$TTY")
[[ -n "$PROTECTION" ]] && cmd+=(--protection "$PROTECTION")
[[ -n "$HA_MANAGED" ]] && cmd+=(--ha-managed "$HA_MANAGED")
[[ -n "$IGNORE_UNPACK" ]] && cmd+=(--ignore-unpack-errors "$IGNORE_UNPACK")
[[ -n "$DEBUG_CREATE" ]] && cmd+=(--debug "$DEBUG_CREATE")
[[ -n "$BWLIMIT" ]] && cmd+=(--bwlimit "$BWLIMIT")
[[ -n "$START" ]] && cmd+=(--start "$START")

local _pct_log _pct_ec
_pct_log="$(mktemp "${TMPDIR:-/tmp}/pve-oci-pct.XXXXXX")" || die "mktemp failed for pct log"
_pct_ec=0
if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
  out_cmd "$(printf '%q ' "${cmd[@]}")"
  "${cmd[@]}" >"$_pct_log" 2>&1 || _pct_ec=$?
else
  out_step "run" "" "pct create"
  out_cmd "$(printf '%q ' "${cmd[@]}")"
  "${cmd[@]}" >"$_pct_log" 2>&1 || _pct_ec=$?
fi
if [[ "$_pct_ec" -ne 0 ]]; then
  cat "$_pct_log" >&2
  oci_create_pct_failure_cleanup "$VMID" "$POOL" "$_pct_log"
  rm -f "$_pct_log"
  die "pct create failed"
fi
rm -f "$_pct_log"

if [[ "$_PVE_OCI_CREATE_QUIET" -eq 1 ]]; then
  out_muted "Scratch CT ${VMID} ready (stopped) — used only as rsync source; will be destroyed."
else
  out_ok "CT ${VMID} created (stopped) — pct start ${VMID} when ready"
fi
}
