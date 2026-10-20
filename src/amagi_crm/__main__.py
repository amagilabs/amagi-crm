"""Allow ``python -m amagi_crm``."""

import sys

from .cli import main

sys.exit(main())
