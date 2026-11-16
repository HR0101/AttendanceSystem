"""ハッシュ固定の依存パッケージを、移動しない専用venvへ準備する。"""

import ast
import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import uuid

from .updater import UpdateError, atomic_write, file_lock, runtime_directory, write_json


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
        environment = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(directory / "browsers"),
                       "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
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
                run([str(python), "-c", "import keyring; from playwright.sync_api import sync_playwright; "
                     "p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop()"], 60)
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
