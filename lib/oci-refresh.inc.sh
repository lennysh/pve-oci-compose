# shellcheck shell=bash
# oci-refresh.inc.sh — OCI LXC rootfs refresh (inlined from oci-ct-refresh-rootfs.sh)
# Sourced by pve-oci-compose.sh after lib/ui.inc.sh. Uses out_* helpers from there.

oci_refresh_usage() {
  echo "Usage: pve-oci-compose.sh refresh   (image + vmid from compose file)"
  echo "   or: oci_refresh_main [options] [--] <old_ctid> <new_oci_ref> [temp_ctid]"
  echo ""
  echo "Options:"
  echo "  --pre-backup MODE         Pre-refresh safety: snapshot (default), auto, vzdump, or none"
  echo "  --backup-storage ID       vzdump target when MODE is auto or vzdump (see OCI_REFRESH_VZDUMP_STORAGE)"
  echo "  --no-snapshot             Same as --pre-backup none"
  echo "  --allow-failed-snapshot   Continue if pre-backup fails (default: abort)"
  echo "  -h, --help                Show this help"
  echo ""
  echo "  old_ctid     Running or stopped CT to update (keeps same CTID, net, mpX, ...)"
  echo "  new_oci_ref  OCI image (oci://… or bare ghcr.io/… / docker.io/… — see README)"
  echo "  temp_ctid    Optional; default: next free cluster VMID"
  echo ""
  echo "Example:"
  echo "  ./pve-oci-compose.sh refresh"
  echo "  oci_refresh_main 100 oci://docker.io/library/nginx:latest"
  echo ""
  echo "OCI temp CTs use the same vztmpl path as apply (oci-registry-pull). For multiple template"
  echo "storages on a node, set OCI_REFRESH_TEMPLATE_STORAGE to the vztmpl storage id (compose refresh"
  echo "also exports this from template_storage / storage)."
  echo ""
  echo "Running CTs are shut down gracefully (pct shutdown) before rootfs work; pct stop runs only"
  echo "if still active after OCI_REFRESH_SHUTDOWN_TIMEOUT seconds (default 60). Temp CT uses pct stop."
  echo ""
  echo "Pre-backup modes:"
  echo "  snapshot  pct snapshot only (abort on failure unless --allow-failed-snapshot)"
  echo "  auto        pct snapshot, then vzdump to --backup-storage / OCI_REFRESH_VZDUMP_STORAGE on failure"
  echo "  vzdump      vzdump only (no pct snapshot)"
  echo "  none        skip pre-backup (--no-snapshot)"
  exit 1
}

pve_oci_refresh_node_name() {
  if command -v pvecm >/dev/null 2>&1; then
    pvecm nodename 2>/dev/null || hostname -s
  else
    hostname -s
  fi
}

# vzdump a stopped CT; waits on the Proxmox task UPID when returned. Returns 0 on success.
pve_oci_refresh_vzdump_ct() {
  local vmid="$1" storage="$2"
  local node out upid

  [[ "$vmid" =~ ^[0-9]+$ ]] || return 1
  [[ -n "$storage" ]] || return 1

  node="$(pve_oci_refresh_node_name)"
  NODE="$node"
  export NODE

  out_sub "vzdump CT ${vmid}"
  out_kv "Backup storage" "$storage"
  out_kv "Mode" "stop (CT already offline)"
  out_cmd "pvesh create /nodes/${node}/vzdump --vmid ${vmid} --storage ${storage} --mode stop --compress zstd"

  set +e
  out=$(pvesh create "/nodes/${node}/vzdump" \
    --vmid "$vmid" \
    --storage "$storage" \
    --mode stop \
    --compress zstd \
    --notes-template "pve-oci-compose-pre-refresh" \
    --output-format json 2>&1)
  local vz_rc=$?
  set -e
  if [[ "$vz_rc" -ne 0 ]]; then
    echo "$out" >&2
    return 1
  fi

  upid="$(parse_upid_from_create_response "$out" || true)"
  if [[ -n "$upid" ]]; then
    out_kv "Task UPID" "$upid"
    out_sub "Waiting for vzdump task …"
    wait_for_task "$upid"
  fi

  out_ok "vzdump finished"
  out_kv "Restore" "Proxmox UI → Datacenter → Backup, or storage ${storage} for CT ${vmid}"
  return 0
}

