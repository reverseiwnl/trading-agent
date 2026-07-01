import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import common  # noqa: E402  (needs the src path above)


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Keep test runs out of the repo's real logs/ directory: any main() a test
    invokes writes its DEBUG file log under the test's tmp dir, and the file
    handler is detached afterwards so handlers never accumulate across tests
    (loggers are process-global) or keep writing into a stale tmp dir."""
    monkeypatch.setattr(common, "LOGS_DIR", tmp_path / "logs")
    yield
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("trading_agent.") and isinstance(logger, logging.Logger):
            for handler in [h for h in logger.handlers
                            if isinstance(h, logging.FileHandler)]:
                handler.close()
                logger.removeHandler(handler)
