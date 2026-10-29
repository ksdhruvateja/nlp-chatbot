"""Standalone background worker (run as its own container/service in production).

    python -m scripts.worker
"""
from __future__ import annotations

import logging
import signal

from app import db
from app.pipeline.runner import Worker

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.migrate()
    worker = Worker(poll_s=1.0)
    for sig in (signal.SIGTERM, signal.SIGINT):        # ECS sends SIGTERM on deploy/scale-in
        signal.signal(sig, lambda *_: worker.stop())
    worker.start()
    worker.join()
