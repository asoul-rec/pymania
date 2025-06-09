import time
import tkinter as tk
import asyncio
from pathlib import Path
from tkinter import ttk
from typing import Optional
from collections import deque
import logging

import av
import sounddevice as sd

# Assuming these are in a 'mymania' subfolder or your PYTHONPATH
from mymania import AsyncTkHelper, parse_osu_beatmap, AudioPlayer
from mymania.audio import AudioFile
from mymania.beatmap import scan_dir

# --- Configuration ---
WINDOW_WIDTH = 500
SIDEBAR_WIDTH = 500
WINDOW_HEIGHT = 700
NOTE_SPEED = 1200  # Pixels per second

# --- Note & Color Constants ---
TAP_NOTE = "TAP"
HOLD_NOTE_BODY = "HOLD_BODY"
LANE_COLOR = "#333333"
LINE_COLOR = "#555555"
TAP_NOTE_COLOR = "cyan"
HOLD_NOTE_COLOR = "magenta"
JUDGMENT_LINE_COLOR = "red"


class GameNote:
    def __init__(self, lane, note_type, hit_time, hit_sound, end_time=None):
        self.lane = lane
        self.note_type = note_type
        self.hit_time = hit_time
        self.hit_sound = hit_sound
        self.end_time = end_time
        self._length = 12 if note_type == TAP_NOTE else int((end_time - hit_time) * NOTE_SPEED)
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
            y2 = int((game_time - self.hit_time) * NOTE_SPEED + y_offset)
            y1 = y2 - self._length
            return y1, y2
        return None

    def _get_padded_drawing_bounds(self, game_time: float) -> Optional[tuple[float, float, float, float]]:
        lane_x = self.get_x_coords()
        note_y = self.get_y_coords(game_time)
        if lane_x and note_y:
            return lane_x[0], note_y[0], lane_x[1], note_y[1]
        return None

    def draw_on_canvas(self, game_time: float):
        if self.is_judged or self.canvas_item_id or not self.canvas: return
        bounds = self._get_padded_drawing_bounds(game_time)
        if not bounds: return
        x1_pad, y1_draw, x2_pad, y2_draw = bounds
        canvas_height = self.canvas.winfo_height()
        is_vertically_visible = y2_draw > 0 and y1_draw < canvas_height
        if is_vertically_visible:
            self._time_at_last_visual_update = game_time
            color = TAP_NOTE_COLOR if self.note_type == TAP_NOTE else HOLD_NOTE_COLOR
            try:
                self.canvas_item_id = self.canvas.create_rectangle(
                    x1_pad, y1_draw, x2_pad, y2_draw,
                    fill=color, outline=color, tags="note"
                )
            except tk.TclError:
                self.canvas_item_id = None

    def update_visual_position(self, game_time: float):
        if self.is_judged or not self.canvas_item_id or not self.canvas or not self.canvas.winfo_exists(): return
        pixel_movement = (game_time - self._time_at_last_visual_update) * NOTE_SPEED
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

    def _finalize_judgement(self, judgement: str, time_difference: float = None):
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


class GameCanvas(tk.Canvas):
    judgment_line_y = None
    lane_count: int = 0
    lane_width: float = 0.0

    def lane_configure(self, lane_count: int):
        self.lane_count = lane_count
        if self.winfo_width() > 1:
            self.lane_width = self.winfo_width() / lane_count

    def draw_judgment_line(self, y_pos_from_bottom):
        if self.winfo_height() > 1:
            self.judgment_line_y = self.winfo_height() - y_pos_from_bottom
            self.create_line(
                0, self.judgment_line_y,
                self.winfo_width(), self.judgment_line_y,
                fill=JUDGMENT_LINE_COLOR, width=3, tags="judgment_line"
            )

    def draw_lanes(self):
        if self.lane_count > 0 and self.winfo_width() > 1:
            for i in range(1, self.lane_count):
                x = i * self.lane_width
                self.create_line(x, 0, x, self.winfo_height(), fill=LINE_COLOR, width=2)

    def clear(self):
        self.delete("all")


