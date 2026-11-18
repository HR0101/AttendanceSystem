"""日本語の幅を考慮したターミナル描画。"""

import curses
import os
import unicodedata


PALETTES = {
    "dark": (
        curses.COLOR_CYAN,
        curses.COLOR_GREEN,
        curses.COLOR_RED,
        curses.COLOR_YELLOW,
    ),
    # 白・明背景で黄やシアンが沈まない高コントラスト配色。
    "light": (
        curses.COLOR_BLUE,
        curses.COLOR_GREEN,
        curses.COLOR_RED,
        curses.COLOR_MAGENTA,
    ),
}

STATUS_PREFIXES = {
    "error": "[エラー] ",
    "warning": "[注意] ",
    "update": "[更新] ",
    "success": "[完了] ",
}


def no_color_enabled(environ=None):
    """NO_COLOR 環境変数が有効（空でない値）か返す。"""
    env = os.environ if environ is None else environ
    return bool(env.get("NO_COLOR"))


def _is_light_color_index(index):
    if index < 0:
        return False
    if index < 16:
        # 8 は bright black（暗灰色）のため暗背景として扱う。
        return index == 7 or index >= 9
    if index <= 231:
        offset = index - 16
        red, green, blue = offset // 36, (offset // 6) % 6, offset % 6
        return (0.2126 * red + 0.7152 * green + 0.0722 * blue) >= 3.0
    if index <= 255:
        return index >= 244
    return False


def detect_theme(environ=None):
    """ATTENDANCE_UI_THEME と COLORFGBG からテーマ（light / dark）を決める。"""
    env = os.environ if environ is None else environ
    setting = (env.get("ATTENDANCE_UI_THEME") or "").strip().lower()
    if setting in ("light", "dark"):
        return setting
    colorfgbg = (env.get("COLORFGBG") or "").strip()
    if colorfgbg:
        last = colorfgbg.split(";")[-1].strip()
        if last.isascii() and last.isdecimal():
            return "light" if _is_light_color_index(int(last)) else "dark"
    return "dark"


def init_colors(environ=None):
    """NO_COLOR と背景色に応じてカラーペアを初期化し、色使用可否を返す。"""
    if no_color_enabled(environ):
        return False
    try:
        if not curses.has_colors():
            return False
        curses.start_color()
        theme = detect_theme(environ)
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = curses.COLOR_WHITE if theme == "light" else curses.COLOR_BLACK
        for number, foreground in enumerate(PALETTES[theme], 1):
            curses.init_pair(number, foreground, background)
        return True
    except curses.error:
        return False


def prefix_text(kind, text):
    """色だけに依存せず状態を判別できるようテキスト接頭辞を付与する。"""
    prefix = STATUS_PREFIXES.get(kind, "")
    if not prefix or not text or text.startswith(prefix):
        return text
    return f"{prefix}{text}"


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
    if no_color_enabled():
        return 0
    try:
        return curses.color_pair(number)
    except curses.error:
        return 0


class Canvas:
    def __init__(self, screen, *, use_colors=True):
        self.screen = screen
        self.height, self.width = screen.getmaxyx()
        self.use_colors = bool(use_colors) and not no_color_enabled()

    def color(self, number):
        if not self.use_colors:
            return 0
        return color(number)

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
        attr = self.color(1) | curses.A_BOLD if enabled else curses.A_DIM
        if focused:
            attr |= curses.A_REVERSE
        self.put(y, x, f" {label} ", attr)
