"""`python -m tortoise_capture` -- same entry point as the `tct` script."""

import sys

from .cli import main

sys.exit(main())
