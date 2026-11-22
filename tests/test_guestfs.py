import os

import pytest

from pve_oci_compose.guestfs import GuestPathError, read_file, split_guest_path, write_file


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "rootfs"
    (r / "etc").mkdir(parents=True)
    return r


@pytest.fixture
def host_file(tmp_path):
    f = tmp_path / "host-secret"
    f.write_text("HOST DATA\n")
    return f


def test_write_and_read_roundtrip(root):
    write_file(str(root), "/etc/app/conf.json", b'{"a":1}\n', mode=0o600)
    p = root / "etc/app/conf.json"
    assert p.read_bytes() == b'{"a":1}\n'
    assert (p.stat().st_mode & 0o7777) == 0o600
    assert read_file(str(root), "/etc/app/conf.json") == b'{"a":1}\n'


def test_overwrite_truncates(root):
    write_file(str(root), "/etc/x", b"long content here")
    write_file(str(root), "/etc/x", b"short")
    assert (root / "etc/x").read_bytes() == b"short"


def test_refuses_symlinked_target(root, host_file):
    os.symlink(str(host_file), root / "etc/pve-oci-compose.json")
    with pytest.raises(GuestPathError, match="symlink"):
        write_file(str(root), "/etc/pve-oci-compose.json", b"{}")
    assert host_file.read_text() == "HOST DATA\n"


def test_refuses_relative_symlink_escape(root, host_file):
    os.symlink("../../host-secret", root / "etc/rel")
    with pytest.raises(GuestPathError):
        write_file(str(root), "/etc/rel", b"x")
    assert host_file.read_text() == "HOST DATA\n"


def test_refuses_symlinked_directory(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), root / "etc/linkdir")
    with pytest.raises(GuestPathError, match="traverse"):
        write_file(str(root), "/etc/linkdir/evil", b"x")
    assert not (outside / "evil").exists()


def test_refuses_hardlinked_target(root, host_file):
    os.link(host_file, root / "etc/hl")
    with pytest.raises(GuestPathError, match="hard links"):
        write_file(str(root), "/etc/hl", b"x")
    assert host_file.read_text() == "HOST DATA\n"


def test_refuses_fifo(root):
    os.mkfifo(root / "etc/fifo")
    with pytest.raises(GuestPathError):  # ENXIO (no reader) or "not a regular file"
        write_file(str(root), "/etc/fifo", b"x")


@pytest.mark.parametrize("bad", ["/../etc/x", "/etc/../../x", "", "/", "a/\0b"])
def test_split_rejects(bad):
    with pytest.raises(GuestPathError):
        split_guest_path(bad)


def test_read_does_not_follow_symlink(root, host_file):
    os.symlink(str(host_file), root / "etc/m.json")
    assert read_file(str(root), "/etc/m.json") is None
    assert read_file(str(root), "/etc/missing") is None
