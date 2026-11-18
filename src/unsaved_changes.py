"""未保存の変更を残したまま、保存・破棄・編集継続を選ぶ確認。"""

import curses

from .terminal_layout import color, fit_text, wrap_text


class SavePrompt:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.selected = 2
        self.title = ""
        self.details = ()
        self.save = self.discard = None

    def open(self, title, *, save, discard, details=()):
        """未保存確認を開く。detailsが渡された場合は保存影響の補足行を表示する。"""
        self.active = True
        self.selected = 2
        self.title = title
        self.details = tuple(details)
        self.save, self.discard = save, discard

    def handle(self, key):
        if key in ("\x1b", "q", "Q"):
            self.reset()
        elif key in ("\t", curses.KEY_DOWN, curses.KEY_RIGHT):
            self.selected = (self.selected + 1) % 3
        elif key in (curses.KEY_UP, curses.KEY_LEFT, curses.KEY_BTAB):
            self.selected = (self.selected - 1) % 3
        elif key in ("\n", "\r", curses.KEY_ENTER):
            action = self.save if self.selected == 0 else self.discard if self.selected == 1 else None
            self.reset()
            if action:
                action()

    def draw(self, canvas):
        width = min(canvas.width - 4, 64)
        inner = max(2, width - 6)
        detail_lines = [line for entry in self.details for line in wrap_text(entry, inner)]
        base_height = 14 + len(detail_lines) if detail_lines else 15
        height = min(canvas.height - 2, base_height)
        left, top = (canvas.width - width) // 2, (canvas.height - height) // 2
        canvas.box(top, left, width, height, "未保存の変更", color(4))
        canvas.put(top + 2, left + 3, self.title, curses.A_BOLD, inner)
        canvas.put(top + 3, left + 3, "変更を保存してから戻りますか？", width=inner)
        max_shown = max(0, height - 14)
        shown = detail_lines[:max_shown]
        if len(detail_lines) > max_shown and shown:
            shown[-1] = fit_text(shown[-1], max(2, inner - 2)) + "…"
        for offset, line in enumerate(shown):
            canvas.put(top + 4 + offset, left + 3, line, width=inner)
        buttons_top = top + 5 + len(shown) if shown else top + 5
        for index, label in enumerate(("[ 保存して戻る ]", "[ 保存せず戻る ]", "[ 編集を続ける ]")):
            canvas.button(buttons_top + index * 2, left + 3, label, self.selected == index)
        canvas.put(top + height - 3, left + 3, "↑↓/Tab: 選択  Enter: 決定", curses.A_DIM, inner)
        canvas.put(top + height - 2, left + 3, "Esc/Q: 編集に戻る", curses.A_DIM, inner)