oci_refresh_pre_backup_abort_or_continue() {
  local msg="$1"
  if [[ "$ALLOW_FAILED_SNAPSHOT" -eq 0 ]]; then
    echo "$msg" >&2
    exit 1
  fi
  out_warn "$msg"
  out_warn "Continuing without rollback safety (--allow-failed-snapshot)."
}

oci_refresh_main() {
  unset PVE_OCI_LAST_PULL_REFERENCE PVE_OCI_LAST_REFERENCE_INPUT 2>/dev/null || true
SKIP_SNAPSHOT=0
ALLOW_FAILED_SNAPSHOT=0
PRE_BACKUP_MODE=""
VZDUMP_STORAGE="${OCI_REFRESH_VZDUMP_STORAGE:-}"
while [[ "${1:-}" == -* ]]; do
  case "$1" in
    --)                        shift; break ;;
    --no-snapshot)             SKIP_SNAPSHOT=1; shift ;;
    --allow-failed-snapshot)   ALLOW_FAILED_SNAPSHOT=1; shift ;;
    --pre-backup)              PRE_BACKUP_MODE="${2:?}"; shift 2 ;;
    --backup-storage)          VZDUMP_STORAGE="${2:?}"; shift 2 ;;
    -h|--help)                 oci_refresh_usage ;;
    *)
      echo "Unknown option: $1" >&2
      oci_refresh_usage
      ;;
  esac
done

[[ ${1:-} ]] && [[ ${2:-} ]] || oci_refresh_usage
ORIG_REF_RAW="$2"
if [[ "$SKIP_SNAPSHOT" -ne 0 && "$ALLOW_FAILED_SNAPSHOT" -ne 0 ]]; then
  echo "Cannot combine --no-snapshot with --allow-failed-snapshot (nothing to allow)." >&2
  exit 1
fi
if [[ "$SKIP_SNAPSHOT" -ne 0 && -n "$PRE_BACKUP_MODE" && "$PRE_BACKUP_MODE" != "none" ]]; then
  echo "Cannot combine --no-snapshot with --pre-backup ${PRE_BACKUP_MODE}." >&2
  exit 1
fi

if [[ "$SKIP_SNAPSHOT" -ne 0 ]]; then
  PRE_BACKUP="none"
elif [[ -n "$PRE_BACKUP_MODE" ]]; then
  PRE_BACKUP="$PRE_BACKUP_MODE"
else
  PRE_BACKUP="${OCI_REFRESH_PRE_BACKUP:-snapshot}"
fi
case "$PRE_BACKUP" in
  snapshot|auto|vzdump|none) ;;
  *)
    echo "Invalid pre-backup mode: ${PRE_BACKUP} (use snapshot, auto, vzdump, or none)." >&2
    exit 1
    ;;
esac
if [[ "$PRE_BACKUP" == "none" && "$ALLOW_FAILED_SNAPSHOT" -ne 0 ]]; then
  echo "Cannot combine --no-snapshot / --pre-backup none with --allow-failed-snapshot." >&2
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
[[ "$NEW_OCI" != "$2" ]] && out_detail "Image ref normalized: $2 → $NEW_OCI"

if [[ -n "${3:-}" ]]; then
  TEMP="$3"
else
  TEMP="$(pve_oci_next_cluster_id)" || {
    echo "Could not read cluster nextid (pvesh / jq?)." >&2
    exit 1
  }
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

