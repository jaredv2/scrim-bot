import asyncio
import logging
import multiprocessing
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("entrypoint")


def run_dashboard():
    from dashboard import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


def run_bot():
    from bot import main
    asyncio.run(main())


if __name__ == "__main__":
    dashboard_proc = multiprocessing.Process(target=run_dashboard, daemon=True)
    dashboard_proc.start()
    logger.info("Dashboard started on port 8080")

    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        dashboard_proc.terminate()
        dashboard_proc.join()
