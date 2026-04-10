import asyncio
import inspect
import os
import tempfile

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run test in asyncio event loop")


def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_func):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(test_func(**kwargs))
        return True
    return None


# ---------------------------------------------------------------------------
# Per-test isolated state dir
# ---------------------------------------------------------------------------
#
# Every test gets its own ``MCPXY_STATE_DIR`` pointing at a fresh tempdir,
# plus a fresh randomly generated ``MCPXY_SECRETS_KEY``. Without this,
# every test shares the production default ``/var/lib/mcpxy`` and stomps
# on the same SQLite DB + secrets table — once one test writes a row
# the next one reads stale data, and a schema change in mid-suite
# leaves leftover tables in the wrong shape until someone manually
# wipes the file.
#
# Tests that *need* a particular state dir override this fixture
# explicitly via ``monkeypatch.setenv("MCPXY_STATE_DIR", ...)`` inside
# the test, which takes precedence because it runs after the fixture
# below sets it.


@pytest.fixture(autouse=True)
def _isolated_mcpxy_state_dir(monkeypatch):
    from cryptography.fernet import Fernet

    tmp = tempfile.TemporaryDirectory(prefix="mcpxy-test-state-")
    monkeypatch.setenv("MCPXY_STATE_DIR", tmp.name)
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    # ``MCPXY_DB_URL`` defaults to sqlite:///<state_dir>/mcpxy.db, so
    # clearing any leaked value from the parent shell guarantees the
    # state dir override actually picks up.
    monkeypatch.delenv("MCPXY_DB_URL", raising=False)
    try:
        yield tmp.name
    finally:
        tmp.cleanup()
