"""Shared logger: every pipeline step is printed to the terminal that runs the app."""

import logging
import sys

log = logging.getLogger("excel_qa")
if not log.handlers:                       # Streamlit re-imports modules, so add the handler once
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
