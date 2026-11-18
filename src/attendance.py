"""提示画面に対応するログインと出席登録。"""

import re
from urllib.parse import urlparse, urlunparse

from .verification import AttendanceError, enrollment_warning, is_attended, no_active_class, normalized, require_student_identity, require_target

# ページ本文は個人情報やURLを含み得るため、分類後の固定文言・固定種別名だけを扱う。
_ERROR_CATEGORIES: tuple[tuple[str, tuple[re.Pattern[str], ...], str], ...] = (
    (
        "lock",
        (
            re.compile(r"アカウント.{0,12}(?:ロック|凍結|停止|制限)"),
            re.compile(r"(?:ロック|凍結)され"),
            re.compile(r"ロック中"),
            re.compile(r"(?:試行|失敗).{0,6}回数|回数制限|上限に達し|連続.{0,8}失敗"),
            re.compile(r"認証制限|利用停止|利用制限"),
            re.compile(r"account.{0,12}lock|\blocked\b|too many (?:failed|attempts)|suspended"),
        ),
        "アカウントがロックまたは認証制限されている可能性があります。大学側の案内を確認してください。",
    ),
    (
        "maintenance",
        (
            re.compile(r"メンテナンス|保守(?:作業|中|点検)?"),
            re.compile(r"(?:システム|サービス).{0,6}(?:停止|休止)|停止中|休止中"),
            re.compile(r"障害|混み合って|混雑|アクセス集中|過負荷"),
            re.compile(r"(?:ただいま|現在).{0,6}(?:利用|ご利用)(?:できません|いただけません)"),
            re.compile(r"maintenance|service unavailable|temporarily unavailable|\b50[0234]\b"),
        ),
        "大学側がメンテナンス中または一時的に利用できない可能性があります。時間をおいて確認してください。",
    ),
    (
        "session",
        (
            re.compile(r"セッション|タイムアウト|有効期限|期限切れ|再(?:度)?ログイン|ログインし直"),
            re.compile(r"トークン|csrf|session|timed?\s*out|expired"),
        ),
        "セッションが切れたかタイムアウトしました。再度実行してください。",
    ),
    (
        "invalid_request",
        (
            re.compile(r"受付時間外|時間外|受付期間外|受付.{0,4}終了|締(?:め)?切"),
            re.compile(r"不正な(?:リクエスト|操作|遷移|画面|アクセス)|二重送信|重複登録"),
            re.compile(r"(?:登録|出席)できません|対象外"),
            re.compile(r"invalid request|bad request|out of.{0,8}window|already (?:registered|attended)"),
        ),
        "受付時間外または不正な画面状態として拒否された可能性があります。受付時間と画面を確認してください。",
    ),
    (
        "forbidden",
        (
            re.compile(r"アクセス権|権限がありません|許可されていません"),
            re.compile(r"access denied|forbidden|\b403\b"),
        ),
        "アクセス権限または利用条件により拒否されました。大学の画面を確認してください。",
    ),
    (
        "auth",
        (
            re.compile(r"(?:ログイン|認証).{0,8}(?:失敗|できません|エラー)|一致しません|誤り|正しくありません"),
            re.compile(r"ユーザー\s*id|ユーザ\s*id|学籍番号|パスワード"),
            re.compile(r"invalid credentials|authentication failed|login failed|incorrect"),
        ),
        "ログインを確認できません。IDまたはパスワードを確認してください。",
    ),
)

_MODAL_HEADING_SELECTORS = (
    "#errorModal .dialog_title",
    "#errorModal .modal-title",
    "#errorModal h1",
    "#errorModal h2",
    "#errorModal h3",
    "#errorModal h4",
)
_MODAL_BODY_SELECTORS = (
    "#errorModal .dialog_body",
    "#errorModal .modal-body",
    "#errorModal .message",
    "#errorModal .error",
    "#errorModal p",
)
_LOGIN_ERROR_SELECTORS = (
    ".error",
    ".errors",
    ".error_message",
    ".error-message",
    ".login_error",
    ".login-error",
    ".alert",
    ".alert-danger",
    ".alert-error",
    '[role="alert"]',
    ".message",
    "#error",
)
_LOGIN_GENERIC_SELECTORS = ("h1", "h2", "h3", "h4", "p")


def _read_locator_texts(locator, *, visible_only: bool = False, limit: int = 8) -> list[str]:
    texts = []
    try:
        count = locator.count()
    except Exception:
        return texts
    if not isinstance(count, int) or count <= 0:
        return texts
    for index in range(min(count, limit)):
        item = locator.nth(index)
        try:
            if visible_only and not item.is_visible():
                continue
            text = item.inner_text(timeout=500)
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def _collect_texts(page, selectors: tuple[str, ...], *, visible_only: bool = False) -> list[str]:
    texts = []
    for selector in selectors:
        texts.extend(_read_locator_texts(page.locator(selector), visible_only=visible_only))
    return texts


