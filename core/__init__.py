"""
Shared logger. Everything the pipeline does is printed to the terminal that
runs `streamlit run app.py`, so you can follow a question step by step:

    12:01:07  QUESTION  total amount by category
    12:01:07  SQL try 1 SELECT category, SUM(...) ...
    12:01:07  DONE      ok, 4 rows, 1 attempt(s), 0.61s
"""

import logging
import sys

log = logging.getLogger("excelqa")
if not log.handlers:                       # Streamlit re-imports modules; add the handler once
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
