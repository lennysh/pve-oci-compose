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

# Which guest type holds vmid in the cluster (qemu and lxc both reserve the same vmid namespace).
# stdout: one of: free | lxc | qemu | unknown
pve_oci_cluster_resources_vmid_kind() {
  local vmid="$1" raw out
  [[ "$vmid" =~ ^[0-9]+$ ]] || {
    printf 'unknown\n'
    return 0
  }
  command -v pvesh >/dev/null 2>&1 || {
    printf 'unknown\n'
    return 0
  }
  command -v jq >/dev/null 2>&1 || {
    printf 'unknown\n'
    return 0
  }
  raw="$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null)" || {
    printf 'unknown\n'
    return 0
  }
  out="$(printf '%s\n' "$raw" | jq -r --argjson vid "$vmid" '
    def unwrap:
      if type == "string" then (if test("^\\s*\\{") then fromjson else . end)
      else . end;
    (unwrap | if type == "object" and (.data != null) then .data else . end)
    | if type == "array" then . else [] end
    | map(select((.type == "lxc" or .type == "qemu") and (.vmid == $vid)))
    | if length == 0 then "free"
      elif .[0].type == "qemu" then "qemu"
      else "lxc"
      end
  ' 2>/dev/null)" || out=""
  [[ -n "$out" ]] || out="unknown"
  printf '%s\n' "$out"
}

