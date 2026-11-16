"""依存更新テスト用のロック情報・実行環境・ローカルwheel。"""

import base64
import hashlib
import json
from pathlib import Path
import platform
import sys
import zipfile


def lock_source(text, requirements):
    return (f"REQUIREMENTS_SHA256 = {hashlib.sha256(requirements).hexdigest()!r}\n"
            f"LOCK_TEXT = {text!r}\n").encode()


def spec(text):
    return {"text": text, "lock_hash": hashlib.sha256(text.encode()).hexdigest()}


def seed_environment(base, text, identity="a" * 32):
    directory = base.resolve() / ".updates/environments" / identity
    (directory / "bin").mkdir(parents=True)
    (directory / "bin/python").write_text("# test interpreter\n")
    (directory / "pyvenv.cfg").write_text("# test environment\n")
    (directory / "requirements.lock").write_text(text)
    state = {"id": identity, "lock_hash": spec(text)["lock_hash"]}
    (directory / "ready.json").write_text(json.dumps({"directory": str(directory), "lock_hash": state["lock_hash"],
        "python": list(sys.version_info[:2]), "platform": sys.platform, "machine": platform.machine()}))
    return state


def wheel(root, name, version, files):
    directory = f"{name}-{version}.dist-info"
    contents = {**{key: value.encode() for key, value in files.items()},
                f"{directory}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
                f"{directory}/WHEEL": b"Wheel-Version: 1.0\nGenerator: attendance-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    rows = []
    for path, data in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{path},sha256={digest},{len(data)}")
    contents[f"{directory}/RECORD"] = ("\n".join(rows) + f"\n{directory}/RECORD,,\n").encode()
    output = root / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in contents.items():
            archive.writestr(path, data)
    return output


def test_packages(root, version):
    keyring = wheel(root, "keyring", "25.0.0", {"keyring/__init__.py": "# No Keychain access in this fixture\n"})
    playwright = wheel(root, "playwright", version, {
        "playwright/__init__.py": f"__version__ = {version!r}\n",
        "playwright/__main__.py": "import os; from pathlib import Path; "
            "p = Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']); p.mkdir(parents=True, exist_ok=True); (p / 'installed').write_text('ok')\n",
        "playwright/sync_api.py": "import os\nfrom pathlib import Path\n"
            "class Browser:\n def close(self): pass\n"
            "class Chromium:\n def launch(self, **kwargs):\n  assert (Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']) / 'installed').is_file()\n  return Browser()\n"
            "class Playwright:\n chromium = Chromium()\n def start(self): return self\n def stop(self): pass\n"
            "def sync_playwright(): return Playwright()\n",
    })
    return "\n".join(f"{name}=={ver} --hash=sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
                     for name, ver, path in (("keyring", "25.0.0", keyring), ("playwright", version, playwright))) + "\n"
