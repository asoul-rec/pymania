from tkinter import Canvas

LINE_COLOR = "#555555"
JUDGMENT_LINE_COLOR = "red"


class GameCanvas(Canvas):
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
            self.create_line(0, self.judgment_line_y, self.winfo_width(), self.judgment_line_y,
                             fill=JUDGMENT_LINE_COLOR, width=3, tags="judgment_line")

    def draw_lanes(self):
        if self.lane_count > 0 and self.winfo_width() > 1:
            for i in range(1, self.lane_count): self.create_line(i * self.lane_width, 0, i * self.lane_width,
                                                                 self.winfo_height(), fill=LINE_COLOR, width=2)

    def draw_key_hints(self, key_bindings: dict):
        if not self.winfo_height() > 1 or not self.lane_count > 0:
            return
        lane_to_key_map = {v: k for k, v in key_bindings.items()}
        y_position = (self.judgment_line_y + self.winfo_height()) / 2
        for lane_index in range(self.lane_count):
            x_position = (lane_index + 0.5) * self.lane_width
            key_text = lane_to_key_map.get(lane_index, '').capitalize()

            self.create_text(
                x_position, y_position,
                text=key_text.upper(),
                fill="white",
                font=("Wumpus Mono", int(28 * len(key_text) ** -0.3)),
                tags="key_hint"  # Tagging makes it easy to clear with canvas.clear()
            )

    def clear(self):
        self.delete("all")
