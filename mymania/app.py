import asyncio
import logging
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

import av
import sounddevice as sd

from .gui import GameCanvas, SettingsSidebar, AsyncTkHelper
from .game import ManiaGame
from .audio import AudioPlayer, AudioFile

# --- Configuration ---
WINDOW_WIDTH = 500
SIDEBAR_WIDTH = 300  # Adjusted for more space
WINDOW_HEIGHT = 700

LANE_COLOR = "#333333"

# --- New Judgment Text & Color Configuration ---
# Defines the text to display and the color(s) to use.
# A list of colors will be used to color each character of the text.
JUDGE_TEXT_CONFIG = {
    "PERFECT": {"text": "300", "colors": ["#B892FF", "#BBFF89", "#FF9A4F"], "size": 1.0},
    # Rainbow (Cyan, Magenta, Yellow)
    "GREAT": {"text": "300", "colors": ["#FFD700"], "size": 1},  # Gold
    "GOOD": {"text": "200", "colors": ["#32CD32"], "size": 1},  # LimeGreen
    "OK": {"text": "100", "colors": ["#1E90FF"], "size": 1},  # DodgerBlue
    "MEH": {"text": "50", "colors": ["#D3D3D3"], "size": 1},  # LightGray
    "Miss": {"text": "miss!", "colors": ["#FF4444"], "size": 1.0},  # Bright Red
    "_FONT_SIZE": 70,  # Default font size for the text
}


class App(AsyncTkHelper):
    canvas: GameCanvas

    def __init__(self, root=None):
        if root is None:
            root = tk.Tk()
        self.root = root
        self.root.title("My Mania Game")
        self.bind_destroy()

        self.game_instance: Optional[ManiaGame] = None
        self.game_task: Optional[asyncio.Task] = None

        main_frame = ttk.Frame(self.root)
        main_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas = GameCanvas(main_frame, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, bg=LANE_COLOR)
        self.canvas.pack(side=tk.LEFT, fill=tk.Y, expand=False)

        self.sidebar = SettingsSidebar(main_frame, self, width=SIDEBAR_WIDTH)
        self.sidebar.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.audio_player: Optional[AudioPlayer] = None
        self.sfx_data: Optional[list] = None
        self.current_judgement_task: Optional[asyncio.Task] = None

    async def main_loop(self):
        try:
            await super().main_loop()
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
        # self.sidebar.pause_button.config(state="normal")
        self.sidebar.stop_button.config(state="normal")
        settings = {
            'audio_offset': self.sidebar.offset_var.get() / 1000.0,
            'host_api': self.sidebar.host_api_var.get(),
            'device': self.sidebar.device_var.get(),
            'note_speed': self.sidebar.note_speed_var.get(),
        }
        self.game_instance = ManiaGame(self, self.canvas, beatmap_path, settings)
        self.game_task = asyncio.create_task(self._game_runner())

    async def _game_runner(self):
        self._tk_update_interval = 0.
        try:
            api_name = self.game_instance.settings['host_api']
            if api_name == 'Windows WASAPI (exclusive)':
                api_name = 'Windows WASAPI'
                extra_settings = sd.WasapiSettings(exclusive=True)
            elif api_name == 'Windows WASAPI':
                extra_settings = sd.WasapiSettings(exclusive=False)
            else:
                extra_settings = None
            device_name = self.game_instance.settings['device']
            api_info = next(api for api in sd.query_hostapis() if api['name'] == api_name)
            device_info = next(
                d for d in [sd.query_devices(i) for i in api_info['devices']] if d['name'] == device_name)
            self.audio_player = AudioPlayer(48000, sample_fmt='s16', latency='low')
            self.audio_player.start_stream(device=device_info['index'], extra_settings=extra_settings)
            self.game_instance.audio_player = self.audio_player

            try:
                sfx_file = AudioFile("drum-hitnormal.wav")
                await sfx_file.open(resampler=av.AudioResampler('fltp', 'stereo', 48000))
                self.sfx_data = await sfx_file.read(100_000)
            except Exception as e:
                logging.error(f"Could not load SFX: {e}")
                self.sfx_data = None

            await self.audio_player.load_song(str(self.game_instance.song_file), False)
            self.game_instance.prepare_canvas()
            self._bind_game_keys()

            self.game_instance.game_start_time = time.perf_counter() + self.game_instance.preparation_time
            song_started = False
            while not self.destroyed:
                game_time = self.game_instance.current_game_time()
                if not self.audio_player.is_playing_song:
                    if song_started:
                        break
                    if game_time >= 0:
                        self.audio_player.resume_song()
                        song_started = True
                else:
                    if (song_start_time := self.audio_player.song_start_time) is not None:
                        final_start_time = song_start_time + self.game_instance.settings['audio_offset']
                        if abs(final_start_time - self.game_instance.game_start_time) > 0.002:
                            self.game_instance.game_start_time = final_start_time
                self.game_instance.update_notes(game_time)
                await asyncio.sleep(1 / 240)

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

    # def pause_game(self):
    #     logging.warning("Pause function is not yet implemented.")

    async def _cleanup_game(self):
        logging.info("Cleaning up game instance...")
        try:
            self._unbind_game_keys()
            # **MODIFICATION**: Cancel the single current judgement task if it exists
            if self.current_judgement_task and not self.current_judgement_task.done():
                self.current_judgement_task.cancel()

            self.sidebar.start_button.config(state="normal")
            # self.sidebar.pause_button.config(state="disabled")
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
            var = self.sidebar.judgement_vars[judgement]
            var.set(var.get() + 1)

    def play_sfx(self):
        if self.audio_player and self.sfx_data:
            self.audio_player.play_sound_effect(self.sfx_data)

    def display_judgement(self, judgement: str):
        if not self.game_instance or judgement not in JUDGE_TEXT_CONFIG: return

        # **MODIFICATION**: Cancel the previous judgement task if it's still running
        if self.current_judgement_task and not self.current_judgement_task.done():
            self.current_judgement_task.cancel()

        config = JUDGE_TEXT_CONFIG[judgement]
        text = config["text"]
        colors = config["colors"]
        font_size = config["size"]

        x_center = self.canvas.winfo_width() / 2
        y = self.canvas.winfo_height() * 0.6

        # **MODIFICATION**: Store the new task so it can be cancelled later
        self.current_judgement_task = asyncio.create_task(
            self._display_judgement_coro(text, x_center, y, colors, font_size))

    async def _display_judgement_coro(self, text, x_center, y, colors, font_size):
        text_ids = []
        try:
            font_family = "Wumpus Mono"  # Use a monospaced font for consistent character width
            base_font_size = JUDGE_TEXT_CONFIG['_FONT_SIZE']
            font_size = int(base_font_size * font_size)
            char_width = base_font_size * 0.7  # fixed center position
            total_text_width = len(text) * char_width
            start_x = x_center - (total_text_width / 2)

            for i, char in enumerate(text):
                color = colors[i % len(colors)]
                char_x = start_x + (i * char_width) + (char_width / 2)

                text_id = self.canvas.create_text(char_x, y, text=char, fill=color,
                                                  font=(font_family, font_size, "bold"),
                                                  tags="judgement_text")
                text_ids.append(text_id)

            await asyncio.sleep(0.5)

        except (tk.TclError, asyncio.CancelledError):
            pass  # Task was cancelled (new judgement appeared) or widget destroyed
        finally:
            try:
                if self.canvas.winfo_exists():
                    for text_id in text_ids:
                        if self.canvas.find_withtag(text_id):
                            self.canvas.delete(text_id)
            except tk.TclError:
                pass
