#!/usr/bin/env python3
"""Run the Python implementation from a checkout: ./pve-oci-compose.py [options] <command>."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pve_oci_compose.cli import main  # noqa: E402

sys.exit(main())
