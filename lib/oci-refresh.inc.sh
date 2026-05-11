# shellcheck shell=bash
# oci-refresh.inc.sh — OCI LXC rootfs refresh (inlined from oci-ct-refresh-rootfs.sh)
# Sourced by pve-oci-compose.sh. Defines oci_refresh_main [options] <old_ctid> <new_oci_ref> [temp_ctid]

# Readable progress (plain text if NO_COLOR=1 or stdout is not a TTY).
_out_init() {
  B=D=G=Y=M=R=
  _OUTW=$(( ${COLUMNS:-80} - 4 ))
  [[ "$_OUTW" -lt 48 ]] && _OUTW=48
  [[ "$_OUTW" -gt 100 ]] && _OUTW=100
  if [[ -n "${NO_COLOR:-}" ]] || ! [[ -t 1 ]] || ! command -v tput >/dev/null 2>&1; then
    return 0
  fi
  B=$(tput bold 2>/dev/null || true)
  D=$(tput dim 2>/dev/null || true)
  G=$(tput setaf 2 2>/dev/null || true)
  Y=$(tput setaf 3 2>/dev/null || true)
  M=$(tput setaf 6 2>/dev/null || true)
  R=$(tput sgr0 2>/dev/null || true)
}
_out_init

hr() {
  local i
  printf '%s' "$D"
  for ((i = 0; i < _OUTW; i++)); do printf '─'; done
  printf '%s\n' "$R"
}

out_title() {
  printf '\n%s%s%s\n' "$B$M" "$*" "$R"
  hr
}

out_sub() {
  printf '\n%s%s%s\n' "$B" "$*" "$R"
}

out_kv() {
  printf '  %-20s  %s\n' "$1" "$2"
}

out_note() {
  printf '  %s%s%s\n' "$D" "$*" "$R"
}

out_step() {
  printf '\n%s▸ %s/%s%s  %s\n' "$B$Y" "$1" "$2" "$R" "$3"
}

out_ok() {
  printf '%s✓ %s%s\n' "$G" "$*" "$R"
}

out_warn() {
  printf '%s! %s%s\n' "$Y" "$*" "$R" >&2
}

out_cmd_line() {
  printf '  %s%s%s\n' "$D" "$*" "$R"
}
oci_refresh_usage() {
  echo "Usage: $0 [options] <old_ctid> <new_oci_ref> [temp_ctid]"
  echo ""
  echo "Options:"
  echo "  --no-snapshot             Skip pct snapshot entirely"
  echo "  --allow-failed-snapshot   Try pct snapshot, but continue if it fails (default: abort on failure)"
  echo "  -h, --help                Show this help"
  echo ""
  echo "  old_ctid     Running or stopped CT to update (keeps same CTID, net, mpX, ...)"
  echo "  new_oci_ref  OCI image (oci://… or bare ghcr.io/… / docker.io/… — see README)"
  echo "  temp_ctid    Optional; default: next free cluster VMID"
  echo ""
  echo "Example:"
  echo "  $0 100 oci://docker.io/library/nginx:latest"
  echo "  $0 --allow-failed-snapshot 100 oci://docker.io/library/nginx:latest"
  echo "  $0 --no-snapshot 100 oci://docker.io/library/nginx:latest"
  exit 1
}

oci_refresh_main() {
SKIP_SNAPSHOT=0
ALLOW_FAILED_SNAPSHOT=0
while [[ "${1:-}" == -* ]]; do
  case "$1" in
    --no-snapshot)             SKIP_SNAPSHOT=1; shift ;;
    --allow-failed-snapshot)   ALLOW_FAILED_SNAPSHOT=1; shift ;;
    -h|--help)                 oci_refresh_usage ;;
    *)
      echo "Unknown option: $1" >&2
      oci_refresh_usage
      ;;
  esac
done

[[ ${1:-} ]] && [[ ${2:-} ]] || oci_refresh_usage
if [[ "$SKIP_SNAPSHOT" -ne 0 && "$ALLOW_FAILED_SNAPSHOT" -ne 0 ]]; then
  echo "Cannot combine --no-snapshot with --allow-failed-snapshot (nothing to allow)." >&2
  exit 1
fi

