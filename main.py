import time
import tkinter as tk
import asyncio
from typing import Optional
from collections import deque

from mymania import AsyncTkHelper, parse_osu_beatmap

# --- Configuration ---
WINDOW_WIDTH = 500
WINDOW_HEIGHT = 700
NOTE_SPEED = 800  # Pixels per second
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
    "Great": 0.064,  # Perfect/Great
    "Good": 0.097,  # Great/Good
    "Okay": 0.127,  # Good/Okay (or Bad)
    "Miss": 0.151  # Okay/Miss (anything later than this is a miss)
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
        self.note_type = note_type  # TAP_NOTE or HOLD_NOTE_BODY
        self.hit_time = hit_time  # Target time for the head of the note
        self.end_time = end_time  # Target time for the tail of a hold note

        # Visual length
        if note_type == TAP_NOTE:
            self._length = 12
        else:  # HOLD_NOTE_BODY
            self._length = int((end_time - hit_time) * NOTE_SPEED)

        self.canvas: Optional[GameCanvas] = None
        self.canvas_item_id = None
        self._time_at_last_visual_update = 0.0  # For visual movement interpolation

        # --- Judgement & State ---
        self.is_judged = False  # True if any final judgement (hit or miss) has been made
        self.judgement_result: Optional[str] = None

        # For Hold Notes
        self.head_hit_error: Optional[float] = None  # For hold notes, abs error of head press
        self.tail_release_error: Optional[float] = None  # For hold notes, abs error of tail release
        self.is_holding = False  # Is the key currently considered pressed for this active hold note
        self.is_head_hit_successfully = False  # Was the head of the hold note hit within any valid window (not a miss)
        self.broken_hold = False  # Was the key released prematurely during the hold

    def get_x_coords(self):
        # ... (no change from your code)
        lane_width = self.canvas.lane_width
        x1 = self.lane * lane_width
        x2 = x1 + lane_width
        return x1, x2

    def get_y_coords(self, game_time):
        if self.canvas is not None and self.canvas.judgment_line_y is not None:
            y2 = int((game_time - self.hit_time) * NOTE_SPEED + self.canvas.judgment_line_y)
            y1 = y2 - self._length
            return y1, y2
        raise ValueError("Canvas or judgment line Y position is not set.")

    def out_of_visual_range(self, game_time) -> int:
        if not self.canvas:
            return 1  # No canvas, effectively off-screen
        y1, y2 = self.get_y_coords(game_time)
        # -1: above screen, 0: on screen, 1: below screen (top edge passed bottom)
        if y2 < 0:
            return -1  # Entirely above
        if y1 > self.canvas.winfo_height():
            return 1  # Entirely below
        return 0  # On screen or partially on screen

    def draw_on_canvas(self, game_time: float):
        """
        Creates the canvas item for the note if it's visually in range and doesn't exist.
        Assumes self.canvas is already set.
        """
        if self.is_judged:
            self.remove_from_canvas()  # Clean up if judged or canvas gone
            return

        # 1. Check if canvas is set
        # 2. This method is for initial creation. If item_id exists, update_visual_position handles it.
        # 3. Only create if it's currently in visual range

        if not self.canvas or self.canvas_item_id is not None or self.out_of_visual_range(game_time) != 0:
            return

        self._time_at_last_visual_update = game_time  # Set time reference for first positioning
        x1, x2 = self.get_x_coords()
        padding = 2
        x1_pad, x2_pad = x1 + padding, x2 - padding
        y1_draw, y2_draw = self.get_y_coords(game_time)  # Get current coordinates for drawing
        color = TAP_NOTE_COLOR if self.note_type == TAP_NOTE else HOLD_NOTE_COLOR

        self.canvas_item_id = self.canvas.create_rectangle(
            x1_pad, y1_draw, x2_pad, y2_draw,
            fill=color, outline=color, tags="note", state='normal'
        )

    def update_visual_position(self, game_time: float):
        """
        Moves an existing canvas item and handles its visibility (hide/show)
        if it moves off/on screen.
        """
        if self.is_judged:
            self.remove_from_canvas()
            return
        if not self.canvas or not self.canvas_item_id:
            return

        current_visual_status = self.out_of_visual_range(game_time)

        try:
            current_item_state = self.canvas.itemcget(self.canvas_item_id, 'state')
        except tk.TclError:  # Item might have been deleted externally or canvas closed
            self.canvas_item_id = None
            return

        if current_visual_status != 0:  # Note is visually off-screen (entirely above or below)
            if current_item_state == 'normal':
                self.canvas.itemconfig(self.canvas_item_id, state='hidden')
            # Even if hidden, keep its logical time reference somewhat updated if needed for y_coords,
            # though y_coords should primarily be driven by game_time vs hit_time.
            # self._time_at_last_visual_update = game_time # This might lead to large jumps if it reappears.
            # Better to let draw_on_canvas reset it when it becomes visible.
            return  # Don't attempt to move if hidden and determined to be off-screen
        else:  # Note is visually on-screen
            if current_item_state == 'hidden':
                self.canvas.itemconfig(self.canvas_item_id, state='normal')
                # When note becomes visible again, reset its drawing position and time anchor
                self._time_at_last_visual_update = game_time
                x1, x2 = self.get_x_coords()
                padding = 2
                x1_pad, x2_pad = x1 + padding, x2 - padding
                y1_draw, y2_draw = self.get_y_coords(game_time)
                self.canvas.coords(self.canvas_item_id, x1_pad, y1_draw, x2_pad, y2_draw)
                # No further movement needed in this tick if we just re-drew it / made it visible at current pos

        # Calculate movement based on time delta since last *visual* update when it *was* visible
        time_elapsed = game_time - self._time_at_last_visual_update
        pixel_movement = time_elapsed * NOTE_SPEED

        if abs(pixel_movement) >= 0.1:  # Threshold to apply move; can be 0 for max precision
            self.canvas.move(self.canvas_item_id, 0, pixel_movement)
            self._time_at_last_visual_update = game_time

    def update_visual_position(self, game_time):
        if self.is_judged or not self.canvas_item_id:
            self.remove_from_canvas()
            return

        if self.out_of_visual_range(game_time) != 0:
            if self.canvas.itemcget(self.canvas_item_id, 'state') == 'normal':
                self.canvas.itemconfig(self.canvas_item_id, state='hidden')
            self._time_at_last_visual_update = game_time  # Keep this updated even if hidden
            return
        else:  # Is visually on screen
            if self.canvas.itemcget(self.canvas_item_id, 'state') == 'hidden':
                self.canvas.itemconfig(self.canvas_item_id, state='normal')

        time_elapsed = game_time - self._time_at_last_visual_update
        pixel_movement = time_elapsed * NOTE_SPEED

        if abs(pixel_movement) >= 1:  # Threshold to move
            self.canvas.move(self.canvas_item_id, 0, pixel_movement)
            self._time_at_last_visual_update = game_time

    def _finalize_judgement(self, judgement: str):
        self.is_judged = True
        self.judgement_result = judgement
        print(f"Lane {self.lane} ({self.note_type}): {self.judgement_result}! (Hit: {self.hit_time:.3f}" +
              (f", End: {self.end_time:.3f}" if self.end_time else "") + ")")
        self.remove_from_canvas()

    def judge_tap_hit(self, judgement: str):
        if self.is_judged: return
        self._finalize_judgement(judgement)

    def judge_hold_head_hit(self, head_error_abs: float, head_judgement: str):
        if self.is_judged: return
        self.head_hit_error = head_error_abs
        self.is_head_hit_successfully = head_judgement != "Miss"  # Assuming Miss is passed if head is missed
        self.is_holding = self.is_head_hit_successfully  # Start holding only if head was a success
        # Don't call _finalize_judgement here, wait for release or miss
        print(f"Lane {self.lane} (HOLD HEAD): {head_judgement}! Error: {head_error_abs:.3f}s")
        # Visual feedback for head hit could be added here (e.g., change color of the note slightly)

    def judge_hold_complete(self, final_judgement: str):
        if self.is_judged: return
        self._finalize_judgement(final_judgement)

    def judge_as_miss(self):
        if self.is_judged: return
        self._finalize_judgement("Miss")
        # If it's a hold note and the head was missed, specific logic for hold miss might be needed too.

    def remove_from_canvas(self):
        if self.canvas and self.canvas_item_id:
            try:
                if self.canvas.winfo_exists():  # Check if canvas still exists
                    self.canvas.delete(self.canvas_item_id)
            except tk.TclError:  # Widget might be destroyed
                pass
            finally:
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

        self.overall_difficulty = self.beatmap_data['Difficulty']['OverallDifficulty']
        self._calculate_od_windows()  # New method to set self.od_judgement_windows_s etc.

        self.lane_count = round(self.beatmap_data['Difficulty']['CircleSize'])
        self.key_bindings = self._get_default_key_bindings(self.lane_count)

        self.canvas = GameCanvas(root, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, bg=LANE_COLOR)
        self.canvas.pack()
        root.update()
        self.canvas.lane_configure(self.lane_count)
        self.canvas.draw_lanes()
        self.canvas.draw_judgment_line(100)
        # time to judge_line + 0.5s buffer
        self.NOTE_ACTIVATION_LEAD_TIME_S = (self.canvas.judgment_line_y / NOTE_SPEED) + 0.5
        self.pending_notes: list[GameNote] = []
        self.active_notes: deque[GameNote] = deque()
        self._create_notes()  # Uses self.note_factory or directly GameNote

        self.game_start_time = 0.0
        self.current_game_time = 0.0
        self.keys_currently_pressed_lanes: set[int] = set()  # Tracks active key presses by lane index

        self._setup_input_bindings()
        self.judgement_display_tasks = []
        self.game_task = None  # Initialized in main_loop

    def _calculate_od_windows(self):
        od = self.overall_difficulty

        # Base hit windows in milliseconds (defines ± error from exact time)
        # These are the values for PERFECT, GREAT, GOOD, OK, MEH judgments if a hit occurs.
        perfect_ms = 16.0  # Fixed for osu!mania
        great_ms = 64.0 - 3 * od
        good_ms = 97.0 - 3 * od
        ok_ms = 127.0 - 3 * od
        meh_ms = 151.0 - 3 * od
        # This defines the absolute earliest a press can interact with a note
        # Rule: "Hitting a note before the MISS window has no effect".
        miss_interaction_boundary_ms = 188.0 - 3 * od

        if great_ms <= perfect_ms:
            raise ValueError(
                f"Overall Difficulty {od} is too high. "
                f"'GREAT' window ({great_ms:.2f}ms) must be greater than 'PERFECT' window ({perfect_ms}ms)."
            )
        assert miss_interaction_boundary_ms > 0  # Should never be hit if previous check passes

        self.od_judgement_windows_ms = {
            "PERFECT": perfect_ms, "GREAT": great_ms, "GOOD": good_ms, "OK": ok_ms, "MEH": meh_ms,
            # This is not a "judgement name" like others but a boundary for hit registration.
            "MISS": miss_interaction_boundary_ms
        }

        # Convert to seconds for use in game logic
        self.od_judgement_windows_s = {k: v / 1000.0 for k, v in self.od_judgement_windows_ms.items()}

        # --- Define critical timing offsets for game logic based on the rules ---

        # Rule: "Hitting a note before the MISS window has no effect."
        # This offset is negative (for time *before* note.hit_time).
        # Uses the MISS_HIT_BOUNDARY (derived from 188 - 3*OD).
        self.no_effect_early_press_offset_s = -self.od_judgement_windows_s['MISS']

        # Rule: "not hitting a note will cause a miss after the OK window passes."
        # This offset is positive (for time *after* note.hit_time). This is for auto-missing *unhit* notes.
        self.auto_miss_if_unhit_offset_s = self.od_judgement_windows_s['OK']

    def _get_previous_window(self, current_key: str) -> str:
        order = ["PERFECT", "GREAT", "GOOD", "OK", "MEH"]
        idx = order.index(current_key)
        return order[idx - 1]

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
        # Using instance methods for handlers now
        for key_char in self.key_bindings.keys():
            self.root.bind(f"<KeyPress-{key_char}>", self._on_key_press_event)
            self.root.bind(f"<KeyRelease-{key_char}>", self._on_key_release_event)

    def _on_key_press_event(self, event):
        if self.game_start_time == 0:
            return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane not in self.keys_currently_pressed_lanes:  # Process only new presses
            self.keys_currently_pressed_lanes.add(lane)
            self._process_press(lane, self.current_game_time)

    def _on_key_release_event(self, event):
        if self.game_start_time == 0:
            return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane in self.keys_currently_pressed_lanes:
            self.keys_currently_pressed_lanes.remove(lane)
            self._process_release(lane, self.current_game_time)

    def _create_notes(self):
        lane_count = self.canvas.lane_count
        hit_objects = self.beatmap_data['HitObjects']
        all_notes = []
        for obj in hit_objects:
            type_ = int(obj[3])
            is_hold = type_ & (1 << 7)
            is_tap = type_ & 1
            x_osu = int(obj[0])
            lane = min(max(int(x_osu * lane_count / 512), 0), lane_count - 1)
            hit_time = int(obj[2]) / 1000

            if is_hold:
                note_type = HOLD_NOTE_BODY
                end_time = int(obj[5].split(':', 1)[0]) / 1000
                assert end_time > hit_time  # assume the beatmap is valid
            elif is_tap:
                note_type = TAP_NOTE
                end_time = None
            else:
                continue

            all_notes.append(GameNote(lane=lane, note_type=note_type, hit_time=hit_time, end_time=end_time))

        all_notes.sort(key=lambda note: note.hit_time, reverse=True)
        self.pending_notes = all_notes

    # --- Main Judgement Methods ---
    def _process_press(self, lane: int, press_time: float):
        best_note_to_hit: Optional[GameNote] = None

        # Iterate active_notes to find the earliest unjudged note in the correct lane
        # that this press could possibly interact with.
        for note in self.active_notes:
            if note.lane == lane and not note.is_judged:
                # For hold notes, if head is successfully hit and we are waiting for release,
                # this new press in the same lane should not re-judge the head.
                if note.note_type == HOLD_NOTE_BODY and note.is_head_hit_successfully:
                    continue

                time_difference = press_time - note.hit_time

                # Check if the press is within the widest possible interaction window for this note.
                # Earliest interaction: press_time >= note.hit_time + self.no_effect_early_press_offset_s
                # Latest interaction: press_time <= note.hit_time + self.od_judgement_windows_s['MISS_HIT_BOUNDARY']
                if self.no_effect_early_press_offset_s <= time_difference <= self.od_judgement_windows_s['MISS']:
                    # This note is a candidate. Since active_notes are processed in order,
                    # the first such candidate is the one we want.
                    best_note_to_hit = note
                    break

        if best_note_to_hit:
            note = best_note_to_hit
            time_difference = press_time - note.hit_time  # Positive if late, negative if early
            abs_error_s = abs(time_difference)

            # Determine judgement based on OD windows
            press_judgement = "Miss"  # Default

            if abs_error_s <= self.od_judgement_windows_s["PERFECT"]:
                press_judgement = "PERFECT"
            elif abs_error_s <= self.od_judgement_windows_s["GREAT"]:
                press_judgement = "GREAT"
            elif abs_error_s <= self.od_judgement_windows_s["GOOD"]:
                press_judgement = "GOOD"
            elif abs_error_s <= self.od_judgement_windows_s["OK"]:
                press_judgement = "OK"
            elif abs_error_s <= self.od_judgement_windows_s["MEH"]:
                press_judgement = "MEH"
            # Else, it remains "Miss" (as it's within MISS_HIT_BOUNDARY but > MEH)

            if note.note_type == TAP_NOTE:
                note.judge_tap_hit(press_judgement)
                self._display_judgement_text(note.judgement_result, note.lane)
            elif note.note_type == HOLD_NOTE_BODY:
                # For hold notes, this press is for the head.
                if press_judgement != "Miss":
                    note.judge_hold_head_hit(abs_error_s, press_judgement)
                    # Display head hit judgement, maybe distinct or simpler
                    self._display_judgement_text(f"H:{press_judgement}", note.lane)
                else:  # Head press was a miss for the hold note
                    note.judge_as_miss()  # The whole hold note is missed
                    self._display_judgement_text(note.judgement_result, note.lane)
        else:
            # No suitable unjudged note found for this press (empty press or too far off for any note)
            self._display_judgement_text("Break", lane, color="gray")

    def _process_release(self, lane: int, release_time: float):
        active_hold_note_in_lane: Optional[GameNote] = None
        for note in self.active_notes:
            if note.lane == lane and note.note_type == HOLD_NOTE_BODY and \
                    note.is_head_hit_successfully and note.is_holding and not note.is_judged:
                active_hold_note_in_lane = note
                break

        if active_hold_note_in_lane:
            note = active_hold_note_in_lane
            note.is_holding = False  # Mark that player is no longer physically holding the key for this note

            # Check for premature release (broken hold)
            # A hold is broken if released before the *start* of the tail's MEH window.
            # (i.e., release_time < note.end_time - MEH_window_for_tail)
            # The rule "Releasing the key during the hold note body will prevent judgements higher than MEH."
            # applies if the key is released at any point before the very end of the hold note's tail judgment window.
            # For simplicity, if released before note.end_time (target), mark as potentially broken for capping later.
            if release_time < note.end_time - self.od_judgement_windows_s['MEH']:  # Released too early, clearly broken
                note.broken_hold = True
                print(
                    f"Lane {note.lane} HOLD BROKEN significantly early at {release_time:.3f}s (tail target {note.end_time:.3f}s)")

            # Calculate tail release error relative to note.end_time
            # The release should be within the general interaction window of the tail
            # (e.g., note.end_time +/- MISS_HIT_BOUNDARY)
            tail_time_difference = release_time - note.end_time
            if abs(tail_time_difference) <= self.od_judgement_windows_s['MISS']:
                note.tail_release_error = abs(tail_time_difference)
            else:  # Release was way too early or way too late relative to tail target
                note.tail_release_error = self.od_judgement_windows_s['MISS'] + 0.001  # Assign a very large error
                note.broken_hold = True  # If release is outside any reasonable tail window, consider it a break.
                print(
                    f"Lane {note.lane} HOLD TAIL release at {release_time:.3f}s was outside interaction window of tail {note.end_time:.3f}s")

            self._judge_completed_hold_note(note)

    def _judge_completed_hold_note(self, note: GameNote):
        if note.is_judged or note.note_type != HOLD_NOTE_BODY or not note.is_head_hit_successfully:
            return

            # Ensure head_hit_error is set (should be by judge_hold_head_hit)
        if note.head_hit_error is None:
            # This case should ideally not be reached if head was processed correctly
            note.judge_as_miss()  # Failsafe
            self._display_judgement_text(note.judgement_result, note.lane)
            return

        # If tail_release_error is not set (e.g., auto-judged due to time passing without release),
        # it should have been set by update_notes before calling this.
        # For now, if it's None, assume a very bad release.
        if note.tail_release_error is None:
            note.tail_release_error = self.od_judgement_windows_s['MISS']  # Penalize heavily
            note.broken_hold = True  # If no explicit release was processed and we are here, something is off.

        # Apply osu!mania hold note judgement rules from the wiki
        final_judgement = "MEH"  # Default before checking better conditions

        p_win = self.od_judgement_windows_s["PERFECT"]
        g_win = self.od_judgement_windows_s["GREAT"]
        gd_win = self.od_judgement_windows_s["GOOD"]
        ok_win = self.od_judgement_windows_s["OK"]
        # MEH window is self.od_judgement_windows_s["MEH"]

        combined_error = note.head_hit_error + note.tail_release_error

        # Rule: "MISS: Not having the key pressed from the tail's early MEH window start to late OK window end"
        # This is handled by auto-miss logic in update_notes if the hold is abandoned.
        # If we reach here via a release or time-based completion, we try to score it.

        if note.head_hit_error <= p_win * 1.2 and combined_error <= p_win * 2.4:
            final_judgement = "PERFECT"
        elif note.head_hit_error <= g_win * 1.1 and combined_error <= g_win * 2.2:
            final_judgement = "GREAT"
        elif note.head_hit_error <= gd_win * 1.0 and combined_error <= gd_win * 2.0:
            final_judgement = "GOOD"
        elif note.head_hit_error <= ok_win * 1.0 and combined_error <= ok_win * 2.0:
            final_judgement = "OK"
        # MEH is the fallback if none of the above are met.

        # Rule: "Releasing the key during the hold note body will prevent judgements higher than MEH."
        if note.broken_hold:
            if final_judgement in ["PERFECT", "GREAT", "GOOD", "OK"]:
                final_judgement = "MEH"

        note.judge_hold_complete(final_judgement)
        self._display_judgement_text(note.judgement_result, note.lane)

    # In ManiaGame class:

    def update_notes(self, game_time: float):
        # 1. Activate pending notes: Move from pending_notes to active_notes
        #    Notes are activated if their hit_time is approaching.
        while self.pending_notes:
            # Peek at the note that would be processed next (earliest hit_time, at end of list for pop)
            note_to_consider_activating = self.pending_notes[-1]

            if note_to_consider_activating.hit_time <= game_time + self.NOTE_ACTIVATION_LEAD_TIME_S:
                # This note's showtime is approaching. Activate it.
                note = self.pending_notes.pop()

                # IMPORTANT: Set canvas reference on the note object.
                note.canvas = self.canvas

                self.active_notes.append(note)
            else:
                # The earliest pending note is still too far in the future. Stop checking.
                break

        # 2. Update active notes: visuals, auto-misses, hold logic
        for note in list(self.active_notes):  # Iterate over a copy if a note might be removed from active_notes
            if note.is_judged:
                # If judged, it should have called remove_from_canvas itself.
                # No further processing needed for this note.
                continue

            # Ensure canvas is set (should be if it came from pending_notes processing above)
            if not note.canvas:
                note.canvas = self.canvas

            # A. Handle drawing and visual updates:
            if not note.canvas_item_id:
                # Note is active but not yet on canvas (e.g., just activated). Try to draw it.
                # draw_on_canvas will create the item if it's visually in range.
                # It also sets _time_at_last_visual_update upon successful drawing.
                note.draw_on_canvas(game_time)

            if note.canvas_item_id:
                # If it has a canvas item (either just created or existed), update its position.
                # update_visual_position handles movement and hiding/showing if it goes off edges.
                note.update_visual_position(game_time)

            # B. Auto-Miss Logic for Tap Notes and Hold Note Heads (if still not judged by player)
            if not note.is_judged:  # Double-check, as it might have been judged by player input between game ticks
                is_head_miss_candidate = (note.note_type == TAP_NOTE) or \
                                         (note.note_type == HOLD_NOTE_BODY and not note.is_head_hit_successfully)

                if is_head_miss_candidate:
                    if game_time > note.hit_time + self.auto_miss_if_unhit_offset_s:  # Past OK window for head
                        note.judge_as_miss()  # This method also calls remove_from_canvas
                        self._display_judgement_text("Miss", note.lane)
                        continue  # Done with this note

                # C. Hold Note specific update logic (if head was hit and not yet fully judged)
                if note.note_type == HOLD_NOTE_BODY and note.is_head_hit_successfully and not note.is_judged:
                    # Check for broken hold if player released key prematurely
                    if note.is_holding and (note.lane not in self.keys_currently_pressed_lanes):
                        if game_time < note.end_time - self.od_judgement_windows_s[
                            'MEH']:  # Released before tail MEH window even starts
                            note.broken_hold = True
                        note.is_holding = False
                        print(f"Lane {note.lane} HOLD BROKEN (key release detected in update) at {game_time:.3f}s")

                    # Auto-judge hold note tail if time has passed significantly
                    if game_time > note.end_time + self.auto_miss_if_unhit_offset_s:  # Past tail's OK window
                        if not note.is_judged:  # If not already judged by an explicit release
                            print(f"Lane {note.lane} HOLD TAIL auto-judging past OK window at {game_time:.3f}s")

                            is_key_pressed_for_tail_end = (note.lane in self.keys_currently_pressed_lanes and
                                                           game_time >= note.end_time + self.no_effect_early_press_offset_s and  # From start of tail MISS_HIT_BOUNDARY
                                                           game_time <= note.end_time + self.auto_miss_if_unhit_offset_s)  # To end of tail OK window

                            if note.broken_hold or not is_key_pressed_for_tail_end:
                                note.tail_release_error = self.od_judgement_windows_s['MISS'] + 0.001  # Penalize
                            else:  # Still holding properly through the tail's OK window
                                note.tail_release_error = 0.0  # Assume perfect release if held through

                            self._judge_completed_hold_note(note)
                            continue  # Judged

        # 3. Clean up judged notes from the main active_notes deque
        self.active_notes = deque(note for note in self.active_notes if not note.is_judged)

    async def _display_judgement_text_coro(self, text_item, duration):
        await asyncio.sleep(duration)
        # Check if canvas and text_item still exist before deleting
        if self.canvas and not self.destroyed:
            try:
                # Check if item is still valid; find_all might be safer if tags are used
                if text_item in self.canvas.find_withtag("judgement_text"):
                    self.canvas.delete(text_item)
            except tk.TclError:
                pass  # Widget might have been destroyed

    def _display_judgement_text(self, text: str, lane: int, duration: float = 0.5, color: Optional[str] = None):
        # ... (your existing display logic is good, just ensure canvas checks if used in async coro)
        if not self.canvas or not self.canvas.winfo_exists():
            return

        x = (lane + 0.5) * self.canvas.lane_width
        y = self.canvas.judgment_line_y - 40

        text_color = color
        if not text_color:  # Default colors
            if text == "PERFECT":
                text_color = "gold"
            elif text == "GREAT":
                text_color = "lightgreen"
            elif text == "GOOD":
                text_color = "lightblue"
            elif text == "OK":
                text_color = "orange"
            elif text == "MEH":
                text_color = "purple"  # Added MEH color
            elif text == "Miss":
                text_color = "red"
            else:
                text_color = "white"  # For "Break" etc.

        text_item = self.canvas.create_text(x, y, text=text, fill=text_color, font=("Arial", 16, "bold"),
                                            tags="judgement_text")  # Added tag

        task = asyncio.create_task(self._display_judgement_text_coro(text_item, duration))
        self.judgement_display_tasks.append(task)
        self.judgement_display_tasks = [t for t in self.judgement_display_tasks if not t.done()]

    async def game_loop(self):
        # Note should appear at the top before the song starts,
        # so we add a preparation time for both the game and the player.
        self.game_start_time = time.perf_counter() + PREPARATION_TIME  # Start time of the song

        while not self.destroyed:
            self.current_game_time = time.perf_counter() - self.game_start_time
            print(f"Current game time: {self.current_game_time:.3f} seconds")
            self.update_notes(self.current_game_time)
            await asyncio.sleep(1 / 240)  # High update rate for logic

    async def main_loop(self, refresh=1 / 120):  # Default refresh for Tkinter if needed
        # This ensures game_task is created after the event loop starts and before tkinter loop runs.
        if not self.game_task or self.game_task.done():
            self.game_task = asyncio.create_task(self.game_loop())
        await super().main_loop(refresh)


if __name__ == '__main__':
    root = tk.Tk()
    beatmap1 = "571547 Gom (HoneyWorks) - Zen Zen Zense/Gom (HoneyWorks) - Zen Zen Zense (Antalf) [Kaito's 7K Hard].osu"
    beatmap2 = "146875 Nanamori-chu _ Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size)/Nanamori-chu  Goraku-bu - Yuriyurarararayuruyuri Daijiken (TV Size) (Lokovodo) [EZ].osu"

    game = ManiaGame(root, beatmap2)
    game.run()
