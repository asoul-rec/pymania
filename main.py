import time
import tkinter as tk
import asyncio
from typing import Optional
from collections import deque

from mymania import AsyncTkHelper, parse_osu_beatmap

# --- Configuration ---
WINDOW_WIDTH = 500
WINDOW_HEIGHT = 700
NOTE_SPEED = 1000  # Pixels per second
# FPS = 60
# UPDATE_DELAY_MS = int(1000 / FPS)

# Note types
TAP_NOTE = "TAP"
HOLD_NOTE_START = "HOLD_START"  # For the head of a long note
HOLD_NOTE_BODY = "HOLD_BODY"  # For the tail/body of a long note

# Colors
LANE_COLOR = "#333333"
LINE_COLOR = "#555555"
TAP_NOTE_COLOR = "cyan"
HOLD_NOTE_COLOR = "magenta"
JUDGMENT_LINE_COLOR = "red"


class GameNote:
    def __init__(self, lane, note_type, hit_time, end_time=None):  # length for tap notes is just their visual height
        self.lane = lane
        self.type = note_type
        self.hit_time = hit_time  # Time when the note was pressed
        self.end_time = end_time
        # Length of the note in pixels
        self._length = int(20 if end_time is None else (end_time - hit_time) * NOTE_SPEED)
        self.canvas: Optional[GameCanvas] = None  # To store the canvas where the note is drawn
        self.canvas_item_id = None  # To store the ID of the drawn item on canvas
        self._time = None

    def get_x_coords(self):
        lane_width = self.canvas.lane_width
        x1 = self.lane * lane_width
        x2 = x1 + lane_width
        return x1, x2

    def get_y_coords(self, game_time=None):
        if game_time is None:
            game_time = self._time
        if game_time is not None and self.canvas is not None and self.canvas.judgment_line_y is not None:
            y2 = int((self._time - self.hit_time) * NOTE_SPEED + self.canvas.judgment_line_y)
            y1 = y2 - self._length
            return y1, y2

    def out_of_range(self, game_time):
        """Check if the note is within the visible range of the canvas."""
        y1, y2 = self.get_y_coords(game_time)
        return -1 + (y2 > 0) + (y1 > self.canvas.winfo_height())

    def draw_on_canvas(self, canvas: "GameCanvas", game_time):
        self._time = game_time
        self.canvas = canvas
        if self.out_of_range(game_time) != 0:
            return
        x1, x2 = self.get_x_coords()
        padding = 2
        x1 += padding
        x2 -= padding
        y1, y2 = self.get_y_coords()
        color = TAP_NOTE_COLOR if self.type == TAP_NOTE else HOLD_NOTE_COLOR

        # Add a small padding/margin for notes, so they don't touch lane lines
        self.canvas_item_id = canvas.create_rectangle(
            x1, y1, x2, y2, fill=color, outline=color, tags="note"
        )

    def update(self, game_time):
        pos_delta = (game_time - self._time) * NOTE_SPEED
        if pos_delta > 0.5:
            print(pos_delta)
            self._time = game_time
            if self.canvas is not None:
                if self.canvas_item_id is not None and self.out_of_range(game_time) > 0:
                    # print(self.get_y_coords(game_time), self.canvas.winfo_height())
                    # print(self.canvas.coords(self.canvas_item_id))
                    # If the note has moved past the bottom of the canvas, remove it
                    self.canvas.delete(self.canvas_item_id)
                    self.canvas_item_id = None
                else:
                    self.canvas.move(self.canvas_item_id, 0, pos_delta)


class GameCanvas(tk.Canvas):
    judgment_line_y = None
    lane_count: int
    lane_width: float

    def lane_configure(self, lane_count):
        self.lane_count = round(lane_count)
        self.lane_width = self.winfo_width() / lane_count

    def draw_judgment_line(self, y_pos):
        self.judgment_line_y = self.winfo_height() - y_pos

        self.create_line(
            0, self.judgment_line_y,
            WINDOW_WIDTH, self.judgment_line_y,
            fill=JUDGMENT_LINE_COLOR, width=3, tags="judgment_line"
        )
        self.create_text(
            WINDOW_WIDTH - 40, self.judgment_line_y - 15,
            text="JUDGE HERE", fill=JUDGMENT_LINE_COLOR, font=("Arial", 8)
        )

    def draw_lanes(self):
        for i in range(1, self.lane_count):
            x = i * self.lane_width
            self.create_line(x, 0, x, WINDOW_HEIGHT, fill=LINE_COLOR, width=2)


