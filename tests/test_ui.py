import pytest

from pve_oci_compose import ui


@pytest.mark.parametrize("sec,exp", [(0, "0s"), (4.4, "4s"), (59.6, "1m 00s"), (72, "1m 12s"), (3725, "1h 02m")])
def test_fmt_duration(sec, exp):
    assert ui.fmt_duration(sec) == exp


@pytest.mark.parametrize("ref,exp", [
    ("ghcr.io/o/app:sha-1", ("ghcr.io/o/app", "sha-1")),
    ("registry:5000/img:2", ("registry:5000/img", "2")),
    ("registry:5000/img", ("registry:5000/img", "")),
    ("x/y@sha256:abc", ("x/y", "@sha256:abc")),
])
def test_split_image(ref, exp):
    assert ui.split_image(ref) == exp


def test_summarize_paths():
    assert ui.summarize_paths(["/a"]) == "1 file injected (/a)"
    assert ui.summarize_paths(["/a", "/b", "/c"]) == "3 files injected (/a, /b, /c)"
    assert ui.summarize_paths([f"/{i}" for i in range(6)]) == "6 files injected (/0, /1, +4 more)"


def test_timeline_layout(capsys):
    tl = ui.Timeline(3)
    tl.header("app", "CT 228", "refresh", old="ghcr.io/o/app:v1", new="ghcr.io/o/app:v2", reason="image ref changed")
    with tl.step("Pull image"):
        ui.muted("pinned x → y")  # output during a step lands under its result line
    with tl.step("Shut down CT 228"):
        tl.downtime_start()
    with tl.step("Backup: none") as st:
        st.result = "skipped"
    tl.downtime_end()
    tl.done("Refreshed", [("rollback", "pct rollback 228 snap"), ("", "temp CT 9 removed")])
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("━━ app · CT 228 · refresh ━━")
    assert out[2] == "   ghcr.io/o/app" and out[3].startswith("   v1 → v2") and "(image ref changed)" in out[4]
    i = next(n for n, line in enumerate(out) if "[1/3] Pull image" in line)  # header now 5 lines
    assert "✓" in out[i] and "pinned x → y" in out[i + 1]
    assert "── downtime starts ──" in out[i + 3]
    assert "–" in out[i + 4] and out[i + 4].rstrip().endswith("skipped")
    assert any("✓ Refreshed in" in line and "downtime" in line for line in out)
    assert any(line.startswith("   rollback pct rollback 228 snap") for line in out)


def test_failed_step_marks_cross_and_reraises(capsys):
    tl = ui.Timeline(2)
    with pytest.raises(RuntimeError):
        with tl.step("Sync rootfs (rsync)"):
            tl.downtime_start()
            raise RuntimeError("boom")
    tl.failed("Refresh")
    out = capsys.readouterr().out
    assert "[1/2] Sync rootfs (rsync)" in out and "✗" in out
    assert "✗ Refresh failed after" in out and "(CT not running)" in out


def test_warnings_are_not_deferred(capsys):
    tl = ui.Timeline(1)
    with tl.step("Work"):
        ui.warn("careful")
        assert "careful" in capsys.readouterr().err  # printed immediately, not after the step


def test_header_full_refs_when_repo_changes(capsys):
    ui.Timeline(1).header("a", "CT 1", "refresh", old="docker.io/x/a:1", new="ghcr.io/y/a:1")
    out = capsys.readouterr().out
    assert "   docker.io/x/a:1" in out and "   → ghcr.io/y/a:1" in out


def test_image_lines_variants():
    assert ui.image_lines(None, "docker.io/library/nginx:latest", new_version="1.31.9") == [
        "   docker.io/library/nginx:latest = 1.31.9"]
    assert ui.image_lines("ghcr.io/b/frigate:stable", "ghcr.io/b/frigate:stable", "0.17.1", "0.18.3")[1] == \
        "   stable: 0.17.1 → 0.18.3"
    # older marker (no digest/version), new version unknown → short digest, never a guess
    assert ui.image_lines("ghcr.io/b/f:stable", "ghcr.io/b/f:stable", new_digest="sha256:d1a3e0008557bb3f")[1] == \
        "   stable: previous → d1a3e0008557"


def test_image_lines_forced_refresh_same_digest():
    assert ui.image_lines("r/x:latest", "r/x:latest", "2.1.4", "2.1.4", "sha256:a", "sha256:a")[1] == \
        "   latest: 2.1.4 (unchanged)"
