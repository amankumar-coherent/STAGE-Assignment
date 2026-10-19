"""STAGE "One User, One Agent": a per-viewer decide / verify / act / measure loop."""

__version__ = "1.0.0"

import logging as _logging

_logging.getLogger("stage_agent").addHandler(_logging.NullHandler())
