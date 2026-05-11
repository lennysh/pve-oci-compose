# shellcheck shell=bash
# Shared terminal UI for oci-create and oci-refresh (sourced before those libs).
#
# Set NO_COLOR=1 or run without a tty for plain text.
# Set PVE_OCI_VERBOSE=1 for dim hints (out_detail), echoed shell commands (out_cmd), and extras.

_OUTW=80
_PVE_UI_READY=
_B=_D=_G=_Y=_M=_R=

_pve_ui_init() {
  [[ -n "$_PVE_UI_READY" ]] && return 0
  _PVE_UI_READY=1
  _B=$_D=$_G=$_Y=$_M=$_R=
  _OUTW=$(( ${COLUMNS:-80} - 4 ))
  [[ "$_OUTW" -lt 48 ]] && _OUTW=48
  [[ "$_OUTW" -gt 100 ]] && _OUTW=100
  if [[ -n "${NO_COLOR:-}" ]] || ! [[ -t 1 ]] || ! command -v tput >/dev/null 2>&1; then
    return 0
  fi
  _B=$(tput bold 2>/dev/null || true)
  _D=$(tput dim 2>/dev/null || true)
  _G=$(tput setaf 2 2>/dev/null || true)
  _Y=$(tput setaf 3 2>/dev/null || true)
  _M=$(tput setaf 6 2>/dev/null || true)
  _R=$(tput sgr0 2>/dev/null || true)
}

pve_ui_verbose() { [[ "${PVE_OCI_VERBOSE:-0}" == 1 ]]; }

pve_ui_hr() {
  local i
  printf '%s' "$_D"
  for ((i = 0; i < _OUTW; i++)); do printf '─'; done
  printf '%s\n' "$_R"
}

# Major phase (OCI create, registry pull, rootfs refresh, …)
out_title() {
  _pve_ui_init
  printf '\n%s%s%s\n' "${_B}${_M}" "$*" "${_R}"
  pve_ui_hr
}

# Section under a phase
out_sub() {
  _pve_ui_init
  printf '\n%s%s%s\n' "${_B}" "$*" "${_R}"
}

out_kv() {
  _pve_ui_init
  printf '  %-20s  %s\n' "$1" "$2"
}

# Subdued one-liner — always printed (counts as “minimal” chatter)
out_muted() {
  _pve_ui_init
  printf '  %s%s%s\n' "${_D}" "$*" "${_R}"
}

# Verbose-only: implementation notes, long hints (default off unless PVE_OCI_VERBOSE=1)
out_detail() {
  _pve_ui_init
  pve_ui_verbose || return 0
  printf '  %s%s%s\n' "${_D}" "$*" "${_R}"
}

# Optional full command-line preview (verbose only); skopeo/pct noise still streams from the tools
out_cmd() {
  _pve_ui_init
  pve_ui_verbose || return 0
  printf '  %s%s%s\n' "${_D}" "$*" "${_R}"
}

# Step counter within a phase
out_step() {
  _pve_ui_init
  printf '\n%s▸ %s/%s%s  %s\n' "${_B}${_Y}" "$1" "$2" "${_R}" "$3"
}

out_ok() {
  _pve_ui_init
  printf '%s✓ %s%s\n' "${_G}" "$*" "${_R}"
}

out_warn() {
  _pve_ui_init
  printf '%s! %s%s\n' "${_Y}" "$*" "${_R}" >&2
}
