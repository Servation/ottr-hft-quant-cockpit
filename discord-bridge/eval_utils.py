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
import shutil
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

    orig_dir = pf_mod._DATA_DIR
    orig_file = pf_mod._PORTFOLIO_FILE
    orig_state = copy.deepcopy(portfolio._state)

    # Redirect BOTH the data dir and the state file to a throwaway temp DIR (never the
    # live data dir). save() does mkstemp(dir=_DATA_DIR) + os.replace() onto
    # _PORTFOLIO_FILE; keeping both inside the same temp dir means the atomic write
    # stays on one filesystem (no cross-device error) AND never churns a portfolio_*.tmp
    # or eval_portfolio_*.json into the live data dir on a failed/interrupted cleanup.
    # load()/save() read these module globals by name, so reassigning them redirects the
    # singleton and any new Portfolio() instance created inside the block.
    tmpdir = Path(tempfile.mkdtemp(prefix="eval_portfolio_"))
    pf_mod._DATA_DIR = tmpdir
    pf_mod._PORTFOLIO_FILE = tmpdir / "portfolio_state.json"

    try:
        if initial_state is not None:
            portfolio._state = copy.deepcopy(initial_state)
        portfolio.save()
        yield portfolio
    finally:
        pf_mod._DATA_DIR = orig_dir
        pf_mod._PORTFOLIO_FILE = orig_file
        portfolio._state = orig_state
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextlib.contextmanager
def isolated_meeting_memory():
    """
    Redirect the meeting-memory persistence (log, markdown vault, embedding index)
    to a throwaway temp directory for the duration of an eval, so running a meeting
    can NEVER append to the live data/meeting_log.json. Restored on exit.

    save() / save_meeting() / _save_index() read the bot.memory module globals by
    name, so reassigning them here transparently redirects every write done by the
    meeting_memory singleton (and any new instance) during the block.
    """
    from bot import memory as mem_mod

    orig = (mem_mod.DATA_DIR, mem_mod.LOG_PATH, mem_mod.VAULT_DIR, mem_mod.INDEX_PATH)
    # A single temp dir holds all four; memory.save() does mkstemp(dir=DATA_DIR)
    # then os.replace into it, so temp + destination share a filesystem (no
    # cross-device error).
    tmpdir = Path(tempfile.mkdtemp(prefix="eval_memory_"))
    mem_mod.DATA_DIR = tmpdir
    mem_mod.LOG_PATH = tmpdir / "meeting_log.json"
    mem_mod.VAULT_DIR = tmpdir / "vesper_vault"
    mem_mod.INDEX_PATH = tmpdir / "embeddings_index.json"
    try:
        yield
    finally:
        mem_mod.DATA_DIR, mem_mod.LOG_PATH, mem_mod.VAULT_DIR, mem_mod.INDEX_PATH = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextlib.contextmanager
def isolated_data(initial_state: dict | None = None):
    """
    Isolate BOTH the portfolio and the meeting memory to disposable temp storage.

    Use this around any eval that runs a meeting: a meeting both mutates the
    portfolio (via tool calls) and persists a meeting record, so isolating only one
    still leaks into the live data dir.
    """
    with isolated_portfolio(initial_state) as pf, isolated_meeting_memory():
        yield pf
