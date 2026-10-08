"""画面上の対象授業と出席状態を確認する。"""

import unicodedata
import re


class AttendanceError(Exception):
    pass


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def require_student_identity(page, login_id: str) -> None:
    display = page.locator(".style_text_login_user")
    if display.count() != 1:
        raise AttendanceError("ログイン中の学籍番号表示を確認できません。")
    match = re.fullmatch(r"学籍番号[:：]\s*([A-Za-z0-9]+)\s*でログイン中", normalized(display.inner_text()))
    if not match:
        raise AttendanceError("ログイン中の学籍番号を読み取れません。")
    shown_id = match.group(1)
    if shown_id != login_id and not (login_id.startswith("K") and shown_id == login_id[1:]):
        raise AttendanceError("ログイン中の学籍番号が入力したIDと一致しません。")


def require_target(page) -> dict:
    """授業情報を読む。QRのURL番号と授業の教室名は独立した情報。"""
    if page.locator(".table_list").count() != 1:
        raise AttendanceError("授業情報の表を特定できません。")
    rows = page.locator(".table_list tr")
    values = {}
    for index in range(rows.count()):
        cells = rows.nth(index).locator("td")
        if cells.count() == 2:
            values[normalized(cells.nth(0).inner_text()).rstrip("：:")] = normalized(cells.nth(1).inner_text())
    for label in ("授業名", "時限", "教室名", "開始時刻", "終了時刻"):
        if not values.get(label):
            raise AttendanceError(f"授業情報の {label} を確認できません。")
    return values


def is_attended(page) -> bool:
    buttons = page.locator("#attendForm button")
    if buttons.count() != 1:
        raise AttendanceError("出席状態のボタンを特定できません。")
    button = buttons.first
    label = normalized(button.inner_text()).replace(" ", "")
    if label == "出席済" and button.is_disabled():
        return True
    if label == "出席で登録する" and not button.is_disabled() and button.get_attribute("id") == "attend":
        return False
    raise AttendanceError("出席状態を判定できません。")


def enrollment_warning(page) -> bool:
    """登録画面本体の履修警告を確認する（確認タブの凡例は対象外）。"""
    messages = page.locator(".panel.active .main .container > p.message")
    if messages.count() == 0:
        return False
    if messages.count() != 1:
        raise AttendanceError("履修状態の表示を判定できません。")
    message = normalized(messages.first.inner_text())
    if "システム上に上記授業の履修が登録されていないため仮登録となります" in message:
        return True
    raise AttendanceError("想定外の注意表示があります。履修状態を確認してください。")


def no_active_class(page) -> bool:
    """大学側が当該時間の出席可能な授業なしと表示した場合だけ真。"""
    if page.locator(".table_list").count():
        return False
    panel = page.locator(".panel.active")
    return panel.count() == 1 and normalized(panel.inner_text()) == "現在、出席できる授業はありません。"
