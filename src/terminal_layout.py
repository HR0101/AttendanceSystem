"""日本語の幅を考慮したターミナル描画。"""

import curses
import unicodedata


def text_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def fit_text(text: str, width: int) -> str:
    result, used = [], 0
    for char in text.replace("\n", " "):
        if unicodedata.category(char).startswith("C"):
            continue
        size = text_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap_text(text, width):
    lines = []
    for paragraph in text.splitlines() or [""]:
        paragraph = fit_text(paragraph, max(1, text_width(paragraph)))
        while paragraph:
            line = fit_text(paragraph, max(2, width))
            lines.append(line)
            paragraph = paragraph[len(line):]
    return lines or [""]


def color(number):
    try:
        return curses.color_pair(number)
    except curses.error:
        return 0


class Canvas:
    def __init__(self, screen):
        self.screen = screen
        self.height, self.width = screen.getmaxyx()

    def put(self, y, x, text, attr=0, width=None):
        if not (0 <= y < self.height - 1 and 0 <= x < self.width - 1):
            return
        limit = self.width - x - 1
        if width is not None:
            limit = min(limit, width)
        try:
            self.screen.addstr(y, x, fit_text(text, limit), attr)
        except curses.error:
            pass

    def fill(self, y, x, width, height, attr=0):
        for row in range(y, y + height):
            self.put(row, x, " " * width, attr, width)

    def box(self, y, x, width, height, title="", attr=0):
        self.fill(y, x, width, height)
        # ACSは端末が持つ罫線を使うため、日本語ロケールでも幅がずれない。
        try:
            for row in (y, y + height - 1):
                self.screen.hline(row, x + 1, curses.ACS_HLINE, width - 2, attr)
            for col in (x, x + width - 1):
                self.screen.vline(y + 1, col, curses.ACS_VLINE, height - 2, attr)
            for row, col, glyph in ((y, x, curses.ACS_ULCORNER), (y, x + width - 1, curses.ACS_URCORNER), (y + height - 1, x, curses.ACS_LLCORNER), (y + height - 1, x + width - 1, curses.ACS_LRCORNER)):
                self.screen.addch(row, col, glyph, attr)
        except (curses.error, AttributeError):
            self.put(y, x, "+" + "-" * (width - 2) + "+", attr)
            self.put(y + height - 1, x, "+" + "-" * (width - 2) + "+", attr)
            for row in range(y + 1, y + height - 1):
                self.put(row, x, "|", attr)
                self.put(row, x + width - 1, "|", attr)
        if title:
            self.put(y, x + 2, f" {title} ", attr | curses.A_BOLD, width - 4)

    def button(self, y, x, label, focused=False, enabled=True):
        attr = color(1) | curses.A_BOLD if enabled else curses.A_DIM
        if focused:
            attr |= curses.A_REVERSE
        self.put(y, x, f" {label} ", attr)
