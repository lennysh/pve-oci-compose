# shellcheck shell=bash
# Shared helpers for pve-oci-compose (sourced before lib/oci-create.inc.sh).

# Next free cluster VMID from pvesh /cluster/nextid (same parsing as the create/refresh flows).
pve_oci_next_cluster_id() {
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
    id=$(printf '%s\n' "$out" | sed -n 's/.*"data"[[:space:]]*:[[:space:]]*"\([0-9][0-9]*\)".*/\1/p')
    [[ -n "$id" ]] || id=$(printf '%s\n' "$out" | sed -n 's/.*"data"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
    [[ -n "$id" ]] || id=$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p')
  fi

  [[ -n "$id" ]] || return 1
  printf '%s\n' "$id"
}

# Encode a REST path segment (e.g. task UPID) so colons in UPID don't break parsing of the URL.
pve_api_quote_path_segment() {
  python3 -c '
import urllib.parse, sys
s = sys.argv[1]
# encode whole segment — Proxmox expects %3A for colons in UPID:…
print(urllib.parse.quote(s, safe=""))
' "$1" 2>/dev/null || printf '%s\n' "$1"
}

# From pvesh JSON for GET .../tasks/{upid}/status: normalize .data wrapping and return status\\texitstatus.
pve_task_status_from_json() {
  printf '%s' "${1//$'\r'/}" | jq -r '
    def unwrap:
      . as $s |
      if ($s | type) != "string" then $s
      elif ($s | test("^\\s*\\{")) then ($s | fromjson)
      else (($s | try fromjson catch $s))
      end;

    unwrap
    | . as $raw
    | (
        ($raw.data
          | if type == "object" then .
            elif type == "array" and (length > 0) then .[0]
            else empty end)
        // (if ($raw | type) == "object"
              and (($raw.status // "") != "" or ($raw.exitstatus // "") != "") then $raw
           else empty end)
      )
    | if type == "object" then [.status // "", .exitstatus // ""] | @tsv else ["", ""] | @tsv end
  ' 2>/dev/null
}

# --- pve-oci-compose CT marker ----------------------------------------------
# Canonical ref + service live on the **guest rootfs** (included in pct snapshots /
# rollback). UI shows only the short sentinel tag **`pve-oci-compose`** (plus your own tags).
#
# Optionally override path (must stay under `/etc/` or templates may omit it inconsistently).
PVE_OCI_ROOTFS_MARKER="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"

pve_oci_pct_tags() {
  local vmid="$1"
  pct config "$vmid" 2>/dev/null | sed -n 's/^tags: //p' | head -1 || true
}

pve_oci_ct_rootfs_mountpoint() {
  printf '/var/lib/lxc/%s/rootfs' "$1"
}

# Write JSON marker under an already-mounted rootfs tree (absolute host path ending in …/rootfs).
pve_oci_marker_write_mountpoint() {
  local mp="$1" svc="$2" ref="$3"
  local mr p
  mr="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"
  [[ "$mr" == /* ]] || mr="/$mr"
  p="${mp%/}${mr}"
  install -d "$(dirname "$p")"
  python3 -c '
import json, sys
svc, ref, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"service": svc, "ref": ref}, f, separators=(",", ":"), ensure_ascii=False)
    f.write("\n")
' "$svc" "$ref" "$p" || return 1
}

# Stopped CT: mount briefly, write marker, unmount (used after oci create).
pve_oci_marker_write_after_create_stopped() {
  local vmid="$1" svc="$2" ref="$3"
  local mp
  mp="$(pve_oci_ct_rootfs_mountpoint "$vmid")"
  pct mount "$vmid" >/dev/null
  trap 'pct unmount "$vmid" 2>/dev/null || true' EXIT
  if ! [[ -d "$mp" ]]; then
    pct unmount "$vmid" 2>/dev/null || true
    trap - EXIT
    return 1
  fi
  pve_oci_marker_write_mountpoint "$mp" "$svc" "$ref" || {
    pct unmount "$vmid" 2>/dev/null || true
    trap - EXIT
    return 1
  }
  pct unmount "$vmid"
  trap - EXIT
}

# Ref from JSON blob (stdin).
_pve_oci_marker_ref_from_json_blob() {
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    r = d.get("ref")
    if r is None:
        raise ValueError()
    sys.stdout.write(str(r))
except Exception:
    sys.exit(1)
'
}

# JSON object text → ref field (stdout; failure → exit ≠0).
pve_oci_marker_ref_from_json_text() {
  printf '%s' "$1" | _pve_oci_marker_ref_from_json_blob || return 1
}

# Best-effort: ref from marker file inside guest (pct exec while running else mount).
pve_oci_marker_read_rootfs_ref() {
  local vmid="$1" mr mp js r
  mr="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"
  [[ "$mr" == /* ]] || mr="/$mr"
  js=""
  mp="$(pve_oci_ct_rootfs_mountpoint "$vmid")"

  # pct status prints a single-word status line (normally "running" when active).
  if [[ "$(pct status "$vmid" 2>/dev/null | tr -d '\r')" == running ]]; then
    js="$(pct exec "$vmid" -- cat "$mr" 2>/dev/null || true)"
    if [[ -n "$js" ]]; then
      r="$(pve_oci_marker_ref_from_json_text "$js")" || r=""
      if [[ -n "$r" ]]; then printf '%s\n' "$r"; return 0; fi
    fi
  fi

  if ! pct mount "$vmid" >/dev/null 2>&1; then
    return 1
  fi
  trap 'pct unmount "$vmid" 2>/dev/null || true' EXIT
  if [[ -r "${mp%/}${mr}" ]]; then js="$(cat -- "${mp%/}${mr}")" || js=""; fi
  pct unmount "$vmid" >/dev/null 2>&1 || true
  trap - EXIT
  [[ -z "$js" ]] && return 1
  r="$(pve_oci_marker_ref_from_json_text "$js")" || return 1
  [[ -n "$r" ]] || return 1
  printf '%s\n' "$r"
}

# Merge pct tags with a single sentinel (drop duplicate sentinel if present).
_pve_oci_tags_merge_sentinel_py() {
  python3 - "$@" <<'PY'
import re, sys

OWNER = "pve-oci-compose"


def split_tags(s):
    if not s:
        return []
    s = str(s).strip()
    if not s:
        return []
    return [p.strip() for p in re.split(r"[;,]+", s) if p.strip()]


def merge_sentinel_only(existing_line: str) -> str:
    parts = [t for t in split_tags(existing_line) if t != OWNER]
    parts.append(OWNER)
    return ";".join(parts)


if sys.argv[1] != "merge_sentinel":
    sys.exit(2)
sys.stdout.write(merge_sentinel_only(sys.argv[2] if len(sys.argv) > 2 else ""))
PY
}

pve_oci_tags_merge_sentinel_only() {
  local existing="$1"
  _pve_oci_tags_merge_sentinel_py merge_sentinel "${existing:-}"
}

# Stored image ref for drift detection: guest JSON marker only (`PVE_OCI_ROOTFS_MARKER`).
pve_oci_stored_ref() {
  local vmid="$1" r
  if r="$(pve_oci_marker_read_rootfs_ref "$vmid")" && [[ -n "$r" ]]; then
    printf '%s\n' "$r"
    return 0
  fi
  printf '%s\n' ""
}

# Sentinel tag merge + marker file write (creates/refreshes).
pve_oci_set_managed_marker() {
  local vmid="$1" svc="$2" ref="$3"
  local merged
  merged="$(pve_oci_tags_merge_sentinel_only "$(pve_oci_pct_tags "$vmid")")"
  pct set "$vmid" --tags "$merged"

  if [[ "$(pct status "$vmid" 2>/dev/null | tr -d '\r')" == running ]]; then
    pve_oci_marker_write_via_exec "$vmid" "$svc" "$ref"
    return 0
  fi
  pve_oci_marker_write_after_create_stopped "$vmid" "$svc" "$ref" || {
    echo "pve-oci-compose: warning: failed to write ${PVE_OCI_ROOTFS_MARKER} on CT ${vmid} (plan/refresh drift may lag until writable)." >&2
  }
}

# Running CT marker — avoids another host pct mount/unmount cycle.
pve_oci_marker_write_via_exec() {
  local vmid="$1" svc="$2" ref="$3" mr blob
  mr="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"
  blob="$(python3 -c '
import json, sys
svc, ref = sys.argv[1], sys.argv[2]
print(json.dumps({"service": svc, "ref": ref}, separators=(",", ":"), ensure_ascii=False))
' "$svc" "$ref")" || return 1
  pct exec "$vmid" -- sh -ec 'install -d "$(dirname "$1")"' x "$mr" >/dev/null 2>&1 || true
  printf '%s\n' "$blob" | pct exec "$vmid" -- sh -ec 'cat >"$1"' x "$mr" || return 1
}

# --- Datacenter resource pool (UI grouping) --------------------------------
# Compose `name` / `project` → default pool id unless service sets `pool` (empty/null opts out).

pve_oci_effective_pool_for_service() {
  local svcjson="$1"
  local stack="${2:-}"
  jq -r --arg d "$stack" '
    if has("pool") then
      if .pool == null or .pool == false then ""
      elif (.pool | type) == "string" then .pool
      else (.pool | tostring)
      end
    else
      $d
    end
  ' <<<"$svcjson"
}

# Current node name for pool member objects (run on the node that hosts the CT).
pve_oci_local_nodename() {
  if command -v pvecm &>/dev/null; then
    local n
    n="$(pvecm nodename 2>/dev/null)" || true
    [[ -n "$n" ]] && {
      printf '%s\n' "$n"
      return
    }
  fi
  hostname -s
}

pve_oci_pool_json_ok() {
  [[ -n "${1:-}" ]] && printf '%s\n' "$1" | jq -e . >/dev/null 2>&1
}

# Create pool if missing, then add this node’s LXC (unless PVE_OCI_POOL_NO_AUTOCREATE=1).
pve_oci_pool_ensure_lxc_member() {
  local pool="$1" vmid="$2"
  local raw data cr_out pool_created
  [[ -n "$pool" ]] || return 0
  command -v pvesh >/dev/null 2>&1 || {
    echo "pve-oci-compose: pvesh not found; cannot add CT ${vmid} to pool '${pool}'." >&2
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "pve-oci-compose: jq is required for pool membership updates." >&2
    return 1
  }

  raw="$(pvesh get "/pools/${pool}" --output-format json 2>/dev/null)" || true

  if ! pve_oci_pool_json_ok "$raw"; then
    if [[ "${PVE_OCI_POOL_NO_AUTOCREATE:-0}" == 1 ]]; then
      echo "pve-oci-compose: pool '${pool}' not found; unset PVE_OCI_POOL_NO_AUTOCREATE to auto-create, or create the pool in the UI." >&2
      printf '%s\n' "$raw" >&2
      return 1
    fi
    pool_created=0
    if cr_out="$(pvesh create /pools --poolid "$pool" --comment 'pve-oci-compose (auto-created)' 2>&1)"; then
      pool_created=1
    elif echo "$cr_out" | grep -qiE 'already exist|already exists|duplicate'; then
      pool_created=0
    elif cr_out="$(pvesh create /pools --poolid "$pool" 2>&1)"; then
      pool_created=1
    elif echo "$cr_out" | grep -qiE 'already exist|already exists|duplicate'; then
      pool_created=0
    else
      echo "pve-oci-compose: could not create resource pool '${pool}' (need Pool.Allocate / root). pvesh said:" >&2
      printf '%s\n' "$cr_out" >&2
      return 1
    fi
    raw="$(pvesh get "/pools/${pool}" --output-format json 2>/dev/null)" || true
    if ! pve_oci_pool_json_ok "$raw"; then
      echo "pve-oci-compose: pool '${pool}' still not readable after create. pvesh get output:" >&2
      printf '%s\n' "$raw" >&2
      return 1
    fi
    [[ "$pool_created" -eq 1 ]] && echo "pve-oci-compose: created resource pool '${pool}' (Datacenter → Permissions → Pools)."
  fi

  data="$(printf '%s\n' "$raw" | jq -c 'if type == "object" and (.data | type) == "object" then .data else . end')"
  if printf '%s\n' "$data" | jq -e --arg v "$vmid" '
    (.members // []) | map(select(.type == "lxc" and .vmid == ($v|tonumber))) | length > 0
  ' >/dev/null 2>&1; then
    return 0
  fi

  # PUT /pools/<id> takes vms (pve-vmid-list), not members (members is GET-only).
  if ! pvesh set "/pools/${pool}" --vms "$vmid" --allow-move 1; then
    echo "pve-oci-compose: pvesh set /pools/${pool} failed (--vms, --allow-move)." >&2
    return 1
  fi
}
