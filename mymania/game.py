from pathlib import Path
from collections import deque
from typing import Optional
import time

from .audio import AudioPlayer
from .beatmap import parse_osu_beatmap
from .gui.game_note import GameNote, HOLD_NOTE_BODY, TAP_NOTE, HOLD_NOTE_COLOR, TAP_NOTE_COLOR
from .gui.game_canvas import GameCanvas


class ManiaGame:
    # (Your ManiaGame class, but _process_press now calls app.display_judgement)
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
        self.note_speed = settings['note_speed']
        self.note_activation_lead_time = (canvas.winfo_height() / self.note_speed) + 0.5
        self.preparation_time = self.note_activation_lead_time + 1
        self.pending_notes: list[GameNote] = []
        self.active_notes: deque[GameNote] = deque()
        self._create_notes()
        self.game_start_time = None
        self.keys_currently_pressed_lanes: set[int] = set()
        self.audio_player: Optional[AudioPlayer] = None

    def prepare_canvas(self):
        self.canvas.clear()
        self.canvas.draw_lanes()
        self.canvas.draw_judgment_line(100)
        self.canvas.draw_key_hints(self.key_bindings)

    def current_game_time(self):
        if self.game_start_time is None:
            return -999
        return time.perf_counter() - self.game_start_time

    def _calculate_od_windows(self):
        od = self.overall_difficulty
        p, g, o, k, m, ms = 16.0, 64 - 3 * od, 97 - 3 * od, 127 - 3 * od, 151 - 3 * od, 188 - 3 * od
        if g <= p: raise ValueError(f"OD {od} is too high.")
        self.od_judgement_windows_s = {n: v / 1000.0 for n, v in
                                       zip(["PERFECT", "GREAT", "GOOD", "OK", "MEH", "MISS"], [p, g, o, k, m, ms])}
        self.no_effect_early_press_offset_s = -self.od_judgement_windows_s['MISS']
        self.auto_miss_if_unhit_offset_s = self.od_judgement_windows_s['OK']

    def _get_default_key_bindings(self, num_lanes):
        keys = {
            4: ['d', 'f', 'j', 'k'],
            5: ['d', 'f', 'space', 'j', 'k'],
            6: ['s', 'd', 'f', 'j', 'k', 'l'],
            7: ['s', 'd', 'f', 'space', 'j', 'k', 'l']
        }
        if num_lanes not in keys: raise NotImplementedError(f"{num_lanes}K not implemented.")
        return {key: i for i, key in enumerate(keys[num_lanes])}

    def _create_notes(self):
        ns = self.note_speed
        hit_objects = self.beatmap_data['HitObjects']
        all_notes = []
        for obj in hit_objects:
            t, x, ht, hs = int(obj[3]), int(obj[0]), int(obj[2]) / 1000, int(obj[4])
            lane = min(max(int(x * self.lane_count / 512), 0), self.lane_count - 1)
            is_hold, is_tap = t & 128, t & 1
            if is_hold:
                nt, et = HOLD_NOTE_BODY, int(obj[5].split(':')[0]) / 1000
            elif is_tap:
                nt, et = TAP_NOTE, None
            else:
                continue
            all_notes.append(GameNote(lane, nt, ns, ht, hs, et))
        all_notes.sort(key=lambda n: n.hit_time, reverse=True)
        self.pending_notes = all_notes

    def _on_key_press(self, event):
        if self.game_start_time is None: return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane not in self.keys_currently_pressed_lanes:
            self.keys_currently_pressed_lanes.add(lane)
            if self.settings['audio_offset'] <= 0.05:
                self.app.play_sfx()
            self._process_press(lane, self.current_game_time())

    def _on_key_release(self, event):
        if self.game_start_time is None: return
        lane = self.key_bindings.get(event.keysym)
        if lane is not None and lane in self.keys_currently_pressed_lanes:
            self.keys_currently_pressed_lanes.remove(lane)
            self._process_release(lane, self.current_game_time())

    def _process_press(self, lane: int, press_time: float):
        best_note = next((n for n in self.active_notes if n.lane == lane and not n.is_judged and not (
                    n.note_type == HOLD_NOTE_BODY and n.is_head_hit_successfully)), None)
        if not best_note:
            return
        time_diff = press_time - best_note.hit_time
        if not (self.no_effect_early_press_offset_s <= time_diff <= self.od_judgement_windows_s['MISS']):
            return
        abs_error = abs(time_diff)
        judgement = "Miss"
        for j_type, window in sorted(self.od_judgement_windows_s.items(), key=lambda item: item[1]):
            if j_type != "MISS" and abs_error <= window:
                judgement = j_type
                break
        self.app.update_judgement_count(judgement)
        self.app.display_judgement(judgement)
        if best_note.note_type == TAP_NOTE:
            best_note.judge_tap_hit(judgement, time_diff)
        else:
            best_note.judge_hold_head_hit(abs_error, judgement)

    def _process_release(self, lane: int, release_time: float):
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
                note.tail_release_error = self.od_judgement_windows_s['MISS'] + 0.001
                note.broken_hold = True
            self._judge_completed_hold_note(note)

    def _judge_completed_hold_note(self, note: GameNote):
        if note.is_judged or not note.is_head_hit_successfully:
            return
        if note.head_hit_error is None:
            note.judge_as_miss()
            return
        if note.tail_release_error is None:
            note.tail_release_error = self.od_judgement_windows_s['MISS']
            note.broken_hold = True

        final_judgement = "MEH"
        p, g, gd, ok = (self.od_judgement_windows_s[j] for j in ["PERFECT", "GREAT", "GOOD", "OK"])
        ce = note.head_hit_error + note.tail_release_error
        if note.head_hit_error <= p * 1.2 and ce <= p * 2.4:
            final_judgement = "PERFECT"
        elif note.head_hit_error <= g * 1.1 and ce <= g * 2.2:
            final_judgement = "GREAT"
        elif note.head_hit_error <= gd and ce <= gd * 2:
            final_judgement = "GOOD"
        elif note.head_hit_error <= ok and ce <= ok * 2:
            final_judgement = "OK"
        if note.broken_hold and final_judgement != "MEH": final_judgement = "MEH"
        self.app.update_judgement_count(final_judgement)
        self.app.display_judgement(final_judgement)
        note.judge_hold_complete(final_judgement)

    def update_notes(self, game_time: float):
        while self.pending_notes and self.pending_notes[-1].hit_time <= game_time + self.note_activation_lead_time:
            note = self.pending_notes.pop()
            note.canvas = self.canvas
            self.active_notes.append(note)
        for note in list(self.active_notes):
            if note.is_judged:
                continue
            if not note.canvas_item_id:
                note.draw_on_canvas(game_time)
            if note.canvas_item_id:
                note.update_visual_position(game_time)
            is_head_miss = (note.note_type == TAP_NOTE) or (
                note.note_type == HOLD_NOTE_BODY and not note.is_head_hit_successfully)
            if is_head_miss and game_time > note.hit_time + self.auto_miss_if_unhit_offset_s:
                note.judge_as_miss()
                self.app.update_judgement_count("Miss")
                self.app.display_judgement("Miss")
                continue
            if note.note_type == HOLD_NOTE_BODY and note.is_head_hit_successfully and not note.is_judged:
                if note.is_holding and (note.lane not in self.keys_currently_pressed_lanes):
                    if game_time < note.end_time - self.od_judgement_windows_s['MEH']: note.broken_hold = True
                    note.is_holding = False
                if game_time > note.end_time + self.auto_miss_if_unhit_offset_s: self._judge_completed_hold_note(note)
        self.active_notes = deque(n for n in self.active_notes if not n.is_judged)
