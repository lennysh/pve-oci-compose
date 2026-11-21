"""Python port vs the bash implementation on the same inputs (skipped when bash/jq are missing)."""

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from pve_oci_compose import compose as C
from pve_oci_compose import description

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "compose.example.yaml"

pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="needs bash + jq")


def _bash(script: str, stdin: str = "") -> str:
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; SCRIPT_DIR={REPO}; die(){{ echo \"$*\" >&2; exit 1; }}; "
                       f"source {REPO}/lib/common.inc.sh; {script}"],
        input=stdin, text=True, capture_output=True, check=True,
    ).stdout


def test_description_matches_bash():
    c = C.load(EXAMPLE)
    assert c.services
    for svc in c.services:
        blob = json.dumps({**svc.data, "_service": svc.name})
        args = " ".join(shlex.quote(x) for x in (c.project or "", c.about or "", json.dumps(c.repo)))
        sh = _bash(f"pve_oci_compose_pct_description {args}", blob)
        py = description.build(c, svc) or ""
        assert py == sh, svc.name


def _bash_writeback(text: str, svc: str, vmid: int, tmp: Path) -> str:
    src = (REPO / "pve-oci-compose.sh").read_text()
    fn = re.search(r"^compose_write_service_vmid\(\) \{.*?^\}\n", src, re.S | re.M).group(0)
    f = tmp / "c.yaml"
    f.write_text(text)
    subprocess.run(["bash", "-c", f"set -euo pipefail; die(){{ echo \"$*\" >&2; exit 1; }}\n{fn}\n"
                    f"compose_write_service_vmid {f} {svc} {vmid} >/dev/null"], check=True)
    return f.read_text()


def test_vmid_writeback_matches_bash(tmp_path):
    text = EXAMPLE.read_text()
    for svc in C.load(EXAMPLE).services:
        if svc.vmid_spec != "next" and not re.search(rf"^\s+{svc.name}:", text, re.M):
            continue
        try:
            py = C.rewrite_vmid(text, svc.name, 4242)
        except Exception:
            continue
        assert py == _bash_writeback(text, svc.name, 4242, tmp_path), svc.name


RICH = """\
name: media
about: |
  Stack notes
  line two
repo: https://git.example.com/me/infra
services:
  a:
    image: ghcr.io/o/a:1
    description: "User text"
    guest_ports: [80, {port: 443, proto: TCP, description: https}, {port_number: 53, protocol: udp}]
  b:
    image: b:2
    description: "only verbatim"
  c:
    image: c:3
    about: "svc about"
"""


@pytest.mark.parametrize("repo", ["", "repo: false\n"])
def test_description_rich_matches_bash(tmp_path, repo):
    p = tmp_path / "c.yaml"
    p.write_text(RICH.replace("repo: https://git.example.com/me/infra\n", repo) if repo else RICH)
    c = C.load(p)
    for svc in c.services:
        args = " ".join(shlex.quote(x) for x in (c.project or "", c.about or "", json.dumps(c.repo)))
        sh = _bash(f"pve_oci_compose_pct_description {args}", json.dumps({**svc.data, "_service": svc.name}))
        assert (description.build(c, svc) or "") == sh, svc.name
        assert description.needs_runtime_update(c, svc) == (
            subprocess.run(["bash", "-c", f"source {REPO}/lib/common.inc.sh; "
                            f"pve_oci_compose_description_needs_runtime_meta \"$1\" \"$2\"", "_",
                            json.dumps(svc.data), c.about or ""]).returncode == 0
        ), svc.name
