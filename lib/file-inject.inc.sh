# shellcheck shell=bash
# Guest file injection (rootfs + mount/bind paths) for apply and refresh.
# Sourced by pve-oci-compose.sh after lib/common.inc.sh.

# plan output: file inject summary
pve_oci_file_inject_plan_line() {
  local svcjson="$1"
  local n rf mf rfr mfr
  n="$(pve_oci_file_inject_count "$svcjson")"
  [[ "$n" -gt 0 ]] || return 0
  rf="$(jq -r '(.rootfs_files // []) | length' <<<"$svcjson")"
  mf="$(jq -r '(.mount_files // []) | length' <<<"$svcjson")"
  rfr="$(python3 -c '
import json, sys
doc = json.loads(sys.argv[1])

def on_refresh(item):
    for k in ("on_refresh", "refresh"):
        if k in item:
            v = item[k]
            if isinstance(v, bool): return v
            if isinstance(v, (int, float)): return v != 0
            if isinstance(v, str): return v.strip().lower() in ("1", "true", "yes", "on")
    return False

n = sum(1 for i in (doc.get("rootfs_files") or []) if on_refresh(i))
n += sum(1 for i in (doc.get("mount_files") or []) if on_refresh(i))
print(n)
' "$svcjson")"
  mfr="$((rf + mf - rfr))"
  echo "  file inject:   ${rf} rootfs_files, ${mf} mount_files (apply: all ${n}; refresh: ${rfr} with on_refresh; apply-only: ${mfr})"
}