class ManiaGame:
    PREPARATION_TIME = 2

    def __init__(self, parent_app, canvas: GameCanvas, beatmap_path: str, settings: dict):
        self.app = parent_app
        self.canvas = canvas
        self.beatmap_path = Path(beatmap_path)
        self.settings = settings
        self.beatmap_data = parse_osu_beatmap(self.beatmap_path)
        if self.beatmap_data['General']['Mode'] != 3:
            raise ValueError("Not a mania beatmap!")
        self.overall_difficulty = self.beatmap_data['Difficulty']['OverallDifficulty']
        self._calculate_od_windows()

        self.lane_count = round(self.beatmap_data['Difficulty']['CircleSize'])
        self.song_file = self.beatmap_path.parent / self.beatmap_data['General']['AudioFilename']
        self.key_bindings = self._get_default_key_bindings(self.lane_count)

        self.canvas.lane_configure(self.lane_count)
        self.NOTE_ACTIVATION_LEAD_TIME_S = (600 / NOTE_SPEED) + 0.5  # Approx lead time

        self.pending_notes: list[GameNote] = []
        self.active_notes: deque[GameNote] = deque()
        self._create_notes()

        self.game_start_time = None
        self.keys_currently_pressed_lanes: set[int] = set()

        # Will be set by the App controller
        self.audio_player: Optional[AudioPlayer] = None
        # SFX data would be loaded and passed in here in a more advanced version
        # For now, we assume it's loaded in the App

    def prepare_canvas(self):
        self.canvas.clear()
        self.canvas.draw_lanes()
        self.canvas.draw_judgment_line(100)

    def current_game_time(self):
        if self.game_start_time is None: return -999
        return time.perf_counter() - self.game_start_time

    # (All other ManiaGame methods like _calculate_od_windows, _create_notes, _process_press, etc. go here)
    # The logic is mostly the same as in your file, but they will now use `self.app` to update UI.
    def _calculate_od_windows(self):
        # ... (Your existing method is good)
        od = self.overall_difficulty
        perfect_ms, great_ms, good_ms, ok_ms, meh_ms, miss_ms = 16.0, 64 - 3 * od, 97 - 3 * od, 127 - 3 * od, 151 - 3 * od, 188 - 3 * od
        if great_ms <= perfect_ms: raise ValueError(f"OD {od} is too high.")
        self.od_judgement_windows_s = {k: v / 1000.0 for k, v in {
            "PERFECT": perfect_ms, "GREAT": great_ms, "GOOD": good_ms, "OK": ok_ms, "MEH": meh_ms, "MISS": miss_ms
        }.items()}
        self.no_effect_early_press_offset_s = -self.od_judgement_windows_s['MISS']
        self.auto_miss_if_unhit_offset_s = self.od_judgement_windows_s['OK']

    def _get_default_key_bindings(self, num_lanes):
        keys = {4: ['d', 'f', 'j', 'k'], 7: ['s', 'd', 'f', 'space', 'j', 'k', 'l']}
        if num_lanes not in keys: raise NotImplementedError(f"{num_lanes}K not implemented.")
        return {key: i for i, key in enumerate(keys[num_lanes])}

    def _create_notes(self):
        # ... (Your existing _create_notes method)
        hit_objects = self.beatmap_data['HitObjects']
        all_notes = []
        for obj in hit_objects:
            type_, x_osu, hit_time, hit_sound = int(obj[3]), int(obj[0]), int(obj[2]) / 1000, int(obj[4])
            lane = min(max(int(x_osu * self.lane_count / 512), 0), self.lane_count - 1)
            is_hold, is_tap = type_ & 128, type_ & 1
            if is_hold:
                note_type, end_time = HOLD_NOTE_BODY, int(obj[5].split(':')[0]) / 1000
            elif is_tap:
                note_type, end_time = TAP_NOTE, None
            else:
                continue
            all_notes.append(GameNote(lane, note_type, hit_time, hit_sound, end_time))
        all_notes.sort(key=lambda n: n.hit_time, reverse=True)
        self.pending_notes = all_notes

    def _on_key_press(self, event):
        if self.game_start_time is None:
            return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane not in self.keys_currently_pressed_lanes:
            self.keys_currently_pressed_lanes.add(lane)
            self.app.play_sfx() # Example of calling app method
            self._process_press(lane, self.current_game_time())

    def _on_key_release(self, event):
        if self.game_start_time is None:
            return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane in self.keys_currently_pressed_lanes:
            self.keys_currently_pressed_lanes.remove(lane)
            self._process_release(lane, self.current_game_time())

    def _process_press(self, lane: int, press_time: float):
        # ... (Your existing judgement logic)
        # To update the UI, call the app's method
        # e.g. self.app.update_judgement_count(press_judgement)
        best_note = next((n for n in self.active_notes if n.lane == lane and not n.is_judged and not (
                    n.note_type == HOLD_NOTE_BODY and n.is_head_hit_successfully)), None)
        if not best_note: self.app.update_judgement_count("Break"); return

        time_diff = press_time - best_note.hit_time
        if not (self.no_effect_early_press_offset_s <= time_diff <= self.od_judgement_windows_s['MISS']):
            self.app.update_judgement_count("Break");
            return

        abs_error = abs(time_diff)
        judgement = "Miss"
        for j_type, window in sorted(self.od_judgement_windows_s.items(), key=lambda item: item[1]):
            if j_type != "MISS" and abs_error <= window: judgement = j_type; break

        self.app.update_judgement_count(judgement)
        if best_note.note_type == TAP_NOTE:
            best_note.judge_tap_hit(judgement, time_diff)
        else:
            best_note.judge_hold_head_hit(abs_error, judgement)

    def _process_release(self, lane: int, release_time: float):
        # ... (your existing logic)
        note = next((n for n in self.active_notes if
                     n.lane == lane and n.note_type == HOLD_NOTE_BODY and n.is_head_hit_successfully and n.is_holding and not n.is_judged),
                    None)
        if note:
            note.is_holding = False
            if release_time < note.end_time - self.od_judgement_windows_s['MEH']: note.broken_hold = True
            tail_diff = release_time - note.end_time
            if abs(tail_diff) <= self.od_judgement_windows_s['MISS']:
                note.tail_release_error = abs(tail_diff)
            else:
                note.tail_release_error = self.od_judgement_windows_s['MISS'] + 0.001; note.broken_hold = True
            self._judge_completed_hold_note(note)

    def _judge_completed_hold_note(self, note: GameNote):
        # ... (your existing logic)
        if note.is_judged or not note.is_head_hit_successfully: return
        if note.head_hit_error is None: note.judge_as_miss(); return
        if note.tail_release_error is None: note.tail_release_error = self.od_judgement_windows_s[
            'MISS']; note.broken_hold = True

        # simplified from your logic for brevity
        final_judgement = "MEH"  # Default
        p_win, g_win, gd_win, ok_win = (self.od_judgement_windows_s[j] for j in ["PERFECT", "GREAT", "GOOD", "OK"])
        combined_error = note.head_hit_error + note.tail_release_error
        if note.head_hit_error <= p_win * 1.2 and combined_error <= p_win * 2.4:
            final_judgement = "PERFECT"
        elif note.head_hit_error <= g_win * 1.1 and combined_error <= g_win * 2.2:
            final_judgement = "GREAT"
        elif note.head_hit_error <= gd_win and combined_error <= gd_win * 2:
            final_judgement = "GOOD"
        elif note.head_hit_error <= ok_win and combined_error <= ok_win * 2:
            final_judgement = "OK"
        if note.broken_hold and final_judgement != "MEH": final_judgement = "MEH"

        self.app.update_judgement_count(final_judgement)
        note.judge_hold_complete(final_judgement)

    def update_notes(self, game_time: float):
        # ... (your existing update_notes logic)
        while self.pending_notes and self.pending_notes[-1].hit_time <= game_time + self.NOTE_ACTIVATION_LEAD_TIME_S:
            note = self.pending_notes.pop()
            note.canvas = self.canvas
            self.active_notes.append(note)

        for note in list(self.active_notes):
            if note.is_judged: continue
            if not note.canvas_item_id: note.draw_on_canvas(game_time)
            if note.canvas_item_id: note.update_visual_position(game_time)

            is_head_miss = (note.note_type == TAP_NOTE) or (
                        note.note_type == HOLD_NOTE_BODY and not note.is_head_hit_successfully)
            if is_head_miss and game_time > note.hit_time + self.auto_miss_if_unhit_offset_s:
                note.judge_as_miss();
                self.app.update_judgement_count("Miss");
                continue

            if note.note_type == HOLD_NOTE_BODY and note.is_head_hit_successfully and not note.is_judged:
                if note.is_holding and (note.lane not in self.keys_currently_pressed_lanes):
                    if game_time < note.end_time - self.od_judgement_windows_s['MEH']: note.broken_hold = True
                    note.is_holding = False

                if game_time > note.end_time + self.auto_miss_if_unhit_offset_s:
                    self._judge_completed_hold_note(note)

        self.active_notes = deque(n for n in self.active_notes if not n.is_judged)