OLD="$1"
# pct requires oci:// for registry pulls; bare registry/repo:tag is normalized.
normalize_image_ref() {
  local r="$1"
  case "$r" in
    oci://*) printf '%s\n' "$r" ;;
    /*|../*|./*) printf '%s\n' "$r" ;;
    http://*|https://*) printf '%s\n' "$r" ;;
    *:vztmpl/*|*:import/*) printf '%s\n' "$r" ;;
    *) printf 'oci://%s\n' "$r" ;;
  esac
}
NEW_OCI="$(normalize_image_ref "$2")"
if [[ "$NEW_OCI" != "$2" ]]; then
  printf '%sImage ref normalized:%s %s → %s\n' "$D" "$R" "$2" "$NEW_OCI" >&2
fi

# pvesh JSON varies by version: {"data": N}, {"data": "N"}, double-encoded string, or bare number.
next_cluster_id() {
  local out id
  out=$(pvesh get /cluster/nextid --output-format json 2>/dev/null) || return 1
  [[ -n "$out" ]] || return 1

  if command -v jq >/dev/null 2>&1; then
    id=$(printf '%s\n' "$out" | jq -r '
      def unwrap:
        if type == "string" then
          if test("^\\s*\\{") then fromjson else . end
        else . end;
      unwrap
      | if type == "object" and (.data != null) then .data else . end
      | if type == "number" then tostring
        elif type == "string" and test("^[0-9]+$") then .
        else empty end
    ')
  else
    # No jq: prefer explicit "data" field, then bare JSON integer
    id=$(printf '%s\n' "$out" | sed -n 's/.*"data"[[:space:]]*:[[:space:]]*"\([0-9][0-9]*\)".*/\1/p')
    [[ -n "$id" ]] || id=$(printf '%s\n' "$out" | sed -n 's/.*"data"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
    [[ -n "$id" ]] || id=$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p')
  fi

  [[ -n "$id" ]] || return 1
  printf '%s\n' "$id"
}

if [[ -n "${3:-}" ]]; then
  TEMP="$3"
else
  TEMP="$(next_cluster_id)"
fi

if [[ "$TEMP" == "$OLD" ]]; then
  echo "temp_ctid equals old_ctid; pass an explicit temp id." >&2
  exit 1
fi

if ! pct config "$OLD" &>/dev/null; then
  echo "No CT config for vmid $OLD (wrong id or not on this node?)." >&2
  exit 1
fi

# First field of value line: "key: value" (value may contain ':')
cfg() {
  pct config "$1" | sed -n "s/^$2: //p" | head -1
}

# True if CT should be stopped (running or frozen). The script only needs CT stopped, not "was running".
pct_ct_needs_stop() {
  local s
  s=$(pct status "$1" 2>/dev/null || true)
  [[ "$s" == *running* || "$s" == *frozen* ]]
}

# pct create --rootfs for a NEW disk: STORAGE:<GiB_integer> (e.g. Storage:8, local-zfs:32).
# Using STORAGE:1G is wrong: ZFS/LVM treat "1G" as a volume name → "unable to parse zfs volume name '1G'".
rootfs_alloc_for_pct_create() {
  local st="$1" sz="$2"
  if [[ "$sz" =~ ^([0-9]+)G$ ]]; then
    printf '%s:%s' "$st" "${BASH_REMATCH[1]}"
  elif [[ "$sz" =~ ^([0-9]+)M$ ]]; then
    local mib="${BASH_REMATCH[1]}"
    local gib=$(( (mib + 1023) / 1024 ))
    [[ "$gib" -lt 1 ]] && gib=1
    printf '%s:%s' "$st" "$gib"
  elif [[ "$sz" =~ ^[0-9]+$ ]]; then
    printf '%s:%s' "$st" "$sz"
  else
    printf '%s:%s' "$st" "$sz"
  fi
}

# Proxmox treats <ostemplate> like "STORAGE:volume" and splits on the first ':' (pct, pvesh, API).
# So `oci://registry/...` becomes storage id `oci` → "storage 'oci' does not exist".
# Workaround: skopeo copy docker://REF oci-archive:FILE.tar then pct create VMID /path/file.tar
create_temp_ct() {
  local -a cmd skopeo_args
  local net0="${OCI_REFRESH_TEMP_NET0:-name=eth0,bridge=vmbr0,ip=dhcp}"
  local archive ref tmpdir

  if [[ "$NEW_OCI" == oci://* ]]; then
    ref="${NEW_OCI#oci://}"
    tmpdir="${OCI_REFRESH_TMPDIR:-/var/tmp}"

    out_title "Temp CT ${TEMP} from OCI (skopeo → tar → pct create)"
    out_note "Proxmox splits ostemplate on the first ':' — oci://… is misread as storage 'oci'."
    out_note "This path uses skopeo then a filesystem path to pct (no oci:// in ostemplate)."
    out_kv "Image" "${NEW_OCI}"
    out_kv "Skopeo source" "docker://${ref}"
    out_kv "Archive directory" "${tmpdir}"
    out_note "~2× image size free space; override dir with OCI_REFRESH_TMPDIR."
    out_kv "Temp --rootfs" "${ROOTFS_NEWVOL}  (from size=${SIZE} on CT ${OLD})"
    out_kv "Hostname" "${HOST:-oci-refresh-temp}"
    [[ -n "$MEMORY" ]] && out_kv "Memory (MB)" "${MEMORY}"
    out_kv "Temp net0" "${net0}"
    out_note "Skopeo progress: OCI_REFRESH_SKOPEO_VERBOSE=1"

    if ! command -v skopeo >/dev/null 2>&1; then
      echo "skopeo is required for oci:// temp CTs on current Proxmox (colon parsing bug)." >&2
      echo "Install on the PVE node, e.g.: apt install skopeo" >&2
      exit 1
    fi
    if [[ ! -d "$tmpdir" || ! -w "$tmpdir" ]]; then
      echo "Temp dir not writable: ${tmpdir}" >&2
      exit 1
    fi

    archive="${tmpdir}/oci-refresh-${TEMP}-$$-${RANDOM}.tar"
    skopeo_args=()
    [[ "${OCI_REFRESH_SKOPEO_VERBOSE:-0}" != 1 ]] && skopeo_args+=(--quiet)

    out_step 1 2 "skopeo copy → oci-archive"
    out_cmd_line "skopeo copy${skopeo_args[*]:+ ${skopeo_args[*]}} docker://${ref} oci-archive:${archive}"
    if ! skopeo copy "${skopeo_args[@]}" "docker://${ref}" "oci-archive:${archive}"; then
      echo "=== skopeo copy failed ===" >&2
      echo "Hints: outbound HTTPS; auth for ghcr.io → skopeo login ghcr.io (or /root/.config/containers/auth.json)" >&2
      rm -f "$archive"
      exit 1
    fi
    out_note "Archive on disk:"
    ls -lh "$archive" 2>/dev/null | sed 's/^/    /' || stat "$archive" 2>/dev/null | sed 's/^/    /' || true

    cleanup_oci_tar() { rm -f "$archive"; }
    trap 'cleanup_oci_tar' EXIT

    out_step 2 2 "pct create (local OCI archive)"
    cmd=(
      pct create "$TEMP" "$archive"
      --hostname "${HOST:-oci-refresh-temp}"
      --rootfs "${ROOTFS_NEWVOL}"
      --onboot 0
      --net0 "$net0"
    )
    [[ -n "$OSTYPE" ]] && cmd+=( --ostype "$OSTYPE" )
    [[ -n "$UNPRIV" ]] && cmd+=( --unprivileged "$UNPRIV" )
    [[ -n "$ARCH" ]] && cmd+=( --arch "$ARCH" )
    [[ -n "$FEATURES" ]] && cmd+=( --features "$FEATURES" )
    [[ -n "$MEMORY" ]] && cmd+=( --memory "$MEMORY" )

    out_cmd_line "$(printf '%q ' "${cmd[@]}")"

    if ! "${cmd[@]}"; then
      echo "=== pct create from OCI archive failed ===" >&2
      echo "Check: storage '${STORAGE}' rootdir-capable, VMID ${TEMP} free, archive still at ${archive}" >&2
      exit 1
    fi

    out_ok "pct create finished — temp CT ${TEMP} (typically stopped). Removing tarball…"
    rm -f "$archive"
    trap - EXIT
  else
    out_title "Temp CT ${TEMP} (local template / vztmpl)"
    cmd=(
      pct create "$TEMP" "$NEW_OCI"
      --hostname "${HOST:-oci-refresh-temp}"
      --rootfs "${ROOTFS_NEWVOL}"
      --onboot 0
      --net0 "$net0"
    )
    [[ -n "$OSTYPE" ]] && cmd+=( --ostype "$OSTYPE" )
    [[ -n "$UNPRIV" ]] && cmd+=( --unprivileged "$UNPRIV" )
    [[ -n "$ARCH" ]] && cmd+=( --arch "$ARCH" )
    [[ -n "$FEATURES" ]] && cmd+=( --features "$FEATURES" )
    [[ -n "$MEMORY" ]] && cmd+=( --memory "$MEMORY" )

    out_cmd_line "$(printf '%q ' "${cmd[@]}")"

    if ! "${cmd[@]}"; then
      echo >&2 "=== Temp CT create failed (pct) ===" >&2
      exit 1
    fi
    out_ok "pct create finished — temp CT ${TEMP}."
  fi
}

ROOTFS_LINE="$(cfg "$OLD" rootfs)"
if [[ -z "$ROOTFS_LINE" ]]; then
  echo "Could not read rootfs: for CT $OLD" >&2
  exit 1
fi

# rootfs: pool:volume,size=8G  -> storage id is substring before first comma's first ':'... 
# Actually format is: STORAGE:VOLREF,size=8G  e.g. local-zfs:vm-100-disk-0,size=32G
# Storage id is everything before the first ':' that starts the volume part - tricky.
# Proxmox storage is first segment before ':' only for simple case local-zfs:subvol
STORAGE="${ROOTFS_LINE%%:*}"
VOL_AND_REST="${ROOTFS_LINE#*:}"
SIZE="${VOL_AND_REST##*,size=}"
SIZE="${SIZE%%,*}"

if [[ -z "$SIZE" || "$SIZE" == "$VOL_AND_REST" ]]; then
  echo "Could not parse size= from rootfs line: $ROOTFS_LINE" >&2
  exit 1
fi

if [[ "$STORAGE" == "oci" ]]; then
  echo "Parsed storage id is 'oci' from rootfs line — that is almost certainly wrong." >&2
  echo "  rootfs line was: ${ROOTFS_LINE}" >&2
  echo "  Expected form:   <STORAGE_ID>:<volume>,size=<N>G  (e.g. local-zfs:vm-100-disk-0,size=8G)" >&2
  exit 1
fi

ROOTFS_NEWVOL="$(rootfs_alloc_for_pct_create "$STORAGE" "$SIZE")"

HOST="$(cfg "$OLD" hostname)"
OSTYPE="$(cfg "$OLD" ostype)"
UNPRIV="$(cfg "$OLD" unprivileged)"
ARCH="$(cfg "$OLD" arch)"
FEATURES="$(cfg "$OLD" features)"
MEMORY="$(cfg "$OLD" memory)"

M_OLD="/var/lib/lxc/${OLD}/rootfs"
M_NEW="/var/lib/lxc/${TEMP}/rootfs"

out_title "OCI rootfs refresh"
out_kv "Old CTID" "$OLD"
out_kv "Temp CTID" "$TEMP"
out_kv "New image" "$NEW_OCI"
out_kv "Rootfs (current)" "$ROOTFS_LINE"
out_kv "Temp disk" "${ROOTFS_NEWVOL}  (pct create uses GiB integer, not ${SIZE})"
[[ -n "$MEMORY" ]] && out_kv "Memory (MB)" "${MEMORY} (copied to temp create)"
out_kv "Node" "$(hostname -s)"
out_note "Run on the Proxmox node that owns CT ${OLD}."

out_sub "Stop CT ${OLD} (snapshot + mount need a stopped CT)"
if pct_ct_needs_stop "$OLD"; then
  out_note "State is running or frozen — pct stop…"
  pct stop "$OLD"
  out_ok "CT ${OLD} stopped."
else
  out_ok "CT ${OLD} already stopped — skipped pct stop."
fi

# Proxmox integrates pct snapshot with snapshot-capable rootfs/mp storages (e.g. ZFS).
# Bind-mount host paths are not part of the root volume; snapshot mainly covers managed volumes.
if [[ "$SKIP_SNAPSHOT" -eq 0 ]]; then
  SNAP_NAME="pre-oci-refresh-$(date -u +%Y%m%d-%H%M%S)UTC"
  SNAP_DESC="oci-ct-refresh-rootfs.sh before rsync from ${NEW_OCI}"
  out_sub "Snapshot CT ${OLD}"
  out_note "Name: ${SNAP_NAME}"
  set +e
  pct snapshot "$OLD" "$SNAP_NAME" --description "$SNAP_DESC"
  snap_rc=$?
  set -e
  if [[ "$snap_rc" -eq 0 ]]; then
    out_ok "Snapshot created."
    out_note "Rollback: pct rollback ${OLD} ${SNAP_NAME}"
  else
    if [[ "$ALLOW_FAILED_SNAPSHOT" -eq 0 ]]; then
      echo "Snapshot failed (exit ${snap_rc}); aborting. Fix storage/snapshot support or pass --allow-failed-snapshot." >&2
      exit 1
    fi
    out_warn "pct snapshot failed (exit ${snap_rc}); continuing (--allow-failed-snapshot)."
    out_warn "Prefer snapshot-capable storage, or vzdump/PBS for rollback safety."
  fi
else
  out_sub "Snapshot"
  out_note "Skipped (--no-snapshot)."
fi

if pct config "$TEMP" &>/dev/null; then
  out_warn "Temp CT ${TEMP} already exists — reusing (will stop and refresh from image)."
else
  create_temp_ct
fi

out_sub "Stop temp CT ${TEMP}"
out_note "Usually already stopped after pct create; pct stop is a no-op if so."
pct stop "$TEMP" 2>/dev/null || true

out_sub "Mount root filesystems"
out_note "pct mount holds a lock until unmount."
cleanup_mounts() {
  pct unmount "$TEMP" 2>/dev/null || true
  pct unmount "$OLD" 2>/dev/null || true
}
trap cleanup_mounts EXIT

pct mount "$OLD"
pct mount "$TEMP"
out_ok "Mounted ${M_OLD} and ${M_NEW}"

if [[ ! -d "$M_OLD" || ! -d "$M_NEW" ]]; then
  echo "Expected mount paths missing after pct mount:" >&2
  echo "  $M_OLD" >&2
  echo "  $M_NEW" >&2
  exit 1
fi

# pct config can contain bytes grep treats as "binary" → no matches → no excludes.
# Without excludes, rsync --delete hits bind-mount dirs under pct mount → "rmdir(data0): Device or resource busy".
excludes=()
while IFS= read -r line; do
  [[ "$line" =~ ^mp[0-9]+: ]] || continue
  cpath=""
  if [[ "$line" =~ ,mp=([^,]+) ]]; then
    cpath="${BASH_REMATCH[1]}"
  elif [[ "$line" =~ mp=([^,]+) ]]; then
    cpath="${BASH_REMATCH[1]}"
  fi
  [[ -n "$cpath" ]] || continue
  [[ "$cpath" == /* ]] || cpath="/$cpath"
  rel="${cpath#/}"
  excludes+=( --exclude="${rel}" --exclude="${rel%/}/" )
done < <(pct config "$OLD" | LC_ALL=C grep -aE '^mp[0-9]+:' || true)

out_sub "rsync → CT ${OLD} rootfs"
if [[ ${#excludes[@]} -gt 0 ]]; then
  out_note "rsync excludes (mp= bind-mount paths under rootfs):"
  out_cmd_line "${excludes[*]}"
else
  out_note "No mp= excludes (no mp lines in CT ${OLD} config). Bind mounts under the mount can break rsync --delete if present."
fi
out_note "Syncing ${M_NEW}/ → ${M_OLD}/ …"
rsync -aHAX --delete "${excludes[@]}" "${M_NEW}/" "${M_OLD}/"
out_ok "rsync completed."

trap - EXIT
out_sub "Cleanup: unmount → destroy ${TEMP} → start ${OLD}"
pct unmount "$TEMP"
pct unmount "$OLD"

pct destroy "$TEMP"
pct start "$OLD"

}
