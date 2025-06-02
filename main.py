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

# --- Judgement Windows (difference from note.hit_time in seconds) ---
JUDGEMENT_WINDOWS = {
    "Perfect": 0.016,  # Marvelous/Perfect
    "Great":   0.064,  # Perfect/Great
    "Good":    0.097,  # Great/Good
    "Okay":    0.127,  # Good/Okay (or Bad)
    "Miss":    0.151  # Okay/Miss (anything later than this is a miss)
}
# Negative side of miss window (how early can you press and still miss / not affect a future note)
# This is also important for notes that pass the judgment line.
MISS_WINDOW_LATE = JUDGEMENT_WINDOWS["Miss"]
MISS_WINDOW_EARLY_PENALTY = 0.188  # How early a press is definitively not for the current note.

# Game configs
PREPARATION_TIME = 3  # Seconds before the game starts moving notes


class GameNote:
    def __init__(self, lane, note_type, hit_time, end_time=None):
        self.lane = lane
        self.note_type = note_type
        self.hit_time = hit_time  # Time when the note should be hit (center of judgment)
        self.end_time = end_time  # For hold notes
        self._length = int(20 if end_time is None else (end_time - hit_time) * NOTE_SPEED)
        self.canvas: Optional[GameCanvas] = None
        self.canvas_item_id = None
        self._time_at_last_update = 0.0  # Tracks game_time when this note was last updated or drawn

        self.is_hit = False  # Has this note been judged by a key press?
        self.is_missed = False  # Has this note been marked as a miss?
        self.judgement_result: Optional[str] = None  # e.g., "Perfect", "Miss"

    def get_x_coords(self):
        lane_width = self.canvas.lane_width
        x1 = self.lane * lane_width
        x2 = x1 + lane_width
        return x1, x2

    def get_y_coords(self, game_time):
        if self.canvas is not None and self.canvas.judgment_line_y is not None:
            # y2 is the bottom of the note, y1 is the top
            # A note's hit_time aligns its bottom with the judgment_line_y
            y2 = int((game_time - self.hit_time) * NOTE_SPEED + self.canvas.judgment_line_y)
            y1 = y2 - self._length
            return y1, y2

    def out_of_range(self, game_time):
        """
        Check if the note is within the visible range of the canvas.
        :param game_time: The game time to check against.
        :return: -1 if the note has not entered the visible area,
          0 if it is visible, 1 if it has moved off the bottom of the screen.
        """
        y1, y2 = self.get_y_coords(game_time)
        return -1 + (y2 > 0) + (y1 > self.canvas.winfo_height())

    def draw_on_canvas(self, canvas: "GameCanvas", game_time):
        self._time_at_last_update = game_time
        self.canvas = canvas
        if self.out_of_range(game_time) != 0:
            return

        x1, x2 = self.get_x_coords()
        padding = 2
        x1 += padding
        x2 -= padding
        y1, y2 = self.get_y_coords(game_time)  # Use current game_time for initial draw
        color = TAP_NOTE_COLOR if self.note_type == TAP_NOTE else HOLD_NOTE_COLOR

        self.canvas_item_id = canvas.create_rectangle(
            x1, y1, x2, y2, fill=color, outline=color, tags="note"
        )

    def update(self, game_time):
        # Check if note will be moved off-screen at current time
        if self.out_of_range(game_time) > 0:
            self.remove_from_canvas()
            return
        if self.canvas_item_id is None:
            return  # Ignore notes not on canvas

        # Calculate how much time has passed since the last update for this note
        time_elapsed = game_time - self._time_at_last_update
        # Calculate pixel movement based on this time delta
        pixel_movement = time_elapsed * NOTE_SPEED
        if pixel_movement > 1:  # Only move forward if there's a noticeable change
            self.canvas.move(self.canvas_item_id, 0, pixel_movement)
            self._time_at_last_update = game_time  # Update the time of this note's last move

    def judge_as_hit(self, judgement: str):
        self.is_hit = True
        self.judgement_result = judgement
        print(f"Lane {self.lane}: {self.judgement_result}! (Time: {self.hit_time:.3f})")
        self.remove_from_canvas()

    def judge_as_miss(self):
        self.is_missed = True  # Mark as missed
        self.is_hit = True  # Also mark as 'hit' in the sense that it's been processed
        self.judgement_result = "Miss"
        print(f"Lane {self.lane}: MISS! (Time: {self.hit_time:.3f})")
        self.remove_from_canvas()

    def remove_from_canvas(self):
        if self.canvas and self.canvas_item_id:
            self.canvas.delete(self.canvas_item_id)
            self.canvas_item_id = None