class SettingsSidebar(ttk.Frame):
    def __init__(self, parent, app_controller, **kwargs):
        super().__init__(parent, **kwargs)
        self.app = app_controller
        self.grid_columnconfigure(0, weight=1)
        audio_frame = ttk.LabelFrame(self, text="Audio Settings", padding=10)
        audio_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        audio_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(audio_frame, text="Host API:").grid(row=0, column=0, sticky="w")
        self.host_api_var = tk.StringVar()
        self.host_api_combo = ttk.Combobox(audio_frame, textvariable=self.host_api_var, state="readonly")
        self.host_api_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.host_api_combo.bind("<<ComboboxSelected>>", self.on_host_api_selected)
        ttk.Label(audio_frame, text="Device:").grid(row=1, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(audio_frame, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(audio_frame, text="Audio Offset (ms):").grid(row=2, column=0, sticky="w")
        self.offset_var = tk.IntVar(value=0)
        self.offset_spinbox = ttk.Spinbox(audio_frame, from_=-200, to=200, textvariable=self.offset_var, width=6)
        self.offset_spinbox.grid(row=2, column=1, sticky="w", pady=2)
        song_frame = ttk.LabelFrame(self, text="Song Selection", padding=10)
        song_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=5)
        song_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(song_frame, text="Song:").grid(row=0, column=0, sticky="w")
        self.song_var = tk.StringVar()
        self.song_combo = ttk.Combobox(song_frame, textvariable=self.song_var, state="readonly")
        self.song_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.song_combo.bind("<<ComboboxSelected>>", self.on_song_selected)
        ttk.Label(song_frame, text="Difficulty:").grid(row=1, column=0, sticky="w")
        self.diff_var = tk.StringVar()
        self.diff_combo = ttk.Combobox(song_frame, textvariable=self.diff_var, state="readonly")
        self.diff_combo.grid(row=1, column=1, sticky="ew", pady=2)
        controls_frame = ttk.Frame(self, padding=10)
        controls_frame.grid(row=2, column=0, sticky="ew", padx=5, pady=5)
        controls_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.start_button = ttk.Button(controls_frame, text="Start", command=self.app.start_game)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=2)
        self.pause_button = ttk.Button(controls_frame, text="Pause", command=self.app.pause_game, state="disabled")
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=2)
        self.stop_button = ttk.Button(controls_frame, text="Stop", command=self.app.stop_game, state="disabled")
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=2)
        billboard_frame = ttk.LabelFrame(self, text="Results", padding=10)
        billboard_frame.grid(row=3, column=0, sticky="ew", padx=5, pady=5)
        billboard_frame.grid_columnconfigure(1, weight=1)

        self.judgement_vars = {
            "PERFECT": tk.IntVar(value=0), "GREAT": tk.IntVar(value=0),
            "GOOD": tk.IntVar(value=0), "OK": tk.IntVar(value=0),
            "MEH": tk.IntVar(value=0), "Miss": tk.IntVar(value=0),
            "Break": tk.IntVar(value=0)
        }

        row = 0
        for judge, var in self.judgement_vars.items():
            ttk.Label(billboard_frame, text=f"{judge}:").grid(row=row, column=0, sticky="w")
            ttk.Label(billboard_frame, textvariable=var).grid(row=row, column=1, sticky="e")
            row += 1
        self.populate_audio_devices()
        self.populate_songs()

    def populate_audio_devices(self):
        self.host_apis = sd.query_hostapis()
        self.host_api_combo['values'] = [api['name'] for api in self.host_apis]
        try:  # Set default to WASAPI if available
            default_api_index = [api['name'] for api in self.host_apis].index('Windows WASAPI')
            self.host_api_combo.current(default_api_index)
        except (ValueError, tk.TclError):
            self.host_api_combo.current(0)
        self.on_host_api_selected()

    def on_host_api_selected(self, event=None):
        selected_api_name = self.host_api_var.get()
        selected_api_info = next(api for api in self.host_apis if api['name'] == selected_api_name)
        devices = [sd.query_devices(i) for i in selected_api_info['devices']]
        output_devices = [d['name'] for d in devices if d['max_output_channels'] > 0]
        self.device_combo['values'] = output_devices

        try:  # Set default device for this API
            default_device_info = sd.query_devices(selected_api_info['default_output_device'])
            self.device_combo.set(default_device_info['name'])
        except (ValueError, tk.TclError):
            if output_devices:
                self.device_combo.current(0)
            else:
                self.device_combo.set("")

    def populate_songs(self):
        self.beatmaps = scan_dir("Songs")
        song_titles = list(self.beatmaps.keys())
        self.song_combo['values'] = song_titles
        if song_titles:
            self.song_combo.current(0)
            self.on_song_selected()

    def on_song_selected(self, event=None):
        selected_song = self.song_var.get()
        diffs = [Path(p).stem for p in self.beatmaps.get(selected_song, [])]
        self.diff_combo['values'] = diffs
        if diffs:
            self.diff_combo.current(0)
        else:
            self.diff_combo.set("")

    def get_selected_beatmap_path(self):
        song_title = self.song_var.get()
        diff_stem = self.diff_var.get()
        if not song_title or not diff_stem: return None

        for path_str in self.beatmaps[song_title]:
            if Path(path_str).stem == diff_stem:
                return path_str
        return None

    def reset_judgements(self):
        for var in self.judgement_vars.values():
            var.set(0)


