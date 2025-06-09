from tkinter.ttk import Frame, LabelFrame, Combobox, Spinbox, Button, Label
from tkinter import StringVar, IntVar, TclError
import sounddevice as sd
from pathlib import Path
from ..beatmap import scan_dir


class SettingsSidebar(Frame):
    def __init__(self, parent, app_controller, **kwargs):
        super().__init__(parent, **kwargs)
        self.app = app_controller
        self.grid_columnconfigure(0, weight=1)
        audio_frame = LabelFrame(self, text="Audio Settings", padding=10)
        audio_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        audio_frame.grid_columnconfigure(1, weight=1)
        Label(audio_frame, text="Host API:").grid(row=0, column=0, sticky="w")
        self.host_api_var = StringVar()
        self.host_api_combo = Combobox(audio_frame, textvariable=self.host_api_var, state="readonly", width=60)
        self.host_api_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.host_api_combo.bind("<<ComboboxSelected>>", self.on_host_api_selected)
        Label(audio_frame, text="Device:").grid(row=1, column=0, sticky="w")
        self.device_var = StringVar()
        self.device_combo = Combobox(audio_frame, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=1, column=1, sticky="ew", pady=2)
        Label(audio_frame, text="Audio Offset (ms):").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.offset_var = IntVar(value=0)
        self.offset_spinbox = Spinbox(audio_frame, from_=-200, to=200, textvariable=self.offset_var, width=6)
        self.offset_spinbox.grid(row=2, column=1, sticky="w", pady=2)
        song_frame = LabelFrame(self, text="Song Selection", padding=10)
        song_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=5)
        song_frame.grid_columnconfigure(1, weight=1)
        Label(song_frame, text="Song:").grid(row=0, column=0, sticky="w")
        self.song_var = StringVar()
        self.song_combo = Combobox(song_frame, textvariable=self.song_var, state="readonly")
        self.song_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.song_combo.bind("<<ComboboxSelected>>", self.on_song_selected)
        Label(song_frame, text="Difficulty:").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.diff_var = StringVar()
        self.diff_combo = Combobox(song_frame, textvariable=self.diff_var, state="readonly")
        self.diff_combo.grid(row=1, column=1, sticky="ew", pady=2)
        game_settings_frame = LabelFrame(self, text="Game Settings", padding=10)
        game_settings_frame.grid(row=2, column=0, sticky="ew", padx=5, pady=5)
        Label(game_settings_frame, text="Note Speed (pixels/sec):").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.note_speed_var = IntVar(value=1200)
        self.note_speed_spinbox = Spinbox(game_settings_frame, from_=100, to=2000, increment=100,
                                              textvariable=self.note_speed_var, width=8)
        self.note_speed_spinbox.grid(row=0, column=1, sticky="w", pady=2)
        controls_frame = Frame(self, padding=10)
        controls_frame.grid(row=3, column=0, sticky="ew", padx=5, pady=5)
        controls_frame.grid_columnconfigure((0, 1), weight=1)
        self.start_button = Button(controls_frame, text="Start", command=self.app.start_game)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=2)
        # self.pause_button = Button(controls_frame, text="Pause", command=self.app.pause_game, state="disabled")
        # self.pause_button.grid(row=0, column=1, sticky="ew", padx=2)
        self.stop_button = Button(controls_frame, text="Stop", command=self.app.stop_game, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=2)
        billboard_frame = LabelFrame(self, text="Results", padding=10)
        billboard_frame.grid(row=4, column=0, sticky="ews", padx=5, pady=5)
        billboard_frame.grid_columnconfigure(1, weight=1)
        self.judgement_vars = {"PERFECT": IntVar(value=0), "GREAT": IntVar(value=0), "GOOD": IntVar(value=0),
                               "OK": IntVar(value=0), "MEH": IntVar(value=0), "Miss": IntVar(value=0)}
        row = 0
        for judge, var in self.judgement_vars.items():
            Label(billboard_frame, text=f"{judge}:").grid(row=row, column=0, sticky="w")
            Label(billboard_frame, textvariable=var).grid(row=row, column=1, sticky="e")
            row += 1
        self.populate_audio_devices()
        self.populate_songs()

    def populate_audio_devices(self):
        self.host_apis = sd.query_hostapis()
        host_api_values = [api['name'] for api in self.host_apis]
        try:
            host_api_values.insert(host_api_values.index('Windows WASAPI') + 1, 'Windows WASAPI (exclusive)')
        except ValueError:
            pass
        self.host_api_combo['values'] = host_api_values

        try:  # Set default to WASAPI if available
            default_api_index = host_api_values.index('Windows WASAPI')
            self.host_api_combo.current(default_api_index)
        except (ValueError, TclError):
            self.host_api_combo.current(0)
        self.on_host_api_selected()

    def on_host_api_selected(self, event=None):
        selected_api_name = self.host_api_var.get()
        selected_api_name = 'Windows WASAPI' if selected_api_name == 'Windows WASAPI (exclusive)' else selected_api_name
        selected_api_info = next(api for api in self.host_apis if api['name'] == selected_api_name)
        devices = [sd.query_devices(i) for i in selected_api_info['devices']]
        output_devices = [d['name'] for d in devices if d['max_output_channels'] > 0]
        self.device_combo['values'] = output_devices
        try:
            default_device_info = sd.query_devices(selected_api_info['default_output_device'])
            self.device_combo.set(default_device_info['name'])
        except (ValueError, TclError):
            if output_devices:
                self.device_combo.current(0)
            else:
                self.device_combo.set("")

    def populate_songs(self):
        self.beatmaps = scan_dir("Songs") if Path("Songs").exists() else {}
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
        song_title, diff_stem = self.song_var.get(), self.diff_var.get()
        if not song_title or not diff_stem: return None
        for path_str in self.beatmaps[song_title]:
            if Path(path_str).stem == diff_stem: return path_str
        return None

    def reset_judgements(self):
        for var in self.judgement_vars.values():
            var.set(0)
