import asyncio
import logging
import multiprocessing
import os
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("entrypoint")


def _get_port() -> int:
    # Render/Heroku inject $PORT; fallback to DASHBOARD_PORT / settings / 8080
    for key in ("PORT", "DASHBOARD_PORT"):
        val = os.getenv(key)
        if val and val.strip().isdigit():
            return int(val.strip())
    try:
        from config import settings as _s

        return int(_s.dashboard_port)
    except Exception:
        return 8080


def run_dashboard():
    from dashboard import app

    port = _get_port()
    logger.info("Dashboard starting on 0.0.0.0:%s (PORT=%s DASHBOARD_PORT=%s)", port, os.getenv("PORT"), os.getenv("DASHBOARD_PORT"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_bot():
    from bot import main
    asyncio.run(main())


if __name__ == "__main__":
    port = _get_port()
    dashboard_proc = multiprocessing.Process(target=run_dashboard, daemon=True)
    dashboard_proc.start()
    logger.info("Dashboard started on port %s", port)

    bot_ok = True
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        bot_ok = False
        logger.error(f"Bot crashed: {e}", exc_info=True)
        # Keep dashboard alive even if bot crashes (e.g. DB down / bad token)
        # so the container stays responsive on $PORT for health checks.
        if dashboard_proc.is_alive():
            logger.info("Bot failed but dashboard still running — keeping container alive for health checks")
            try:
                dashboard_proc.join()
            except KeyboardInterrupt:
                pass
    finally:
        # Only tear down dashboard if bot exited cleanly or dashboard already died
        if bot_ok and dashboard_proc.is_alive():
            dashboard_proc.terminate()
            dashboard_proc.join()
