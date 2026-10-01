"""教室番号の入力と出席URL用番号への変換。"""


class RoomError(Exception):
    pass


def parse_room(value: str) -> tuple[str, str]:
    room = value.strip()
    if len(room) != 3 or not room.isascii() or not room.isdecimal():
        raise RoomError("教室番号は半角数字3桁で入力してください（例: 642、731）。")
    if room.startswith("7"):
        return room, room[:2] + "0" + room[2]
    return room, room


def ask_room() -> tuple[str, str]:
    return parse_room(input("教室番号（例: 642、731）: "))
