"""Exception types. The CLI turns ComposeError into ``pve-oci-compose: <msg>`` + exit 1."""


class ComposeError(Exception):
    """User-facing failure (bad compose file, refused operation, failed PVE call)."""


class CommandError(ComposeError):
    """An external command (pct, pvesh, skopeo, …) failed."""

    def __init__(self, cmd: list[str], returncode: int, output: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.output = output
        tail = f": {output.strip()[-600:]}" if output and output.strip() else ""
        super().__init__(f"{' '.join(cmd[:3])} … failed (exit {returncode}){tail}")


class Terminated(BaseException):
    """SIGTERM / SIGHUP (e.g. the SSH session dropped). A BaseException like
    KeyboardInterrupt, so it unwinds through refresh/apply cleanup handlers."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"terminated by signal {signum}")
