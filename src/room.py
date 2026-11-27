"""教室番号の入力と出席URL用番号への変換。"""

import unicodedata


_ROOM_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_ROOM_ERROR = "教室番号は数字3桁、または7号館の4桁番号で入力してください（半角・全角可。例: 642、731、7301）。"


class RoomError(Exception):
    pass


def normalize_room_digits(value: str) -> str:
    return value.translate(_ROOM_DIGITS)


def parse_room(value: str) -> tuple[str, str]:
    """教室番号または7号館4桁番号を正規化し、(表示用教室番号, URL用ルート番号) を返す。"""
    if not isinstance(value, str):
        raise RoomError(_ROOM_ERROR)
    room = unicodedata.normalize("NFKC", value).strip()
    if room.isascii() and room.isdecimal():
        if len(room) == 3:
            if room.startswith("7"):
                return room, f"{room[:2]}0{room[2]}"
            return room, room
        if len(room) == 4 and room.startswith("7") and room[2] == "0":
            return f"{room[:2]}{room[3]}", room
    raise RoomError(_ROOM_ERROR)


def ask_room() -> tuple[str, str]:
    return parse_room(input("教室番号（例: 642、731、7301）: "))
