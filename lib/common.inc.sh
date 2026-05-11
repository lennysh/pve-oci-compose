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
