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
import os
import subprocess
import sys

import pytest


def pytest_sessionstart(session):
    """Regenerate every rack in the gateware tree. Generated cores are
    not committed, and the socket fixtures gbs-build benches that carry
    them."""
    regenerate = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "gateware", "regenerate")
    result = subprocess.run([regenerate], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gateware/regenerate failed:\n"
                           f"{result.stdout}{result.stderr}")


@pytest.fixture(scope="session", autouse=True)
def acrobe_lifecycle_drain():
    yield
    if "acrobe" in sys.modules:
        import acrobe
        asyncio.run(acrobe.shutdown())
