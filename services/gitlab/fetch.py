#!/usr/bin/env python3
# =============================================================================
# services/gitlab/fetch.py — Entrada del CLI: catalog, branches, tags y archive de proyectos de GitLab
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitlab_fetch.cli import main  # noqa: E402

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
