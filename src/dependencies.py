"""ハッシュ固定の依存パッケージを、移動しない専用venvへ準備する。"""

import ast
import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import textwrap
import uuid

from .updater import UpdateError, atomic_write, file_lock, runtime_directory, write_json

UNTRUSTED_PLAYWRIGHT_ENV_VARS = (
    "PLAYWRIGHT_DOWNLOAD_HOST",
    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
    "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
)

_BROWSER_VERIFY_SCRIPT = textwrap.dedent("""
    import json, os, pathlib, keyring, playwright
    from playwright.sync_api import sync_playwright

    browsers_path = pathlib.Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if browsers_path.is_symlink():
        raise RuntimeError("PLAYWRIGHT_BROWSERS_PATH must not be a symlink")
    browsers_root = browsers_path.resolve()
    browsers_meta = pathlib.Path(playwright.__file__).resolve().parent / "driver/package/browsers.json"
    browsers_data = json.loads(browsers_meta.read_text(encoding="utf-8"))
    chromium = next(item for item in browsers_data["browsers"] if item.get("name") == "chromium")
    revisions = {str(chromium["revision"])}
    revisions.update(str(v) for v in (chromium.get("revisionOverrides") or {}).values())
    if not revisions or any(not rev or not rev.isascii() or not rev.isalnum() for rev in revisions):
        raise RuntimeError("Invalid Chromium revision in playwright browsers.json")
    expected_dirs = {f"chromium-{rev}" for rev in revisions}

    p = sync_playwright().start()
    try:
        executable = p.chromium.executable_path
        if not executable:
            raise RuntimeError("Chromium executable_path is empty")
        exe = pathlib.Path(executable)
        if (
            not exe.is_absolute()
            or ".." in exe.parts
            or not exe.exists()
            or exe.is_symlink()
            or not exe.is_file()
            or not os.access(exe, os.X_OK)
        ):
            raise RuntimeError("Chromium executable is missing, not executable, or a symlink")
        resolved = exe.resolve()
        rel = resolved.relative_to(browsers_root)
        if not rel.parts or rel.parts[0] not in expected_dirs:
            raise RuntimeError("Chromium revision does not match playwright browsers.json")
        cur = exe
        while cur.resolve() != browsers_root:
            if cur.is_symlink() or cur == cur.parent:
                raise RuntimeError("Chromium executable path must not traverse symlinks")
            cur = cur.parent
        browser = p.chromium.launch(headless=True)
        browser.close()
    finally:
        p.stop()
""").strip()


def sanitized_environment(directory):
    """ブラウザ取得元の上書き環境変数を除外し、専用venv用の環境変数を組み立てる。"""
    environment = {key: value for key, value in os.environ.items() if key not in UNTRUSTED_PLAYWRIGHT_ENV_VARS}
    environment.update({
        "PLAYWRIGHT_BROWSERS_PATH": str(directory / "browsers"),
        "PYTHONNOUSERSITE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return environment


def run_command(command, *, env, stdout, timeout):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        # PlaywrightのNode子プロセスも止めてから、未完成の環境を削除する。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def read_spec(root):
    try:
        tree = ast.parse((root / "src/dependency_lock.py").read_bytes())
        values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
        text, input_hash = values["LOCK_TEXT"], values["REQUIREMENTS_SHA256"]
        if not isinstance(text, str) or not text.strip() or len(text) > 1024 * 1024:
            raise ValueError
        if input_hash != hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest():
            raise ValueError
        data = text.encode("utf-8")
        return {"text": text, "lock_hash": hashlib.sha256(data).hexdigest()}
    except (OSError, SyntaxError, ValueError, TypeError, KeyError) as exc:
        raise UpdateError("依存パッケージのロック情報が不正、または requirements.txt と一致しません。配布元で再生成してください。") from exc


def prepare_environment(base, spec):
    base = base.resolve()
    updates = base / ".updates"
    if updates.is_symlink():
        raise UpdateError("依存パッケージの保存先がシンボリックリンクです。")
    with file_lock(updates / "dependency.lock", exclusive=True):
        environments = updates / "environments"
        if environments.is_symlink() or updates.is_symlink():
            raise UpdateError("依存パッケージの保存先がシンボリックリンクです。")
        environments.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in sorted(environments.iterdir()):
            state = {"id": path.name, "lock_hash": spec["lock_hash"]}
            try:
                runtime_directory(base, state)
            except (UpdateError, OSError):
                continue
            return state
        state = {"id": uuid.uuid4().hex, "lock_hash": spec["lock_hash"]}
        directory = environments / state["id"]
        python = directory / "bin/python"
        environment = sanitized_environment(directory)
        log_path = updates / "dependencies.log"
        stage = "仮想環境の作成"
        try:
            with os.fdopen(os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600), "a") as log:
                def run(command, timeout=300):
                    log.write(f"\n段階={stage}\n")
                    log.flush()
                    if run_command(command, stdout=log, timeout=timeout, env=environment):
                        raise UpdateError(f"依存パッケージの{stage}に失敗しました。.updates/dependencies.log を確認してください。")

                # venvは絶対パスを含むため、このフォルダを後から移動しない。
                run([getattr(sys, "_base_executable", sys.executable), "-m", "venv", str(directory)], 120)
                atomic_write(directory / "requirements.lock", spec["text"].encode(), 0o600)
                stage = "ダウンロード・ハッシュ検証付き導入"
                run([str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                     "--require-hashes", "--only-binary=:all:", "-r", str(directory / "requirements.lock")])
                stage = "依存関係の整合性確認"
                run([str(python), "-m", "pip", "check"], 60)
                stage = "Chromiumの導入"
                run([str(python), "-m", "playwright", "install", "chromium"])
                stage = "起動確認"
                run([str(python), "-c", _BROWSER_VERIFY_SCRIPT], 60)
            write_json(directory / "ready.json", {"directory": str(directory), "lock_hash": spec["lock_hash"],
                                                  "python": list(sys.version_info[:2]), "platform": sys.platform,
                                                  "machine": platform.machine()})
            runtime_directory(base, state)
            return state
        except BaseException as exc:
            shutil.rmtree(directory, ignore_errors=True)
            if isinstance(exc, (UpdateError, KeyboardInterrupt, SystemExit)):
                raise
            raise UpdateError(f"依存パッケージの{stage}でエラーが発生しました。.updates/dependencies.log を確認してください。") from exc