# Count inject specs (for plan).
pve_oci_file_inject_count() {
  jq -r '
    ((.rootfs_files // []) | length) + ((.mount_files // []) | length)
  ' <<<"${1:?}"
}

# Validate rootfs_files / mount_files on a merged service object.
pve_oci_file_inject_validate() {
  local json="$1" svc="$2" compose_dir="$3"
  python3 - "$svc" "$compose_dir" "$json" <<'PY'
import json, os, re, sys

svc_name, compose_dir = sys.argv[1], sys.argv[2]
doc = json.loads(sys.argv[3])

def err(msg):
    print(f"pve-oci-compose: service {svc_name}: {msg}", file=sys.stderr)
    sys.exit(1)

def on_refresh_flag(item):
    if "on_refresh" in item:
        v = item["on_refresh"]
    elif "refresh" in item:
        v = item["refresh"]
    else:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    err(f"on_refresh must be boolean (file {item.get('path', item)})")
    return False

def check_mode(m, label):
    if m is None:
        return
    if isinstance(m, int):
        if m < 0 or m > 0o7777:
            err(f"{label} mode out of range: {m}")
        return
    if isinstance(m, str) and re.fullmatch(r"0?[0-7]{1,4}", m.strip()):
        return
    err(f"{label} mode must be octal string or integer (got {m!r})")

def check_owner(v, label):
    if v is None:
        return
    if isinstance(v, int) and v >= 0:
        return
    if isinstance(v, str) and v.isdigit():
        return
    err(f"{label} must be a non-negative integer")

def has_source(item):
    return bool(item.get("source"))

def has_content(item):
    return "content" in item

def resolve_source(src, compose_dir):
    if not src or not isinstance(src, str):
        err("source must be a non-empty string")
    src = src.strip()
    if src.startswith("/"):
        path = src
    else:
        path = os.path.normpath(os.path.join(compose_dir, src))
    if not os.path.isfile(path):
        err(f"source not a readable file: {path}")
    return path

def guest_paths_from_mounts(doc):
    out = set()
    for raw in doc.get("mounts") or []:
        if not isinstance(raw, str):
            continue
        parts = raw.split(":")
        if len(parts) < 3:
            continue
        gp = parts[2].split(",")[0].strip()
        if gp.startswith("/"):
            out.add(gp.rstrip("/") or "/")
    for raw in doc.get("bind_mounts") or []:
        if not isinstance(raw, str):
            continue
        m = re.search(r",mp=([^,]+)", raw)
        if m:
            gp = m.group(1).strip()
            if gp.startswith("/"):
                out.add(gp.rstrip("/") or "/")
    return out

known_mps = guest_paths_from_mounts(doc)

for key, label in (("rootfs_files", "rootfs_files"), ("mount_files", "mount_files")):
    items = doc.get(key)
    if items is None:
        continue
    if not isinstance(items, list):
        err(f"{label} must be a YAML list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            err(f"{label}[{i}] must be a mapping")
        on_refresh_flag(item)
        check_mode(item.get("mode"), f"{label}[{i}]")
        check_owner(item.get("owner"), f"{label}[{i}].owner")
        check_owner(item.get("group"), f"{label}[{i}].group")
        hs = has_source(item)
        hc = has_content(item)
        if hs and hc:
            err(f"{label}[{i}]: use source or content, not both")
        if not hs and not hc:
            err(f"{label}[{i}]: missing source or content")
        if hs:
            resolve_source(item["source"], compose_dir)
        for pk in ("path", "mount", "mp"):
            pv = item.get(pk)
            if isinstance(pv, str) and ".." in pv.split("/"):
                err(f"{label}[{i}].{pk}: '..' is not allowed ({pv!r})")
        if key == "rootfs_files":
            p = item.get("path")
            if not p or not isinstance(p, str) or not p.startswith("/"):
                err(f"rootfs_files[{i}].path must be an absolute guest path (e.g. /etc/resolv.conf)")
        else:
            mp = item.get("mount") or item.get("mp")
            if not mp or not isinstance(mp, str):
                err(f"mount_files[{i}] needs mount: (guest mp= path from mounts or bind_mounts)")
            mp_norm = mp.rstrip("/") or "/"
            if known_mps and mp_norm not in known_mps:
                err(
                    f"mount_files[{i}].mount {mp!r} not found in mounts/bind_mounts "
                    f"(known guest paths: {', '.join(sorted(known_mps)) or 'none'})"
                )
            rel = item.get("path")
            if not rel or not isinstance(rel, str):
                err(f"mount_files[{i}].path required (relative to mount or absolute under mount)")
            if rel.startswith("/") and known_mps:
                if not any(rel == k or rel.startswith(k + "/") for k in known_mps):
                    err(f"mount_files[{i}].path {rel!r} is not under mount {mp!r}")

if (doc.get("mount_files") or []) and not known_mps:
    err("mount_files requires at least one mounts: or bind_mounts: entry with a guest mp= path")
PY
}

# phase: apply | refresh — apply injects all specs; refresh only on_refresh: true
# rootfs_mp: host path to …/rootfs when already mounted (refresh); empty → pct mount briefly
# Writes go through pve_oci_compose/inject.py + guestfs.py (no symlink following, guest→host id map).
pve_oci_file_inject_run() {
  local vmid="$1" phase="$2" svcjson="$3" compose_dir="$4" rootfs_mp="${5:-}"
  local count mounted=0 ec=0 unpriv_default
  count="$(pve_oci_file_inject_count "$svcjson")"
  [[ "$count" -gt 0 ]] || return 0

  if [[ -z "$rootfs_mp" ]]; then
    pct mount "$vmid" >/dev/null || {
      echo "pve-oci-compose: file inject: pct mount ${vmid} failed" >&2
      return 1
    }
    mounted=1
    rootfs_mp="$(pve_oci_ct_rootfs_mountpoint "$vmid")"
    if [[ ! -d "$rootfs_mp" ]]; then
      pct unmount "$vmid" 2>/dev/null || true
      echo "pve-oci-compose: file inject: rootfs mount missing at $rootfs_mp" >&2
      return 1
    fi
  fi

  # Only used when pct config has no unprivileged: line (pct create defaults to unprivileged).
  unpriv_default="$(jq -r 'if .unprivileged == null then "1"
    elif (.unprivileged | type) == "boolean" then (if .unprivileged then "1" else "0" end)
    else (.unprivileged | tostring) end' <<<"$svcjson")"
  printf '%s' "$svcjson" | PYTHONPATH="${SCRIPT_DIR:?}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m pve_oci_compose.inject --vmid "$vmid" --phase "$phase" \
    --compose-dir "$compose_dir" --rootfs "$rootfs_mp" --unprivileged-default "$unpriv_default" \
    || ec=$?

  [[ "$mounted" -eq 1 ]] && { pct unmount "$vmid" 2>/dev/null || true; }
  return "$ec"
}

# Mount stopped CT once: marker + apply-phase file inject.
pve_oci_marker_and_files_stopped() {
  local vmid="$1" svc="$2" ref="$3" svcjson="$4" compose_dir="$5"
  local mp rc=0
  mp="$(pve_oci_ct_rootfs_mountpoint "$vmid")"
  pct mount "$vmid" >/dev/null || return 1
  if [[ ! -d "$mp" ]]; then
    rc=1
  elif ! pve_oci_marker_write_mountpoint "$mp" "$svc" "$ref"; then
    rc=1
  elif ! pve_oci_file_inject_report "$vmid" apply "$svcjson" "$compose_dir" "$mp"; then
    rc=1
  fi
  pct unmount "$vmid" 2>/dev/null || true
  return "$rc"
}

pve_oci_set_managed_marker_with_files() {
  local vmid="$1" svc="$2" ref="$3" svcjson="$4" compose_dir="$5"
  local merged
  merged="$(pve_oci_tags_merge_sentinel_only "$(pve_oci_pct_tags "$vmid")")"
  pct set "$vmid" --tags "$merged"

  if [[ "$(pct status "$vmid" 2>/dev/null | tr -d '\r')" == running ]]; then
    pve_oci_marker_write_via_exec "$vmid" "$svc" "$ref" || return 1
    if [[ "$(pve_oci_file_inject_count "$svcjson")" -gt 0 ]]; then
      echo "pve-oci-compose: CT ${vmid} is running — file inject needs a stopped CT; stop it and re-run apply, or use refresh for on_refresh files." >&2
      return 1
    fi
    return 0
  fi
  if [[ "$(pve_oci_file_inject_count "$svcjson")" -gt 0 ]]; then
    pve_oci_marker_and_files_stopped "$vmid" "$svc" "$ref" "$svcjson" "$compose_dir" || {
      echo "pve-oci-compose: warning: failed marker/file setup on CT ${vmid}." >&2
      return 1
    }
    return 0
  fi
  pve_oci_marker_write_after_create_stopped "$vmid" "$svc" "$ref" || {
    echo "pve-oci-compose: warning: failed to write ${PVE_OCI_ROOTFS_MARKER} on CT ${vmid}." >&2
    return 1
  }
}

# Print injected paths (stdout lines from python) with ui helpers.
pve_oci_file_inject_report() {
  local vmid="$1" phase="$2" svcjson="$3" compose_dir="$4" rootfs_mp="${5:-}"
  local line kind dest out
  if [[ "$DRY_RUN" -eq 1 ]]; then
    python3 - "$phase" "$svcjson" <<'PY'
import json, sys
phase = sys.argv[1]
doc = json.loads(sys.argv[2])

def on_refresh(item):
    if "on_refresh" in item:
        v = item["on_refresh"]
    elif "refresh" in item:
        v = item["refresh"]
    else:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False

def run(item):
    return phase == "apply" or on_refresh(item)

for item in doc.get("rootfs_files") or []:
    if run(item):
        print(f"rootfs\t{item['path']}")
for item in doc.get("mount_files") or []:
    if run(item):
        mp = item.get("mount") or item.get("mp") or ""
        p = item.get("path") or ""
        g = p if p.startswith("/") else f"{mp.rstrip('/')}/{p.lstrip('/')}"
        print(f"mount\t{g}")
PY
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      kind="${line%%	*}"
      dest="${line#*	}"
      echo "DRY-RUN: would inject ($phase) → $dest"
    done
    return 0
  fi
  out="$(pve_oci_file_inject_run "$vmid" "$phase" "$svcjson" "$compose_dir" "$rootfs_mp")" || return 1
  while IFS= read -r line; do
    [[ "$line" == INJECTED* ]] || continue
    dest="${line#*$'\t'}"
    dest="${dest#*$'\t'}"
    echo "pve-oci-compose: injected ${dest}"
  done <<<"$out"
}
