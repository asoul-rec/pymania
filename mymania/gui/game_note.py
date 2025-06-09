from typing import Optional
import tkinter as tk
import logging
from .game_canvas import GameCanvas

# --- Note & Color Constants ---
TAP_NOTE = "TAP"
HOLD_NOTE_BODY = "HOLD_BODY"
TAP_NOTE_COLOR = "cyan"
HOLD_NOTE_COLOR = "magenta"


class GameNote:
    def __init__(self, lane, note_type, note_speed, hit_time, hit_sound, end_time=None):
        self.lane = lane
        self.note_type = note_type
        self.hit_time = hit_time
        self.hit_sound = hit_sound
        self.end_time = end_time
        self.note_speed = note_speed
        self._length = 12 if note_type == TAP_NOTE else int((end_time - hit_time) * note_speed)
        self.padding = 2
        self.canvas: Optional[GameCanvas] = None
        self.canvas_item_id = None
        self._time_at_last_visual_update = 0.0
        self.is_judged = False
        self.judgement_result: Optional[str] = None
        self.head_hit_error: Optional[float] = None
        self.tail_release_error: Optional[float] = None
        self.is_holding = False
        self.is_head_hit_successfully = False
        self.broken_hold = False

    def get_x_coords(self) -> Optional[tuple[float, float]]:
        if self.canvas is not None and (lane_width := self.canvas.lane_width) is not None:
            x1 = self.lane * lane_width
            x2 = x1 + lane_width
            return x1 + self.padding, x2 - self.padding
        return None

    def get_y_coords(self, game_time: float) -> Optional[tuple[float, float]]:
        if self.canvas is not None and (y_offset := self.canvas.judgment_line_y) is not None:
            y2 = int((game_time - self.hit_time) * self.note_speed + y_offset)
            y1 = y2 - self._length
            return y1, y2
        return None

    def _get_padded_drawing_bounds(self, game_time: float) -> Optional[tuple[float, float, float, float]]:
        lane_x, note_y = self.get_x_coords(), self.get_y_coords(game_time)
        if lane_x and note_y: return lane_x[0], note_y[0], lane_x[1], note_y[1]
        return None

    def draw_on_canvas(self, game_time: float):
        if self.is_judged or self.canvas_item_id or not self.canvas:
            return
        bounds = self._get_padded_drawing_bounds(game_time)
        if not bounds:
            return
        x1_pad, y1_draw, x2_pad, y2_draw = bounds
        is_vertically_visible = y2_draw > 0 and y1_draw < self.canvas.winfo_height()
        if is_vertically_visible:
            self._time_at_last_visual_update = game_time
            color = TAP_NOTE_COLOR if self.note_type == TAP_NOTE else HOLD_NOTE_COLOR
            try:
                self.canvas_item_id = self.canvas.create_rectangle(x1_pad, y1_draw, x2_pad, y2_draw, fill=color,
                                                                   outline=color, tags="note")
            except tk.TclError:
                self.canvas_item_id = None

    def update_visual_position(self, game_time: float):
        if self.is_judged or not self.canvas_item_id or not self.canvas or not self.canvas.winfo_exists(): return
        pixel_movement = (game_time - self._time_at_last_visual_update) * self.note_speed
        if abs(pixel_movement) >= 0.1:
            try:
                self.canvas.move(self.canvas_item_id, 0, pixel_movement)
                self._time_at_last_visual_update = game_time
            except tk.TclError:
                self.canvas_item_id = None
                return
        if self.canvas_item_id:
            try:
                coords = self.canvas.coords(self.canvas_item_id)
                if coords and coords[1] > self.canvas.winfo_height():
                    self.remove_from_canvas()
            except tk.TclError:
                self.canvas_item_id = None

    def remove_from_canvas(self):
        if self.canvas and self.canvas_item_id and self.canvas.winfo_exists():
            try:
                if self.canvas.find_withtag(self.canvas_item_id): self.canvas.delete(self.canvas_item_id)
            except tk.TclError:
                pass
            finally:
                self.canvas_item_id = None

    def _finalize_judgement(self, judgement: str, time_difference: Optional[float] = None):
        if self.is_judged: return
        self.is_judged = True
        self.judgement_result = judgement
        logging.info(f"Lane {self.lane} ({self.note_type}): {self.judgement_result}! (Hit: {self.hit_time:.3f})")
        self.remove_from_canvas()

    def judge_as_miss(self):
        self._finalize_judgement("Miss")

    def judge_tap_hit(self, judgement: str, time_difference: float):
        self._finalize_judgement(judgement, time_difference)

    def judge_hold_head_hit(self, head_error_abs: float, head_judgement: str):
        if self.is_judged or self.is_head_hit_successfully: return
        self.head_hit_error = head_error_abs
        self.is_head_hit_successfully = head_judgement != "Miss"
        self.is_holding = self.is_head_hit_successfully
        if head_judgement == "Miss": self._finalize_judgement("Miss")

    def judge_hold_complete(self, final_judgement: str):
        self._finalize_judgement(final_judgement)