# Hosting node name for an LXC vmid (from /cluster/resources). stdout; exit 1 if not found / not lxc.
pve_oci_cluster_lxc_node_for_vmid() {
  local vmid="$1" raw out
  [[ "$vmid" =~ ^[0-9]+$ ]] || return 1
  command -v pvesh >/dev/null 2>&1 || return 1
  command -v jq >/dev/null 2>&1 || return 1
  raw="$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null)" || return 1
  out="$(printf '%s\n' "$raw" | jq -r --argjson vid "$vmid" '
    def unwrap:
      if type == "string" then (if test("^\\s*\\{") then fromjson else . end)
      else . end;
    (unwrap | if type == "object" and (.data != null) then .data else . end)
    | if type == "array" then . else [] end
    | map(select(.type == "lxc" and .vmid == $vid))
    | if length > 0 then (.[0].node // empty) else empty end
  ' 2>/dev/null)" || out=""
  [[ -n "$out" ]] || return 1
  printf '%s\n' "$out"
}

# refresh / cmd_refresh: decide if vmid is an LXC when pct config is missing on this member.
# Sets PVE_OCI_REFRESH_PCT_OK=1 when pct config works here; else PVE_OCI_REFRESH_PCT_OK=0 and
# PVE_OCI_REFRESH_LXC_ON_NODE to the node name used for a successful pvesh …/lxc/…/config probe.
# Optional compose_node_hint is tried after the cluster-reported hosting node.
# Returns 0 if config is readable (pct or pvesh); 1 if not a resolvable LXC.
pve_oci_lxc_refresh_resolve_context() {
  local vmid="$1" hint="${2:-}" host_from_api="" n=""

  PVE_OCI_REFRESH_PCT_OK=0
  PVE_OCI_REFRESH_LXC_ON_NODE=""
  [[ "$vmid" =~ ^[0-9]+$ ]] || return 1

  if pct config "$vmid" &>/dev/null; then
    PVE_OCI_REFRESH_PCT_OK=1
    if command -v pvecm >/dev/null 2>&1; then
      PVE_OCI_REFRESH_LXC_ON_NODE="$(pvecm nodename 2>/dev/null || hostname -s)"
    else
      PVE_OCI_REFRESH_LXC_ON_NODE="$(hostname -s)"
    fi
    return 0
  fi

  host_from_api="$(pve_oci_cluster_lxc_node_for_vmid "$vmid" 2>/dev/null || true)"
  n=""
  if [[ -n "$host_from_api" ]] && pvesh get "/nodes/${host_from_api}/lxc/${vmid}/config" --output-format json &>/dev/null; then
    n="$host_from_api"
  elif [[ -n "$hint" && "$hint" != "$host_from_api" ]] && pvesh get "/nodes/${hint}/lxc/${vmid}/config" --output-format json &>/dev/null; then
    n="$hint"
  elif [[ -n "$hint" && -z "$host_from_api" ]] && pvesh get "/nodes/${hint}/lxc/${vmid}/config" --output-format json &>/dev/null; then
    n="$hint"
  fi

  if [[ -n "$n" ]]; then
    PVE_OCI_REFRESH_LXC_ON_NODE="$n"
    if [[ -n "$host_from_api" && -n "$hint" && "$host_from_api" != "$hint" ]]; then
      echo "refresh: warning: compose node: '${hint}' != cluster placement '${host_from_api}' (using node '${n}' for existence)." >&2
    fi
    return 0
  fi

  return 1
}

# Tags string for pct UI (semicolon-separated). Uses pct(1) when /etc/pve is visible here; else
# pvesh GET /nodes/<host>/lxc/<vmid>/config when cluster resources list the CT (other member / pmxcfs).
pve_oci_lxc_tags_pct_or_api() {
  local vmid="$1" node raw tags
  tags="$(pct config "$vmid" 2>/dev/null | sed -n 's/^tags: //p' | head -1 || true)"
  tags="${tags//$'\r'/}"
  [[ -n "${tags//[[:space:]]/}" ]] && {
    printf '%s\n' "$tags"
    return 0
  }
  node="$(pve_oci_cluster_lxc_node_for_vmid "$vmid" 2>/dev/null)" || node=""
  [[ -z "$node" ]] && {
    printf '%s\n' ""
    return 0
  }
  raw="$(pvesh get "/nodes/${node}/lxc/${vmid}/config" --output-format json 2>/dev/null)" || raw=""
  tags="$(printf '%s\n' "$raw" | jq -r '
    def unwrap:
      if type == "string" then (if test("^\\s*\\{") then fromjson else . end)
      else . end;
    def cfg:
      (unwrap | if type == "object" and (.data != null) then .data else . end);
    cfg
    | if type == "object" and (.tags | type) == "string" then .tags
      elif type == "object" and (.tags != null) then (.tags | tostring)
      elif type == "array" then
        ([.[] | select(.key == "tags")] | first | .value // empty)
      else empty end
  ' 2>/dev/null)" || tags=""
  printf '%s\n' "${tags:-}"
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
  pve_oci_lxc_tags_pct_or_api "${1:?}"
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

# Raw marker JSON from guest rootfs (pct exec while running else mount). stdout; exit 1 if missing.
pve_oci_marker_read_rootfs_json() {
  local vmid="$1" mr mp js
  mr="${PVE_OCI_ROOTFS_MARKER:-/etc/pve-oci-compose.json}"
  [[ "$mr" == /* ]] || mr="/$mr"
  mp="$(pve_oci_ct_rootfs_mountpoint "$vmid")"
  js=""
  if [[ "$(pct status "$vmid" 2>/dev/null | tr -d '\r')" == running ]]; then
    js="$(pct exec "$vmid" -- cat "$mr" 2>/dev/null || true)"
    [[ -n "$js" ]] && {
      printf '%s' "$js"
      return 0
    }
    return 1
  fi
  if ! pct mount "$vmid" >/dev/null 2>&1; then
    return 1
  fi
  trap 'pct unmount "$vmid" 2>/dev/null || true' EXIT
  if [[ -r "${mp%/}${mr}" ]]; then js="$(cat -- "${mp%/}${mr}")" || js=""; fi
  pct unmount "$vmid" >/dev/null 2>&1 || true
  trap - EXIT
  [[ -n "$js" ]] && {
    printf '%s' "$js"
    return 0
  }
  return 1
}

# Best-effort: ref from marker file inside guest (pct exec while running else mount).
pve_oci_marker_read_rootfs_ref() {
  local vmid="$1" js r
  js="$(pve_oci_marker_read_rootfs_json "$vmid" 2>/dev/null)" || return 1
  [[ -n "$js" ]] || return 1
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

# Merge env in precedence order: (1) OCI image env on the CT, (2) compose defaults, (3) service.
# Writes /etc/pve/lxc/<vmid>.conf (env: NUL-separated); avoids pct set --env (NUL in argv).
# argv2/3: JSON objects; argv4+: optional KEY=value pairs (direct oci_create_main --env).
pve_oci_pct_env_merge_set() {
  local vmid="$1"
  local defaults_json="${2:-{}}"
  local service_json="${3:-{}}"
  shift 3
  python3 - "$vmid" "$defaults_json" "$service_json" "$@" <<'PY'
import json, pathlib, sys
from collections import OrderedDict

def parse_env_blob(blob):
    for part in blob.split("\0"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k:
            yield k, v

def parse_runtime_line(line):
    _, _, rest = line.partition(":")
    rest = rest.strip()
    if not rest or "=" not in rest:
        return None, None
    k, _, v = rest.partition("=")
    return (k, v) if k else (None, None)

def layer(ordered, mapping):
    for k, v in mapping.items():
        ordered[str(k)] = str(v)

vmid = sys.argv[1]
defaults = json.loads(sys.argv[2] or "{}")
service = json.loads(sys.argv[3] or "{}")
extra_pairs = sys.argv[4:]

if not defaults and not service and not extra_pairs:
    sys.exit(0)

cfg = pathlib.Path(f"/etc/pve/lxc/{vmid}.conf")
if not cfg.is_file():
    sys.stderr.write(f"pve-oci-compose: CT config missing: {cfg}\n")
    sys.exit(1)

text = cfg.read_text(encoding="utf-8", errors="replace")
lines = text.splitlines(keepends=True)
body = []
image = OrderedDict()
for line in lines:
    raw = line.rstrip("\n")
    if raw.startswith("env:"):
        for k, v in parse_env_blob(raw[4:].lstrip()):
            image[k] = v
        continue
    if raw.startswith("lxc.environment.runtime:"):
        k, v = parse_runtime_line(raw)
        if k:
            image[k] = v
        continue
    body.append(line)

merged = OrderedDict(image)
layer(merged, defaults)
layer(merged, service)
for p in extra_pairs:
    if not p or "=" not in p:
        continue
    k, _, v = p.partition("=")
    if k:
        merged[k] = v

if not merged:
    sys.exit(0)

while body and body[-1].strip() == "":
    body.pop()
if body and not body[-1].endswith("\n"):
    body[-1] = body[-1] + "\n"

new_text = "".join(body)
if new_text and not new_text.endswith("\n"):
    new_text += "\n"
new_text += "env: " + "\0".join(f"{k}={v}" for k, v in merged.items()) + "\n"
cfg.write_text(new_text, encoding="utf-8")
PY
}

# --- Proxmox CT description (Markdown from compose) -------------------------
# stdin: one merged service object (JSON). argv1: stack label; argv2: optional stack-level about.
# argv3: JSON for compose "repo" key (null = default footer URL, false = no footer, string = URL).
# stdout: text for pct --description, or empty to omit the flag (nothing to document).
pve_oci_compose_pct_description() {
  local stack="${1:-}" sabout="${2:-}" repo_json="${3:-null}"
  python3 -c '
import json, sys
from urllib.parse import urlparse

DEFAULT_REPO = "https://github.com/lennysh/pve-oci-compose"

stack = sys.argv[1]
stack_about = sys.argv[2]
repo_raw = sys.argv[3] if len(sys.argv) > 3 else "null"
try:
    repo_cfg = json.loads(repo_raw)
except json.JSONDecodeError:
    repo_cfg = None

svc = json.load(sys.stdin)


def repo_footer_url(cfg):
    if cfg is False:
        return None
    if isinstance(cfg, str) and cfg.strip():
        return cfg.strip()
    return DEFAULT_REPO


def link_label_for_url(url):
    if url == DEFAULT_REPO:
        return "lennysh/pve-oci-compose"
    try:
        p = urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
        return parts[-1] if parts else "repository"
    except Exception:
        return "repository"

def sstrip(x):
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    return ""

body_user = sstrip(svc.get("description"))
sname = sstrip(svc.get("_service")) or "?"

img = svc.get("image") or svc.get("reference")
img = sstrip(img) if img is not None else ""
eff = sstrip(svc.get("_compose_image_effective"))
sync_at = sstrip(svc.get("_template_sync_at"))

about_chunks = []
if sstrip(stack_about):
    about_chunks.append(sstrip(stack_about))
if sstrip(svc.get("about")):
    about_chunks.append(sstrip(svc.get("about")))
about_text = "\n\n".join(about_chunks)


def format_guest_ports(gp):
    out = []
    if gp is None:
        return out
    if not isinstance(gp, list):
        return out
    for item in gp:
        if isinstance(item, str):
            t = item.strip()
            if t:
                out.append(f"- {t}")
        elif isinstance(item, dict):
            p = item.get("port")
            if p is None:
                p = item.get("port_number")
            if p is None:
                continue
            pr = item.get("proto") or item.get("protocol") or "tcp"
            pr = str(pr).strip().lower() or "tcp"
            desc = sstrip(item.get("description") or item.get("desc"))
            if desc:
                out.append(f"- **{p}/{pr}** — {desc}")
            else:
                out.append(f"- **{p}/{pr}**")
    return out


port_lines = format_guest_ports(svc.get("guest_ports"))
compose_triggers = bool(port_lines or about_text)
has_img = bool(img)

# Nothing to put in pct --description.
if not body_user and not compose_triggers and not has_img:
    sys.exit(0)

# User description only (no guest_ports / about / stack about): pass through verbatim.
if body_user and not compose_triggers:
    sys.stdout.write(body_user.rstrip() + "\n")
    sys.exit(0)

lines = [
    "# pve-oci-compose",
    "",
    f"- **Stack:** {stack}" if stack else "- **Stack:** _(compose file)_",
    f"- **Service:** {sname}",
]
if eff:
    lines.append(f"- **Image (pulled):** `{eff}`")
    if img and img != eff:
        lines.append(f"- **Compose file ref:** `{img}`")
elif img:
    lines.append(f"- **Image:** `{img}`")
if sync_at:
    lines.append(f"- **Template sync:** {sync_at}")

if body_user:
    lines.append("")
    lines.extend(body_user.splitlines())

if port_lines:
    lines.append("")
    lines.extend(
        [
            "## Listener ports (inside the CT)",
            "",
            "These are sockets that listen **in the guest** (not Docker Compose `ports:` publish maps). "
            "Reach them via the CT IP, host firewall, or a reverse proxy.",
            "",
        ]
    )
    lines.extend(port_lines)

if about_text:
    lines.append("")
    lines.extend(["## Notes", "", about_text])

footer_url = repo_footer_url(repo_cfg)
if footer_url:
    lines.append("")
    lines.extend(
        [
            "---",
            "",
            f"- **Source:** [{link_label_for_url(footer_url)}]({footer_url})",
        ]
    )

sys.stdout.write("\n".join(lines).rstrip() + "\n")
' "$stack" "$sabout" "$repo_json"
}

# Rich description (guest_ports / about / stack about): after oci_create_main, PVE_OCI_LAST_PULL_REFERENCE
# holds the ref actually pulled (skopeo-resolved :latest, etc.); pve-oci-compose calls finalize → pct set.
pve_oci_compose_description_needs_runtime_meta() {
  local svc="$1" sa="${2:-}"
  # After pull: refresh Image (pulled) + Template sync. Skip when the user set only a verbatim description:.
  jq -e --arg sa "$sa" '
    def has_img:
      ((.image // .reference) | type) == "string" and ((.image // .reference) | length) > 0;
    def user_desc_only:
      (.description | type) == "string" and (.description | length > 0)
      and ((.guest_ports // []) | length == 0)
      and ((.about // "") | length == 0)
      and ($sa | length == 0);
    def rich:
      ((.guest_ports // []) | length > 0)
      or ((.about | type) == "string" and (.about | length > 0))
      or ($sa | length > 0);
    rich or (has_img and (user_desc_only | not))
  ' <<<"$svc" >/dev/null 2>&1
}

pve_oci_compose_pct_description_finalize() {
  local vmid="$1" svc="$2" stack="$3" sa="$4" rj="$5"
  local eff ts merged newd
  [[ -n "$vmid" ]] || return 0
  eff="${PVE_OCI_LAST_PULL_REFERENCE:-}"
  [[ -n "$eff" ]] || eff="$(jq -r '.image // .reference // empty' <<<"$svc")"
  [[ -n "$eff" ]] || return 0
  ts="$(date -u +"%Y-%m-%d %H:%M:%S UTC")"
  merged="$(jq -c --arg eff "$eff" --arg ts "$ts" \
    '. + {_compose_image_effective: $eff, _template_sync_at: $ts}' <<<"$svc")"
  newd="$(printf '%s' "$merged" | pve_oci_compose_pct_description "$stack" "$sa" "$rj")"
  [[ -n "$newd" ]] || return 0
  pct set "$vmid" --description "$newd" \
    || echo "pve-oci-compose: warning: pct set --description failed for CT ${vmid}" >&2
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

# Create Datacenter resource pool if missing. **pct create --pool** fails with 403 if the pool
# does not exist yet, so call this before oci_create_main when passing --pool.
pve_oci_pool_ensure_exists() {
  local pool="$1"
  local raw cr_out pool_created
  [[ -n "$pool" ]] || return 0
  command -v pvesh >/dev/null 2>&1 || {
    echo "pve-oci-compose: pvesh not found; cannot ensure pool '${pool}' exists." >&2
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "pve-oci-compose: jq is required for pool checks." >&2
    return 1
  }

  raw="$(pvesh get "/pools/${pool}" --output-format json 2>/dev/null)" || true
  if pve_oci_pool_json_ok "$raw"; then
    return 0
  fi

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
  if [[ "$pool_created" -eq 1 ]]; then
    echo "pve-oci-compose: created resource pool '${pool}' (Datacenter → Permissions → Pools)."
    # Consumed by oci_create_pct_failure_cleanup if pct create fails in the same apply iteration.
    PVE_OCI_POOL_JUST_AUTOCREATED="$pool"
  fi
  return 0
}

# Create pool if missing, then add this node’s LXC (unless PVE_OCI_POOL_NO_AUTOCREATE=1).
pve_oci_pool_ensure_lxc_member() {
  local pool="$1" vmid="$2"
  local raw data
  [[ -n "$pool" ]] || return 0
  command -v pvesh >/dev/null 2>&1 || {
    echo "pve-oci-compose: pvesh not found; cannot add CT ${vmid} to pool '${pool}'." >&2
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "pve-oci-compose: jq is required for pool membership updates." >&2
    return 1
  }

  pve_oci_pool_ensure_exists "$pool" || return 1
  raw="$(pvesh get "/pools/${pool}" --output-format json 2>/dev/null)" || true
  if ! pve_oci_pool_json_ok "$raw"; then
    echo "pve-oci-compose: pool '${pool}' not readable after ensure_exists." >&2
    printf '%s\n' "$raw" >&2
    return 1
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
