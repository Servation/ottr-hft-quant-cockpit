"""
Shared helpers for the eval scripts.

The most important one is `isolated_portfolio`: a context manager that redirects
the global Portfolio singleton to a throwaway temp state file for the duration of
an eval, so running an eval can NEVER mutate the live data/portfolio_state.json.
The original in-memory state and the on-disk file path are restored on exit.
"""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def isolated_portfolio(initial_state: dict | None = None):
    """
    Point the portfolio persistence at a disposable temp file while the block runs.

    Usage:
        with isolated_portfolio():
            ... run a meeting / mutate the portfolio ...

    Any trades executed inside the block (directly or via meeting tool calls) are
    written to the temp file and discarded afterwards. The live portfolio file and
    the singleton's prior in-memory state are restored when the block exits.
    """
    from bot import portfolio as pf_mod
    from bot.portfolio import portfolio

    orig_file = pf_mod._PORTFOLIO_FILE
    orig_state = copy.deepcopy(portfolio._state)

    # Create the throwaway file in the SAME directory as the live file. save()
    # writes atomically via tempfile + os.replace(), which requires the temp and
    # destination to share a filesystem, so an isolated file on /tmp would fail
    # with a cross-device link error.
    state_dir = Path(orig_file).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="eval_portfolio_", dir=str(state_dir))
    os.close(fd)

    # load() / save() read the module global by name, so reassigning it here
    # transparently redirects all reads/writes for the singleton AND any new
    # Portfolio() instances created inside the block.
    pf_mod._PORTFOLIO_FILE = Path(tmp_path)

    try:
        if initial_state is not None:
            portfolio._state = copy.deepcopy(initial_state)
        portfolio.save()
        yield portfolio
    finally:
        pf_mod._PORTFOLIO_FILE = orig_file
        portfolio._state = orig_state
        try:
            os.remove(tmp_path)
        except OSError:
            pass
