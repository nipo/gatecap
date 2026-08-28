"""Suite-wide teardown: drain acrobe's lifecycle registry.

The acrobe CLI drains registered cleanups (USB contexts, background
threads) after every command; a bare interpreter importing acrobe as a
library must do it itself, or libusb's context survives into interpreter
shutdown and its event thread dies mid-call with a pthread_mutex_destroy
assertion (exit 134 after an otherwise green run). Only drains when a
test actually imported acrobe -- importing it here would start the very
adapter enumeration the drain exists to clean up.
"""

import asyncio
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def acrobe_lifecycle_drain():
    yield
    if "acrobe" in sys.modules:
        import acrobe
        asyncio.run(acrobe.shutdown())
