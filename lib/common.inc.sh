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

# --- pve-oci-compose CT marker via pct tags ---------------------------------
# Proxmox tag grammar (pve-tag): [a-z0-9_][a-z0-9_.+-]* — raw image refs are invalid.
# We use a fixed pair:  pve-oci-compose  +  pveocid1<base64url(json{"service","ref"})>.

pve_oci_pct_description() {
  local vmid="$1"
  pct config "$vmid" 2>/dev/null | sed -n 's/^description: //p' | head -1 || true
}

pve_oci_pct_tags() {
  local vmid="$1"
  pct config "$vmid" 2>/dev/null | sed -n 's/^tags: //p' | head -1 || true
}

# Internal: stdin program is "-"; argv: mode args…
_pve_oci_tags_py() {
  python3 - "$@" <<'PY'
import json, base64, re, sys

OWNER = "pve-oci-compose"
PREFIX = "pveocid1"

def split_tags(s):
    if not s:
        return []
    s = str(s).strip()
    if not s:
        return []
    return [p.strip() for p in re.split(r"[;,]+", s) if p.strip()]


def b64e(svc: str, ref: str) -> str:
    j = json.dumps({"service": svc, "ref": ref}, separators=(",", ":"))
    return base64.urlsafe_b64encode(j.encode()).decode().rstrip("=")


def b64d(payload: str) -> dict:
    pad = "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload + pad))


def merge_existing(existing_line: str, svc: str, ref: str) -> str:
    parts = []
    for t in split_tags(existing_line):
        if t == OWNER or t.startswith(PREFIX):
            continue
        parts.append(t)
    parts.append(OWNER)
    parts.append(PREFIX + b64e(svc, ref))
    return ";".join(parts)


def extract_ref_from_tags(tags_line: str) -> str:
    for t in split_tags(tags_line):
        if t.startswith(PREFIX):
            try:
                r = b64d(t[len(PREFIX) :]).get("ref") or ""
                sys.stdout.write(r)
                return
            except Exception:
                return


def extract_ref_from_description(desc: str) -> str:
    d = desc.strip()
    if not d.startswith("pve-oci-compose"):
        return
    m = re.search(r"ref=(.*)$", d, re.DOTALL)
    sys.stdout.write(m.group(1) if m else "")


mode = sys.argv[1]

if mode == "merge":
    existing, svc, ref = sys.argv[2], sys.argv[3], sys.argv[4]
    sys.stdout.write(merge_existing(existing, svc, ref))
elif mode == "extract_ref_tags":
    extract_ref_from_tags(sys.argv[2] if len(sys.argv) > 2 else "")
elif mode == "extract_ref_description":
    extract_ref_from_description(sys.argv[2] if len(sys.argv) > 2 else "")
else:
    sys.exit(2)
PY
}

# Existing tags semicolon-string + compose service/image → full --tags value (preserves unrelated tags).
pve_oci_tags_merge_for_pct_set() {
  local existing="$1" svc="$2" ref="$3"
  _pve_oci_tags_py merge "${existing:-}" "$svc" "$ref"
}

# Image ref tracked for drift (prefers encoded tags, else legacy description line).
pve_oci_stored_ref() {
  local vmid="$1" tags desc r
  tags="$(pve_oci_pct_tags "$vmid")"
  r="$(_pve_oci_tags_py extract_ref_tags "${tags:-}")"
  [[ -n "$r" ]] && {
    printf '%s\n' "$r"
    return 0
  }
  desc="$(pve_oci_pct_description "$vmid")"
  r="$(_pve_oci_tags_py extract_ref_description "${desc:-}")"
  printf '%s\n' "$r"
}

# pct set merged tags plus optional removal of obsolete description marker.
pve_oci_set_managed_tags() {
  local vmid="$1" svc="$2" ref="$3"
  local merged
  merged="$(pve_oci_tags_merge_for_pct_set "$(pve_oci_pct_tags "$vmid")" "$svc" "$ref")"
  pct set "$vmid" --tags "$merged"
  pve_oci_clear_legacy_description_marker_if_present "$vmid"
}

pve_oci_clear_legacy_description_marker_if_present() {
  local vmid="$1"
  local d
  d="$(pve_oci_pct_description "$vmid")"
  [[ "$d" == pve-oci-compose* ]] || return 0
  pct set "$vmid" --description '' 2>/dev/null || true
}
