"""提示画面に対応するログインと出席登録。"""

from urllib.parse import urlparse, urlunparse

from .verification import AttendanceError, enrollment_warning, is_attended, no_active_class, normalized, require_student_identity, require_target


def _require_known_page(page, config: dict, route: str, require_room: bool = False) -> None:
    current = urlparse(page.url)
    allowed = {urlparse(config[key]).hostname for key in ("login_url", "top_url")}
    if current.scheme != "https" or current.hostname not in allowed:
        raise AttendanceError("想定外のサイトへ移動しました。手動認証が必要な場合は画面を確認してください。")
    if current.path not in ("/attendance/login", "/attendance/top", f"/attendance/class_room/{route}"):
        raise AttendanceError("想定外の画面です。画面の変更または追加認証を確認してください。")
    if require_room and current.path != f"/attendance/class_room/{route}":
        raise AttendanceError("指定したQR番号の出席画面を確認できません。")


def run_attendance(page, config: dict, student_id: str, password: str, room: str, route: str, logger, expected_course: str | None = None, expected_start: str | None = None, on_snapshot=None, expected_values: dict | None = None) -> str:
    login = urlparse(config["login_url"])
    room_url = urlunparse(("https", login.netloc, f"/attendance/class_room/{route}", "", "", ""))
    logger.info("段階=教室URLへアクセス")
    page.goto(room_url, wait_until="domcontentloaded")
    _require_known_page(page, config, route)
    if urlparse(page.url).path == "/attendance/login":
        logger.info("段階=ログインフォーム入力")
        if page.locator("#userid").count() != 1 or page.locator("#password").count() != 1:
            raise AttendanceError("ログイン画面が変更されています。")
        page.locator("#userid").fill(student_id)
        page.locator("#password").fill(password)
        # このサイトは keyup でログインボタンを有効化する。fill だけでは発火しない。
        page.locator("#password").press("End")
        login_button = page.locator("button[name=login]")
        if login_button.count() != 1 or login_button.is_disabled():
            raise AttendanceError("ログインボタンが有効になりません。画面を確認してください。")
        logger.info("段階=ログイン送信")
        with page.expect_navigation(wait_until="domcontentloaded"):
            login_button.click()
        _require_known_page(page, config, route)
        if urlparse(page.url).path == "/attendance/login":
            raise AttendanceError("ログインを確認できません。IDまたはパスワードを確認してください。")
    logger.info("ログイン画面を通過")
    # ログイン後に元のURLへ戻るかどうかはサーバー設定に依存するため、明示的に開く。
    logger.info("段階=教室画面を再表示")
    page.goto(room_url, wait_until="domcontentloaded")
    _require_known_page(page, config, route)
    if urlparse(page.url).path == "/attendance/login":
        raise AttendanceError("教室URLへのアクセス後にログイン画面へ戻りました。")
    _require_known_page(page, config, route, require_room=True)
    require_student_identity(page, student_id)
    if page.locator("#errorModal").count():
        raise AttendanceError("出席画面にエラーが表示されています。")
    logger.info("段階=授業と履修状態を確認")
    if no_active_class(page):
        if on_snapshot:
            on_snapshot({}, False, False)
        return "出席受付対象なし（受付時間外の可能性・出席操作なし）"
    values = require_target(page)
    if expected_values is not None and values != expected_values:
        raise AttendanceError("確認した授業情報が変更されています。画面を更新して再確認してください。")
    if expected_course and values["授業名"] != normalized(expected_course):
        raise AttendanceError("予定した授業名と画面の授業名が一致しません。")
    if expected_start and values["開始時刻"] != expected_start:
        raise AttendanceError("予定した開始時刻と画面の開始時刻が一致しません。")
    logger.info("対象授業を確認")
    unenrolled = enrollment_warning(page)
    attended = is_attended(page)
    if on_snapshot:
        on_snapshot(values, attended, unenrolled)
    if unenrolled:
        if attended:
            return "履修登録なし・出席済み表示（仮登録の可能性、要確認）"
        return "履修登録なし（出席操作なし）"
    if attended:
        return "出席済み"
    if config.get("dry_run", False):
        return "未出席（確認のみ）"
    logger.info("段階=出席確認ダイアログ")
    page.locator("#attend").click()
    dialog = page.locator("#confirmModal")
    if dialog.count() != 1 or not dialog.is_visible():
        raise AttendanceError("登録確認ダイアログを確認できません。")
    body = normalized(dialog.locator(".dialog_body").inner_text())
    for label in ("授業名", "時限", "教室名", "開始時刻", "終了時刻"):
        if values[label] not in body:
            raise AttendanceError("確認ダイアログの授業情報が一致しません。")
    logger.info("段階=出席登録送信")
    with page.expect_navigation(wait_until="domcontentloaded"):
        dialog.locator("#ok_confirmModal").click()
    logger.info("段階=登録結果を確認")
    _require_known_page(page, config, route)
    if urlparse(page.url).path == "/attendance/login" or page.locator("#errorModal").count():
        raise AttendanceError("登録結果を確認できません。")
    if require_target(page) != values:
        raise AttendanceError("登録前後の授業情報が一致しません。")
    if enrollment_warning(page):
        return "履修登録なし（登録後に警告を検出・要確認）"
    if not is_attended(page):
        raise AttendanceError("出席済み表示を確認できません。")
    return "出席登録成功"