# Host config is not part of rootfs; OCI template updates can change pct `entrypoint`.
# After rsync, align OLD with the temp CT (new image) so init matches the replaced tree.
pct_sync_entrypoint_from_temp() {
  local old="$1" temp="$2"
  local new_ep old_ep

  [[ -z "$temp" ]] && return 0
  pct config "$temp" &>/dev/null || return 0

  new_ep="$(cfg "$temp" entrypoint)"
  old_ep="$(cfg "$old" entrypoint)"

  if [[ -n "$new_ep" ]]; then
    [[ "$new_ep" == "$old_ep" ]] && return 0
    out_sub "Sync pct entrypoint (host config)"
    out_kv "From new image CT" "${temp}: ${new_ep}"
    [[ -n "$old_ep" ]] && out_kv "Previous on CT ${old}" "$old_ep"
    if ! pct set "$old" --entrypoint "$new_ep"; then
      echo "=== pct set --entrypoint failed for CT ${old} ===" >&2
      exit 1
    fi
    out_ok "entrypoint now matches refreshed template"
    return 0
  fi

  if [[ -n "$old_ep" ]]; then
    out_sub "Clear pct entrypoint override"
    out_detail "New template CT ${temp} has no entrypoint — removing explicit override on ${old}."
    if pct set "$old" --delete entrypoint 2>/dev/null; then
      out_ok "entrypoint override removed"
    else
      out_warn "Could not pct set --delete entrypoint (${old}); review manually vs CT ${temp}."
    fi
  fi
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

# Registry refs (oci://…) use the same path as **apply**: oci-registry-pull into vztmpl,
# then pct create VMID STORAGE:vztmpl/<norm>.tar (see lib/oci-create.inc.sh).
# Optional: OCI_REFRESH_TEMPLATE_STORAGE (set by compose from template_storage / storage).
create_temp_ct() {
  local -a cmd oci_args
  local net0="${OCI_REFRESH_TEMP_NET0:-name=eth0,bridge=vmbr0,ip=dhcp}"
  local ref_bare

  if [[ "$NEW_OCI" == oci://* ]]; then
    ref_bare="${NEW_OCI#oci://}"
    out_title "Temp CT ${TEMP} — OCI (oci-registry-pull + vztmpl, same as apply)"
    out_kv "Image" "${NEW_OCI}"
    out_kv "Reference" "${ref_bare}"
    [[ -n "${OCI_REFRESH_TEMPLATE_STORAGE:-}" ]] && out_kv "Template storage" "${OCI_REFRESH_TEMPLATE_STORAGE}"
    out_kv "Temp rootfs" "${ROOTFS_NEWVOL}  ← from size=${SIZE} on CT ${OLD}"
    out_kv "Hostname" "${HOST:-oci-refresh-temp}"
    [[ -n "$MEMORY" ]] && out_kv "Memory (MB)" "${MEMORY}"
    out_kv "net0" "${net0}"

    oci_args=(
      --vmid "$TEMP"
      --reference "$ref_bare"
      --rootfs "$ROOTFS_NEWVOL"
      --hostname "${HOST:-oci-refresh-temp}"
      --net0 "$net0"
      --unprivileged "${UNPRIV:-1}"
      --onboot 0
    )
    [[ -n "${OCI_REFRESH_TEMPLATE_STORAGE:-}" ]] && oci_args+=(--storage "$OCI_REFRESH_TEMPLATE_STORAGE")
    [[ -n "${MEMORY:-}" ]] && oci_args+=(--memory "$MEMORY")
    [[ -n "${OSTYPE:-}" ]] && oci_args+=(--ostype "$OSTYPE")
    [[ -n "${ARCH:-}" ]] && oci_args+=(--arch "$ARCH")
    [[ -n "${FEATURES:-}" ]] && oci_args+=(--features "$FEATURES")

    out_cmd "$(printf 'PVE_OCI_CREATE_QUIET=1 %q ' oci_create_main)$(printf '%q ' "${oci_args[@]}")"
    PVE_OCI_CREATE_QUIET=1 oci_create_main "${oci_args[@]}"
    unset PVE_OCI_CREATE_QUIET 2>/dev/null || true
    out_ok "Temp CT ${TEMP} ready — disposable source for rsync (will be destroyed)"
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

    out_cmd "$(printf '%q ' "${cmd[@]}")"

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
CT_DISK_STORAGE="${ROOTFS_LINE%%:*}"
VOL_AND_REST="${ROOTFS_LINE#*:}"
SIZE="${VOL_AND_REST##*,size=}"
SIZE="${SIZE%%,*}"

if [[ -z "$SIZE" || "$SIZE" == "$VOL_AND_REST" ]]; then
  echo "Could not parse size= from rootfs line: $ROOTFS_LINE" >&2
  exit 1
fi

if [[ "$CT_DISK_STORAGE" == "oci" ]]; then
  echo "Parsed storage id is 'oci' from rootfs line — that is almost certainly wrong." >&2
  echo "  rootfs line was: ${ROOTFS_LINE}" >&2
  echo "  Expected form:   <STORAGE_ID>:<volume>,size=<N>G  (e.g. local-zfs:vm-100-disk-0,size=8G)" >&2
  exit 1
fi

ROOTFS_NEWVOL="$(rootfs_alloc_for_pct_create "$CT_DISK_STORAGE" "$SIZE")"

HOST="$(cfg "$OLD" hostname)"
OSTYPE="$(cfg "$OLD" ostype)"
UNPRIV="$(cfg "$OLD" unprivileged)"
ARCH="$(cfg "$OLD" arch)"
FEATURES="$(cfg "$OLD" features)"
MEMORY="$(cfg "$OLD" memory)"

M_OLD="/var/lib/lxc/${OLD}/rootfs"
M_NEW="/var/lib/lxc/${TEMP}/rootfs"

out_title "OCI rootfs refresh"
out_kv "CT (keep)" "$OLD"
out_kv "Temp CT" "$TEMP"
out_kv "Image" "$NEW_OCI"
out_kv "Rootfs line" "$ROOTFS_LINE"
out_kv "Temp alloc" "${ROOTFS_NEWVOL}  (from ${SIZE})"
[[ -n "$MEMORY" ]] && out_kv "Memory (MB)" "${MEMORY} (cloned to temp create)"
out_kv "Node" "$(hostname -s)"

out_sub "Shutdown CT ${OLD}"
if pve_oci_pct_ct_is_active "$OLD"; then
  out_kv "Graceful timeout" "${OCI_REFRESH_SHUTDOWN_TIMEOUT:-60}s (then pct stop)"
  if ! pve_oci_pct_shutdown_or_stop "$OLD"; then
    echo "Could not shut down or stop CT ${OLD}." >&2
    exit 1
  fi
  out_ok "CT ${OLD} offline"
else
  out_ok "CT ${OLD} already stopped"
fi

# Pre-backup before rootfs rsync: pct snapshot, vzdump, or both (auto = snapshot then vzdump on failure).
# Bind-mount host paths are not part of the root volume; snapshot mainly covers managed volumes.
case "$PRE_BACKUP" in
  none)
    out_sub "Pre-backup (none)"
    out_ok "Skipped"
    ;;
  vzdump)
    [[ -n "$VZDUMP_STORAGE" ]] \
      || oci_refresh_pre_backup_abort_or_continue "pre-backup vzdump requires --backup-storage or OCI_REFRESH_VZDUMP_STORAGE (compose refresh_backup_storage)."
    if [[ -n "$VZDUMP_STORAGE" ]]; then
      pve_oci_refresh_vzdump_ct "$OLD" "$VZDUMP_STORAGE" \
        || oci_refresh_pre_backup_abort_or_continue "vzdump failed for CT ${OLD} on storage ${VZDUMP_STORAGE}."
    fi
    ;;
  snapshot|auto)
    SNAP_NAME="pre-oci-refresh-$(date -u +%Y%m%d-%H%M%S)UTC"
    SNAP_DESC="oci-ct-refresh-rootfs.sh before rsync from ${NEW_OCI}"
    out_sub "Pre-backup (${PRE_BACKUP})"
    out_kv "Snapshot" "$SNAP_NAME"
    [[ "$PRE_BACKUP" == "auto" && -n "$VZDUMP_STORAGE" ]] && out_kv "vzdump fallback" "$VZDUMP_STORAGE"
    set +e
    pct snapshot "$OLD" "$SNAP_NAME" --description "$SNAP_DESC"
    snap_rc=$?
    set -e
    if [[ "$snap_rc" -eq 0 ]]; then
      out_ok "Snapshot created"
      out_kv "Rollback cmd" "pct rollback ${OLD} ${SNAP_NAME}"
    elif [[ "$PRE_BACKUP" == "snapshot" ]]; then
      oci_refresh_pre_backup_abort_or_continue "pct snapshot failed (exit ${snap_rc}). Fix storage/snapshot support, use --pre-backup auto with refresh_backup_storage, or pass --allow-failed-snapshot."
    else
      out_warn "pct snapshot failed (exit ${snap_rc}); trying vzdump fallback …"
      if [[ -z "$VZDUMP_STORAGE" ]]; then
        oci_refresh_pre_backup_abort_or_continue "snapshot failed and no backup storage configured (set refresh_backup_storage in compose, OCI_REFRESH_VZDUMP_STORAGE, or --backup-storage)."
      elif ! pve_oci_refresh_vzdump_ct "$OLD" "$VZDUMP_STORAGE"; then
        oci_refresh_pre_backup_abort_or_continue "snapshot and vzdump both failed for CT ${OLD}."
      fi
    fi
    ;;
esac

if pct config "$TEMP" &>/dev/null; then
  out_warn "Temp CT ${TEMP} already exists — reusing (will stop and refresh from image)."
else
  create_temp_ct
fi

out_sub "Stop temp CT ${TEMP}"
pct stop "$TEMP" 2>/dev/null || true
out_detail "pct stop temp CT is harmless if already stopped"

out_sub "Mount root filesystems"
out_detail "pct mount keeps a lock on the CT until unmount"
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

out_sub "rsync rootfs (${OLD})"
[[ ${#excludes[@]} -gt 0 ]] && out_cmd "rsync excludes: ${excludes[*]}"
out_detail "Mount-point excludes needed so rsync --delete does not hit bind-mounted mp= paths inside rootfs."
out_detail "${M_NEW}/ → ${M_OLD}/"
rsync -aHAX --delete "${excludes[@]}" "${M_NEW}/" "${M_OLD}/"
out_ok "rsync done"

svc_mark="${PVE_OCI_COMPOSE_SERVICE:--}"
ref_mark="${PVE_OCI_COMPOSE_REF:-$ORIG_REF_RAW}"
out_sub "Guest compose marker (${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json})"
if pve_oci_marker_write_mountpoint "$M_OLD" "$svc_mark" "$ref_mark"; then
  out_kv "Written" "(included in pct snapshots / rollback)"
  out_detail "Driver prefers this JSON over tags for drift detection."
else
  out_warn "Could not write guest marker — run compose refresh or apply afterward."
fi

if [[ -n "${PVE_OCI_COMPOSE_SVC_JSON:-}" && -n "${PVE_OCI_COMPOSE_DIR:-}" ]] \
  && [[ "$(pve_oci_file_inject_count "$PVE_OCI_COMPOSE_SVC_JSON")" -gt 0 ]]; then
  out_sub "Compose file inject (on_refresh)"
  pve_oci_file_inject_report "$OLD" refresh "$PVE_OCI_COMPOSE_SVC_JSON" "$PVE_OCI_COMPOSE_DIR" "$M_OLD" \
    || die "refresh: failed to inject compose files into CT ${OLD}"
fi

trap - EXIT
out_sub "Cleanup: unmount → sync entrypoint → destroy ${TEMP} → start ${OLD}"
pct unmount "$TEMP"
pct unmount "$OLD"

pct_sync_entrypoint_from_temp "$OLD" "$TEMP"

pct destroy "$TEMP"
pct start "$OLD"

out_ok "Refresh complete — CT ${OLD} running (temp VMID ${TEMP} removed)"

}
