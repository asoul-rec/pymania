import asyncio
import threading
# import tkinter
from tkinter import Tk, Event, Canvas
from types import SimpleNamespace


class AsyncTkHelper:
    widgets: SimpleNamespace = None
    loop: asyncio.AbstractEventLoop = None
    destroyed = False
    root: Tk = None
    canvas: Canvas = None

    def run(self):
        asyncio.run(self.main_loop())

    def bind_destroy(self):
        def _destroy(event: Event):
            if event.widget == root:
                self.destroyed = True
                self.on_destroy()

        root = self.root
        root.bind('<Destroy>', _destroy)

    def update(self):
        self._invisible_refresh()
        self.root.update()

    def _invisible_refresh(self):
        if self.canvas is not None:
            if coords := self.canvas.coords("__invisible"):
                move = 300 if coords[3] < 200 else -1
                self.canvas.move("__invisible", 0, move)
            else:
                self.canvas.create_rectangle(0, 0, 500, 500, fill="", outline="", tags="__invisible")

    async def main_loop(self, refresh=1 / 60):
        self.loop = asyncio.get_running_loop()
        while not self.destroyed:
            self.update()
            await asyncio.sleep(refresh)

    @property
    def is_running(self):
        return self.loop is not None and self.loop.is_running()

    def on_destroy(self):
        pass
