"""未保存の変更を残したまま、保存・破棄・編集継続を選ぶ確認。"""

import curses

from .terminal_layout import color


class SavePrompt:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.selected = 2
        self.title = ""
        self.save = self.discard = None

    def open(self, title, *, save, discard):
        self.active = True
        self.selected = 2
        self.title, self.save, self.discard = title, save, discard

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
        width, height = min(canvas.width - 4, 64), min(canvas.height - 2, 15)
        left, top = (canvas.width - width) // 2, (canvas.height - height) // 2
        canvas.box(top, left, width, height, "未保存の変更", color(4))
        canvas.put(top + 2, left + 3, self.title, curses.A_BOLD, width - 6)
        canvas.put(top + 3, left + 3, "変更を保存してから戻りますか？", width=width - 6)
        for index, label in enumerate(("[ 保存して戻る ]", "[ 保存せず戻る ]", "[ 編集を続ける ]")):
            canvas.button(top + 5 + index * 2, left + 3, label, self.selected == index)
        canvas.put(top + height - 3, left + 3, "↑↓/Tab: 選択  Enter: 決定", curses.A_DIM, width - 6)
        canvas.put(top + height - 2, left + 3, "Esc/Q: 編集に戻る", curses.A_DIM, width - 6)
