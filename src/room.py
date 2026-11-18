"""教室番号の入力と出席URL用番号への変換。"""

import unicodedata


class RoomError(Exception):
    pass


def parse_room(value: str) -> tuple[str, str]:
    """教室番号または7号館4桁番号を正規化し、(表示用教室番号, URL用ルート番号) を返す。"""
    if not isinstance(value, str):
        raise RoomError("教室番号は数字3桁、または7号館の4桁番号（例: 642、731、7301）で入力してください。")
    room = unicodedata.normalize("NFKC", value).strip()
    if room.isascii() and room.isdecimal():
        if len(room) == 3:
            if room.startswith("7"):
                return room, f"{room[:2]}0{room[2]}"
            return room, room
        if len(room) == 4 and room.startswith("7") and room[2] == "0":
            return f"{room[:2]}{room[3]}", room
    raise RoomError("教室番号は数字3桁、または7号館の4桁番号（例: 642、731、7301）で入力してください。")


def ask_room() -> tuple[str, str]:
    return parse_room(input("教室番号（例: 642、731、7301）: "))
