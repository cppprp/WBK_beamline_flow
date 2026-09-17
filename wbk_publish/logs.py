"""Logging setup shared by both CLIs.

Everything goes to stdout so the SSH trigger and the batch scripts can capture a
run with a single redirect.
"""

import logging
import sys


def make_logger(name: str, verbose: bool = True) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )

    log.addHandler(handler)
    log.propagate = False
    return log
