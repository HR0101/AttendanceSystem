"""利用者設定の読み込みと検証。"""

import json
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(Exception):
    pass


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError("config.json がありません。") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config.json の {exc.lineno} 行目を確認してください。") from exc
    if not isinstance(config, dict):
        raise ConfigurationError("config.json のルートはオブジェクトにしてください。")
    for key in ("login_url", "top_url"):
        value = config.get(key)
        try:
            parsed = urlparse(value) if isinstance(value, str) else None
            valid_host = parsed and parsed.hostname and not parsed.username and not parsed.password and not parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"{key} は有効な HTTPS のURLにしてください。") from exc
        if not parsed or parsed.scheme != "https" or not valid_host:
            raise ConfigurationError(f"{key} は HTTPS のURLにしてください。")
        if parsed.path != ("/attendance/login" if key == "login_url" else "/attendance/top"):
            raise ConfigurationError(f"{key} のパスを確認してください。")
        if parsed.query or parsed.fragment:
            raise ConfigurationError(f"{key} にクエリやフラグメントは指定できません。")
    if urlparse(config["login_url"]).hostname != urlparse(config["top_url"]).hostname:
        raise ConfigurationError("login_url と top_url のホスト名を一致させてください。")
    student_id = config.get("student_id", "")
    if not isinstance(student_id, str):
        raise ConfigurationError("student_id は文字列にしてください。")
    from .users import validate_users
    validate_users(config)
    browser = config.get("browser", {})
    if not isinstance(browser, dict) or not isinstance(browser.get("headless"), bool):
        raise ConfigurationError("browser.headless は true または false にしてください。")
    timeout = browser.get("timeout_ms")
    if type(timeout) is not int or not 1000 <= timeout <= 120000:
        raise ConfigurationError("browser.timeout_ms は 1000～120000 の整数にしてください。")
    if not isinstance(config.get("dry_run", False), bool):
        raise ConfigurationError("dry_run は true または false にしてください。")
    return config