class GameCanvas(tk.Canvas):
    judgment_line_y = None
    lane_count: int
    lane_width: float

    def lane_configure(self, lane_count: int):
        self.lane_count = lane_count
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
        if self.beatmap_data['General']['Mode'] != 3:
            raise ValueError("This is not a mania beatmap!")

        self.lane_count = round(self.beatmap_data['Difficulty']['CircleSize'])
        self.key_bindings = self._get_default_key_bindings(self.lane_count)  # e.g. {'s':0, 'd':1 ...}

        self.canvas = GameCanvas(root, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, bg=LANE_COLOR)
        self.canvas.pack()
        root.update()
        self.canvas.lane_configure(self.lane_count)  # Pass actual lane_count
        self.canvas.draw_lanes()
        self.canvas.draw_judgment_line(100)  # y_pos from bottom

        self.pending_notes: list[GameNote] = []
        self.active_notes: deque[GameNote] = deque()  # Notes currently on or just removed from screen
        self._create_notes()

        self.game_task = None
        self.game_start_time = 0.0  # time.perf_counter() when the game notes start moving
        self.current_game_time = 0.0  # Time elapsed since game_start_time

        self._setup_input_bindings()
        self.judgement_display_tasks = []  # To manage judgement text display tasks

    @staticmethod
    def _get_default_key_bindings(num_lanes):
        # Example: For 4K: s,d,j,k. For 7K: s,d,f,space,j,k,l
        default_keys_7k = ['s', 'd', 'f', 'space', 'j', 'k', 'l']  # Common 7K layout
        default_keys_4k = ['d', 'f', 'j', 'k']  # Common 4K layout (adjust as needed)
        # Add more or make this configurable

        if num_lanes == 4:
            selected_keys = default_keys_4k
        elif num_lanes == 7:
            selected_keys = default_keys_7k
        else:
            raise NotImplementedError(f"Key bindings for {num_lanes}K are not implemented yet.")

        return {key_char: i for i, key_char in enumerate(selected_keys)}

    def _setup_input_bindings(self):
        def _on_key_press(event):
            if self.game_start_time == 0:
                return  # Game hasn't started properly

            lane = self.key_bindings.get(event.keysym)
            if lane is not None:
                self._process_hit(lane, self.current_game_time)

        for key_char in self.key_bindings.keys():
            self.root.bind(f"<KeyPress-{key_char}>", _on_key_press)
            # self.root.bind(f"<KeyRelease-{key_char}>", self._on_key_release) # For hold notes later

    # def _on_key_release(self, event): # Placeholder for hold notes
    #     if self.game_start_time == 0: return
    #     lane = self.key_bindings.get(event.keysym)
    #     if lane is not None:
    #         # print(f"Key released: {event.keysym} for lane {lane} at {self.current_game_time:.3f}")
    #         pass

    def _process_hit(self, lane: int, press_time: float):
        # Find the earliest unhit note in the correct lane that can be hit
        best_note_to_hit: Optional[GameNote] = None
        min_abs_delta = float('inf')

        for note in self.active_notes:
            if note.lane == lane and not note.is_hit and not note.is_missed:
                # Only consider notes that are "hittable"
                # (i.e., their hit_time is close to press_time)
                # A note is hittable if press_time is not too early before note.hit_time
                # and not too late after note.hit_time.
                # The positive side of the miss window defines "too late".
                # The negative side can be a bit more generous for trying to hit.
                if press_time > note.hit_time + MISS_WINDOW_LATE:
                    continue  # Pressed too late for this note, it should have been a miss or already passed

                # If pressed too early for this note, but there might be an earlier one.
                # We want the note whose hit_time is closest to the press_time.
                time_difference = press_time - note.hit_time
                abs_delta = abs(time_difference)

                # If this note is closer to the press time than previously found notes
                if abs_delta < min_abs_delta:
                    # And also check if we are not excessively early for *this* note
                    if time_difference > -MISS_WINDOW_EARLY_PENALTY:  # Not too early for this specific note
                        min_abs_delta = abs_delta
                        best_note_to_hit = note
                elif best_note_to_hit and note.hit_time < best_note_to_hit.hit_time and time_difference > -MISS_WINDOW_EARLY_PENALTY:
                    # This logic ensures we pick the one closest to the judgement line if multiple are in range
                    # Particularly, if we press slightly early, we want the one whose hit_time is coming up next.
                    # If we already have a candidate, and this new `note` is *earlier* (smaller hit_time)
                    # but still validly hittable by this press, it might be a better candidate if `best_note_to_hit` was much later.
                    # This can get complex; the simplest is often "first hittable note in the column".
                    # For now, the logic of `abs_delta < min_abs_delta` favors the one whose hit_time is closest to press_time.
                    pass

        if best_note_to_hit:
            time_difference = press_time - best_note_to_hit.hit_time
            abs_delta = abs(time_difference)

            judgement = "Miss"  # Default if it's outside all good windows but still considered "for this note"
            for j_type, window_val in JUDGEMENT_WINDOWS.items():
                if abs_delta <= window_val:
                    judgement = j_type
                    break  # Found the best judgement

            best_note_to_hit.judge_as_hit(judgement)
            self._display_judgement_text(judgement, best_note_to_hit.lane)
        else:
            # No note was found in a hittable window for this press in this lane.
            # This could be a "break" in combo, or just an empty press.
            # print(f"Empty press in lane {lane} at {press_time:.3f}")
            self._display_judgement_text("Break", lane, color="gray")  # Optional feedback for empty press
            pass

    async def _display_judgement_text_coro(self, text_item, duration):
        await asyncio.sleep(duration)
        if self.canvas.winfo_exists() and text_item in self.canvas.find_all():
            self.canvas.delete(text_item)

    def _display_judgement_text(self, text: str, lane: int, duration: float = 0.5, color: Optional[str] = None):
        if not self.canvas.winfo_exists():
            return

        x = (lane + 0.5) * self.canvas.lane_width
        y = self.canvas.judgment_line_y - 40  # Display above judgment line

        text_color = color
        if not text_color:
            if text == "Perfect":
                text_color = "gold"
            elif text == "Great":
                text_color = "lightgreen"
            elif text == "Good":
                text_color = "lightblue"
            elif text == "Okay":
                text_color = "orange"
            elif text == "Miss":
                text_color = "red"
            else:
                text_color = "white"

        text_item = self.canvas.create_text(x, y, text=text, fill=text_color, font=("Arial", 16, "bold"),
                                            tags="judgement_text")

        # Schedule its removal
        task = asyncio.create_task(self._display_judgement_text_coro(text_item, duration))
        self.judgement_display_tasks.append(task)
        # Clean up completed tasks (optional, asyncio might handle this if tasks are not held elsewhere)
        self.judgement_display_tasks = [t for t in self.judgement_display_tasks if not t.done()]

    def _create_notes(self):
        lane_count = self.canvas.lane_count
        hit_objects = self.beatmap_data['HitObjects']
        all_notes = []
        for obj in hit_objects:
            type_ = int(obj[3])
            is_hold = type_ & (1 << 7)
            is_tap = type_ & 1

            if is_hold:
                note_type = HOLD_NOTE_BODY
                end_time = int(obj[5].split(':', 1)[0]) / 1000
            elif is_tap:
                note_type = TAP_NOTE
                end_time = None
            else:
                continue  # Skip for other types of hit objects

            # From osu wiki: x determines the index of the column that the hold will be in.
            # It is computed by floor(x * columnCount / 512) and clamped between 0 and columnCount - 1
            x_osu = int(obj[0])
            lane = min(max(int(x_osu * lane_count / 512), 0), lane_count - 1)
            hit_time = int(obj[2]) / 1000
            all_notes.append(GameNote(lane=lane, note_type=note_type, hit_time=hit_time, end_time=end_time))

        all_notes.sort(key=lambda note: note.hit_time, reverse=True)
        self.pending_notes = all_notes

    def update_notes(self, game_time):
        # Add new notes from pending_notes to active_notes if they should be drawn
        while self.pending_notes:
            note_to_add = self.pending_notes[-1]
            # Attempt to draw the note
            note_to_add.draw_on_canvas(self.canvas, game_time)
            if note_to_add.out_of_range(game_time) < 0:
                break
            self.pending_notes.pop()
            if note_to_add.canvas_item_id is not None:
                self.active_notes.append(note_to_add)

        # Update positions of active notes and handle misses
        for note in self.active_notes:
            if note.is_hit or note.is_missed:  # Already processed by input or marked as miss
                continue

            note.update(game_time)  # Moves the note, updates its _time_at_last_update

            # Check for misses AFTER updating its position
            if not note.is_hit and game_time > note.hit_time + MISS_WINDOW_LATE:
                note.judge_as_miss()
                self._display_judgement_text("Miss", note.lane)

        # Clean up removed notes from the front of the deque
        while self.active_notes and self.active_notes[0].canvas_item_id is None:
            self.active_notes.popleft()

    async def game_loop(self):
        # Note should appear at the top before the song starts,
        # so we add a preparation time for both the game and the player.
        self.game_start_time = time.perf_counter() + PREPARATION_TIME  # Start time of the song

        while not self.destroyed:
            self.current_game_time = time.perf_counter() - self.game_start_time
            print(f"Current game time: {self.current_game_time:.3f} seconds")
            self.update_notes(self.current_game_time)
            await asyncio.sleep(1 / 240)  # Update game logic at a high rate, visuals will be capped by Tkinter/monitor

    async def main_loop(self, refresh=0.00):
        self.game_task = asyncio.create_task(self.game_loop())
        await super().main_loop(refresh)


if __name__ == '__main__':
    root = tk.Tk()
    beatmap1 = "571547 Gom (HoneyWorks) - Zen Zen Zense/Gom (HoneyWorks) - Zen Zen Zense (Antalf) [Kaito's 7K Hard].osu"
    beatmap2 = "146875 Nanamori-chu _ Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size)/Nanamori-chu  Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size) (Lokovodo) [EZ].osu"

    game = ManiaGame(root, beatmap1)
    game.run()