# --- New Main App Class ---
class App(AsyncTkHelper):
    canvas: GameCanvas

    def __init__(self, root):
        self.root = root
        self.root.title("My Mania Game")
        self.bind_destroy()

        self.game_instance: Optional[ManiaGame] = None
        self.game_task: Optional[asyncio.Task] = None

        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = GameCanvas(main_frame, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, bg=LANE_COLOR)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sidebar = SettingsSidebar(main_frame, self, width=SIDEBAR_WIDTH)
        self.sidebar.pack(side=tk.RIGHT, fill=tk.Y)

        # Will be initialized when game starts
        self.audio_player: Optional[AudioPlayer] = None
        self.sfx_data = []  # To hold loaded SFX

    async def main_loop(self):
        # Override the main loop to handle game task cancellation on exit
        try:
            await super().main_loop()  # This runs Tkinter's event loop
            if self.game_task and not self.game_task.done():
                await self.game_task
        except asyncio.CancelledError:
            if self.game_task: self.game_task.cancel()
            if self.audio_player: await self.audio_player.stop_stream()
            raise

    def start_game(self):
        if self.game_task and not self.game_task.done():
            logging.info("Game is already running.")
            return

        beatmap_path = self.sidebar.get_selected_beatmap_path()
        if not beatmap_path:
            logging.error("No beatmap selected.")
            return

        self.sidebar.reset_judgements()
        self.sidebar.start_button.config(state="disabled")
        self.sidebar.pause_button.config(state="normal")
        self.sidebar.stop_button.config(state="normal")

        settings = {
            'audio_offset': self.sidebar.offset_var.get() / 1000.0,
            'host_api': self.sidebar.host_api_var.get(),
            'device': self.sidebar.device_var.get(),
        }

        self.game_instance = ManiaGame(self, self.canvas, beatmap_path, settings)
        self.game_task = asyncio.create_task(self._game_runner())

    async def _game_runner(self):
        self._tk_update_interval = 0.
        try:
            # --- Audio Setup ---
            api_name = self.game_instance.settings['host_api']
            device_name = self.game_instance.settings['device']
            api_info = next(api for api in sd.query_hostapis() if api['name'] == api_name)
            device_info = next(
                d for d in [sd.query_devices(i) for i in api_info['devices']] if d['name'] == device_name)

            self.audio_player = AudioPlayer(48000, sample_fmt='s16')
            self.audio_player.latency = 'low'

            # For exclusive mode, which is good for low latency
            extra_settings = None
            if 'wasapi' in api_name.lower():
                extra_settings = sd.WasapiSettings(exclusive=True)

            self.audio_player.start_stream(device=device_info['index'], extra_settings=extra_settings)

            # Pass player to game instance
            self.game_instance.audio_player = self.audio_player

            # Load SFX (placeholder, ideally load from beatmap folder)
            try:
                # Assuming audio.py's AudioFile is accessible
                af = AudioFile("drum-hitnormal.wav")
                await af.open(resampler=av.AudioResampler('fltp', 'stereo', 48000))
                self.sfx_data = await af.read(100_000)
            except Exception as e:
                logging.error(f"Could not load SFX: {e}")
                self.sfx_data = None  # Ensure it's None if loading fails

            # Load song
            await self.audio_player.load_song(str(self.game_instance.song_file), False)
            self.game_instance.prepare_canvas()
            self._bind_game_keys()

            # --- Main Game Loop ---
            self.game_instance.game_start_time = time.perf_counter() + self.game_instance.PREPARATION_TIME
            song_started = False
            while not self.destroyed:
                game_time = self.game_instance.current_game_time()

                if not self.audio_player.is_playing_song:
                    if song_started:
                        break  # Song ended
                    if game_time >= 0:
                        self.audio_player.resume_song()
                        song_started = True
                else:  # Sync visual time to audio time
                    if (song_start_time := self.audio_player.song_start_time) is not None:
                        final_start_time = song_start_time + self.game_instance.settings['audio_offset']
                        if abs(final_start_time - self.game_instance.game_start_time) > 0.002:
                            self.game_instance.game_start_time = final_start_time
                self.game_instance.update_notes(game_time)
                await asyncio.sleep(1 / 240)  # High-rate logic update

        except asyncio.CancelledError:
            logging.info("Game runner was cancelled.")
        except Exception as e:
            logging.error(f"Error during game loop: {e}", exc_info=True)
        finally:
            await self._cleanup_game()
            del self._tk_update_interval

    def stop_game(self):
        if self.game_task and not self.game_task.done():
            self.game_task.cancel()
        # Cleanup will be handled in the _game_runner's finally block

    def pause_game(self):
        # Pause/Resume is complex. This is a placeholder for future implementation.
        # It would involve pausing the game loop and audio stream state.
        logging.warning("Pause function is not yet implemented.")
        pass

    async def _cleanup_game(self):
        logging.info("Cleaning up game instance...")
        try:
            self._unbind_game_keys()
            self.sidebar.start_button.config(state="normal")
            self.sidebar.pause_button.config(state="disabled")
            self.sidebar.stop_button.config(state="disabled")
            self.canvas.clear()
        except tk.TclError:
            pass
        if self.audio_player:
            await self.audio_player.stop_stream()
            self.audio_player = None
        self.game_instance = None

    def _bind_game_keys(self):
        if self.game_instance:
            for key in self.game_instance.key_bindings:
                self.root.bind(f"<KeyPress-{key}>", self.game_instance._on_key_press)
                self.root.bind(f"<KeyRelease-{key}>", self.game_instance._on_key_release)

    def _unbind_game_keys(self):
        if self.game_instance:
            for key in self.game_instance.key_bindings:
                self.root.unbind(f"<KeyPress-{key}>")
                self.root.unbind(f"<KeyRelease-{key}>")

    def update_judgement_count(self, judgement: str):
        if judgement in self.sidebar.judgement_vars:
            self.sidebar.judgement_vars[judgement].set(self.sidebar.judgement_vars[judgement].get() + 1)

    def play_sfx(self):
        if self.audio_player and self.sfx_data:
            self.audio_player.play_sound_effect(self.sfx_data)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    app = App(root)
    try:
        app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.warning("Application interrupted.")
    finally:
        logging.info("Application event loop finished.")
