import tkinter as tk
import asyncio
import logging

from mymania import App


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    app = App()
    app.root.resizable(True, False)
    app.root.geometry(f"+10+10")

    try:
        app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.warning("Application interrupted.")
    finally:
        logging.info("Application event loop finished.")
