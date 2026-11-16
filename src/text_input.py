"""文字位置で編集し、表示幅に合わせてカーソル周辺を描く入力欄。"""

import curses

from .terminal_layout import fit_text, text_width


class TextCursor:
    def __init__(self, text=""):
        self.text = text
        self.position = len(text)

    def sync(self, text):
        if text != self.text:
            self.text, self.position = text, len(text)
        self.position = max(0, min(self.position, len(text)))
        return self.position

    def edit(self, text, key, *, limit, accepts=None):
        position = self.sync(text)
        if key == curses.KEY_LEFT:
            self.position = max(0, position - 1)
        elif key == curses.KEY_RIGHT:
            self.position = min(len(text), position + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.position = 0
        elif key in (curses.KEY_END, "\x05"):
            self.position = len(text)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if position:
                text = text[:position - 1] + text[position:]
                self.position -= 1
        elif key == curses.KEY_DC:
            text = text[:position] + text[position + 1:]
        elif key == "\x15":
            text, self.position = "", 0
        elif isinstance(key, str) and key.isprintable() and (accepts is None or accepts(key)):
            if len(text) + len(key) <= limit:
                text = text[:position] + key + text[position:]
                self.position += len(key)
        self.text = text
        return text

    def draw(self, canvas, row, column, text, width, *, active, masked=False):
        position = self.sync(text)
        shown = "*" * len(text) if masked else text
        start = 0
        caret = shown[position:position + 1] or "_"
        while start < position and (int(start > 0) + text_width(shown[start:position]) + text_width(caret) > width):
            start += 1
        prefix = "<" if start else ""
        visible = prefix + fit_text(shown[start:], max(0, width - len(prefix)))
        attr = curses.A_REVERSE if active else 0
        canvas.fill(row, column, width, 1, attr)
        canvas.put(row, column, visible, attr, width)
        if active:
            offset = len(prefix) + text_width(shown[start:position])
            canvas.put(row, column + offset, caret, attr | curses.A_UNDERLINE | curses.A_BOLD, width - offset)
