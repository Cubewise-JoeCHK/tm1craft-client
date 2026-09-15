"""PyInstaller entry script for the tm1craft-client Windows drop-in (#12).

A plain script beside the spec (Analysis takes script files; module:string
entries are murkier) whose whole job is handing off to
:func:`tm1craft_client.cli.main` — the argparse entrypoint that returns
the process exit code (0 ok, 1 failure, 2 usage/config).
"""

import sys

from tm1craft_client.cli import main

sys.exit(main())
