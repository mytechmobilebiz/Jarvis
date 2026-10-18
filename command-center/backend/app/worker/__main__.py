"""Run the worker: python -m app.worker"""

from __future__ import annotations

import asyncio
import logging
import signal

from ..bootstrap import bootstrap
from ..config import get_settings
from ..db import dispose_engine
from .runner import Worker


async def main() -> None:
    settings = get_settings()
    await bootstrap(settings)
    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
