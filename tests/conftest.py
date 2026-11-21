import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def chown_calls(monkeypatch):
    """Record fchown calls instead of performing them (tests run unprivileged)."""
    calls = []

    def fake_fchown(fd, uid, gid):
        calls.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid))

    monkeypatch.setattr(os, "fchown", fake_fchown)
    return calls