def _classify_texts(texts: list[str], allowed_categories: tuple[str, ...] | None = None) -> tuple[str, str | None]:
    rules = [entry for entry in _ERROR_CATEGORIES if allowed_categories is None or entry[0] in allowed_categories]
    candidates = list(texts)
    if len(texts) > 1:
        candidates.append(" ".join(texts))
    for text in candidates:
        cleaned = normalized(text).lower()
        if not cleaned:
            continue
        for category, patterns, message in rules:
            if any(pattern.search(cleaned) for pattern in patterns):
                return category, message
    return "unknown", None


def _classify_error_modal(page, *, visible_only: bool = False) -> tuple[str, str | None]:
    texts = _collect_texts(page, _MODAL_HEADING_SELECTORS, visible_only=visible_only)
    texts.extend(_collect_texts(page, _MODAL_BODY_SELECTORS, visible_only=visible_only))
    category, message = _classify_texts(texts)
    if category != "unknown":
        return category, message
    container_texts = _read_locator_texts(page.locator("#errorModal"), visible_only=visible_only, limit=2)
    return _classify_texts(container_texts)


def _raise_error_modal(page, logger=None, default_message: str = "出席画面にエラーが表示されています。") -> None:
    """#errorModal の定型見出し・本文を安全な固定メッセージへ分類して停止する。"""
    category, message = _classify_error_modal(page)
    if logger is not None:
        logger.info("エラー分類=%s", category)
    raise AttendanceError(message or default_message)


def _raise_login_error(page, logger=None, default_message: str = "ログインを確認できません。IDまたはパスワードを確認してください。") -> None:
    """ログイン失敗画面のエラー表示・ロック・メンテナンスを安全に分類して停止する。"""
    if page.locator("#errorModal").count():
        category, message = _classify_error_modal(page, visible_only=True)
        if category != "unknown":
            if logger is not None:
                logger.info("エラー分類=%s", category)
            raise AttendanceError(message)
    banner_texts = _collect_texts(page, _LOGIN_ERROR_SELECTORS, visible_only=True)
    category, message = _classify_texts(banner_texts)
    if category != "unknown":
        if logger is not None:
            logger.info("エラー分類=%s", category)
        raise AttendanceError(message)
    # 通常フォームの説明文（ID/パスワード案内）を誤判定しないよう、見出し・段落は停止要因だけ見る。
    generic_texts = _collect_texts(page, _LOGIN_GENERIC_SELECTORS, visible_only=True)
    category, message = _classify_texts(generic_texts, allowed_categories=("lock", "maintenance", "session"))
    if category != "unknown":
        if logger is not None:
            logger.info("エラー分類=%s", category)
        raise AttendanceError(message)
    if logger is not None:
        logger.info("エラー分類=unknown")
    raise AttendanceError(default_message)


def _require_known_page(page, config: dict, route: str) -> None:
    current = urlparse(page.url)
    allowed = {urlparse(config[key]).hostname for key in ("login_url", "top_url")}
    if current.scheme != "https" or current.hostname not in allowed:
        raise AttendanceError("想定外のサイトへ移動しました。手動認証が必要な場合は画面を確認してください。")
    if current.path not in ("/attendance/login", "/attendance/top", f"/attendance/class_room/{route}"):
        raise AttendanceError("想定外の画面です。画面の変更または追加認証を確認してください。")


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
            _raise_login_error(page, logger)
    logger.info("ログイン画面を通過")
    # ログイン後に元のURLへ戻るかどうかはサーバー設定に依存するため、明示的に開く。
    logger.info("段階=教室画面を再表示")
    page.goto(room_url, wait_until="domcontentloaded")
    _require_known_page(page, config, route)
    if urlparse(page.url).path == "/attendance/login":
        _raise_login_error(page, logger, "教室URLへのアクセス後にログイン画面へ戻りました。")
    if page.locator("#errorModal").count():
        _raise_error_modal(page, logger, "出席画面にエラーが表示されています。")
    require_student_identity(page, student_id)
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
    if page.locator("#errorModal").count():
        _raise_error_modal(page, logger, "登録結果を確認できません。")
    if urlparse(page.url).path == "/attendance/login":
        _raise_login_error(page, logger, "登録結果を確認できません。")
    if require_target(page) != values:
        raise AttendanceError("登録前後の授業情報が一致しません。")
    if enrollment_warning(page):
        return "履修登録なし（登録後に警告を検出・要確認）"
    if not is_attended(page):
        raise AttendanceError("出席済み表示を確認できません。")
    return "出席登録成功"
