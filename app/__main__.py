"""Точка входа: python -m app bot | worker"""

import argparse
import asyncio

from app.config import get_settings
from app.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("component", choices=["bot", "worker"], help="что запустить")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.component == "bot":
        from app.bot.app import run_bot

        asyncio.run(run_bot(settings))
    else:
        from app.worker.main import run_worker

        asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