class ManiaGame(AsyncTkHelper):
    def __init__(self, root, beatmap_path):
        self.root = root
        self.root.title("Minimal Mania")
        self.root.geometry('+10+10')
        self.root.resizable(False, False)
        self.bind_destroy()
        self.beatmap_data = parse_osu_beatmap(beatmap_path)
        if self.beatmap_data['General']['Mode'] != 3:  # Mania mode
            raise ValueError("This is not a mania beatmap!")
        canvas = self.canvas = GameCanvas(root, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, bg=LANE_COLOR)
        canvas.pack()
        root.update()  # Ensure the canvas is properly sized before configuring lanes
        canvas.lane_configure(self.beatmap_data['Difficulty']['CircleSize'])
        canvas.draw_lanes()
        canvas.draw_judgment_line(100)
        self.pending_notes: list[GameNote] = []
        self.active_notes: deque[GameNote] = deque()
        self._create_notes()
        self.game_task = None

    def _create_notes(self):
        lane_count = self.canvas.lane_count
        hit_objects = self.beatmap_data['HitObjects']
        all_notes = []
        for obj in hit_objects:
            type_ = int(obj[3])
            if type_ & (1 << 7):  # If the note is a hold note
                note_type = HOLD_NOTE_BODY
                end_time = int(obj[5].split(':', 1)[0]) / 1000  # Convert milliseconds to seconds
            elif type_ & 1:
                note_type = TAP_NOTE
                end_time = None
            else:
                continue
            x = int(obj[0])
            # x determines the index of the column that the hold will be in.
            # It is computed by floor(x * columnCount / 512) and clamped between 0 and columnCount - 1
            lane = min(max(int(x * lane_count / 512), 0), lane_count - 1)
            hit_time = int(obj[2]) / 1000  # Convert milliseconds to seconds
            all_notes.append(GameNote(lane=lane, note_type=note_type, hit_time=hit_time, end_time=end_time))
        all_notes.sort(key=lambda note: note.hit_time, reverse=True)  # Sort notes by press time
        self.pending_notes = all_notes

    def update_notes(self, game_time):
        while self.pending_notes:
            new_note = self.pending_notes[-1]
            new_note.draw_on_canvas(self.canvas, game_time)
            if new_note.canvas_item_id is not None:
                # If the note was successfully drawn, move it to active notes
                self.pending_notes.pop()
                self.active_notes.append(new_note)
            else:
                break

        for note in self.active_notes:
            if note.canvas_item_id is None:
                continue
            note.update(game_time)
        while self.active_notes and self.active_notes[0].canvas_item_id is None:
            self.active_notes.popleft()

        # Optional: Remove inactive notes (or use an object pool for performance in a real game)
        # self.notes = [note for note in self.notes if note.is_active]

    async def game_loop(self):
        start_time = time.perf_counter() + 1  # 1 seconds preparation time
        while not self.destroyed:
            self.update_notes(time.perf_counter() - start_time)
            await asyncio.sleep(0.00)

    async def main_loop(self, refresh=0.00):
        self.game_task = asyncio.create_task(self.game_loop())
        await super().main_loop(refresh)


if __name__ == '__main__':
    root = tk.Tk()
    beatmap1 = "571547 Gom (HoneyWorks) - Zen Zen Zense/Gom (HoneyWorks) - Zen Zen Zense (Antalf) [Kaito's 7K Hard].osu"
    beatmap2 = "146875 Nanamori-chu _ Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size)/Nanamori-chu  Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size) (Lokovodo) [EZ].osu"

    game = ManiaGame(root, beatmap1)
    game.run()
