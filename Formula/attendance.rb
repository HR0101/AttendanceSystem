# typed: false
# frozen_string_literal: true

class Attendance < Formula
  desc "University attendance confirmation and registration CLI/TUI for macOS"
  homepage "https://github.com/HR0101/AttendanceSystem"
  version "0.3.4"
  license "MIT"
  head "https://github.com/HR0101/AttendanceSystem.git", branch: "main"

  depends_on :macos
  depends_on "python@3.12"

  resource "greenlet" do
    url "https://files.pythonhosted.org/packages/72/18/3fc6d951466ae9a2a688edcddde3b2e388da0a8244e0caf7117bbeb0eb95/greenlet-3.5.6-cp312-cp312-macosx_11_0_universal2.whl", using: :nounzip
    sha256 "a5876d0a60355af98d535c47f6cd6eb0f8a432396dab26845d380b92f8412422"
  end

  resource "jaraco-classes" do
    url "https://files.pythonhosted.org/packages/7f/66/b15ce62552d84bbfcec9a4873ab79d993a1dd4edb922cbfccae192bd5b5f/jaraco.classes-3.4.0-py3-none-any.whl", using: :nounzip
    sha256 "f662826b6bed8cace05e7ff873ce0f9283b5c924470fe664fff1c2f00f581790"
  end

  resource "jaraco-context" do
    url "https://files.pythonhosted.org/packages/f2/58/bc8954bda5fcda97bd7c19be11b85f91973d67a706ed4a3aec33e7de22db/jaraco_context-6.1.2-py3-none-any.whl", using: :nounzip
    sha256 "bf8150b79a2d5d91ae48629d8b427a8f7ba0e1097dd6202a9059f29a36379535"
  end

  resource "jaraco-functools" do
    url "https://files.pythonhosted.org/packages/02/36/ecc85bc96c273dc8a11273ed4782272975e6338d4a3e9228621175edf0e3/jaraco_functools-4.6.0-py3-none-any.whl", using: :nounzip
    sha256 "99e3dc0060c5cbe8fcd1cdb36258e2a65ca40f1566b2033b12abb1bb44dd3c30"
  end

  resource "keyring" do
    url "https://files.pythonhosted.org/packages/81/db/e655086b7f3a705df045bf0933bdd9c2f79bb3c97bfef1384598bb79a217/keyring-25.7.0-py3-none-any.whl", using: :nounzip
    sha256 "be4a0b195f149690c166e850609a477c532ddbfbaed96a404d4e43f8d5e2689f"
  end

  resource "more-itertools" do
    url "https://files.pythonhosted.org/packages/5d/57/3c4b1c6f2e0cc29ba023802458ea3d2a996f87085559e4a5f2192d549532/more_itertools-11.2.1-py3-none-any.whl", using: :nounzip
    sha256 "35a7377edd1dd6608dcb2cdf534ded55ea32d49448875ad042cd3f879fb1ded0"
  end

  resource "playwright" do
    on_arm do
      url "https://files.pythonhosted.org/packages/72/4f/572bbbe60a5787ca61a84bad934c3ce19c08c2bc9c0e9781bf0d99cad1a6/playwright-1.64.1-py3-none-macosx_11_0_arm64.whl", using: :nounzip
      sha256 "59f7b041284ce8d1b72098ae50ca48a2d83692eec25154ab5ba79d4b6b4fe8a9"
    end
    on_intel do
      url "https://files.pythonhosted.org/packages/8d/c8/cc677856d89a0c077673b40ab915ed5597bf1347b205cabe70121c431ca9/playwright-1.64.1-py3-none-macosx_10_13_x86_64.whl", using: :nounzip
      sha256 "29453ff76e25c17d0b45658cacd6b92e9eec46e8db21e134fb3b7143df50f013"
    end
  end

  resource "pyee" do
    url "https://files.pythonhosted.org/packages/81/12/5347938b1f9a6453f0dbdfcc3e2388a1320ef9b9ec17fbefbc4ab647ea98/pyee-14.0.0-py3-none-any.whl", using: :nounzip
    sha256 "3ac2d3229a9677f7de2c33d7f52fe25b638a46b19c413fea2edc8c6d0a644e4d"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl", using: :nounzip
    sha256 "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"
  end

  def install
    python3 = formula_opt_bin("python@3.12")/"python3.12"
    system python3, "-m", "venv", libexec

    wheelhouse = buildpath/".brew-wheels"
    wheelhouse.mkpath
    resources.each do |r|
      r.stage { wheelhouse.install Pathname.pwd.children }
    end

    system libexec/"bin/python", "-m", "pip", "install",
           "--no-input", "--disable-pip-version-check",
           "--no-index", "--find-links", wheelhouse,
           "--require-hashes", "--only-binary=:all:",
           "-r", buildpath/"requirements.lock"

    app_dir = libexec/"app"
    app_dir.install %w[
      main.py
      att
      attendance
      start.command
      install.command
      install.sh
      requirements.txt
      requirements.lock
      config.example.json
      schedule.example.json
      README.md
      COMMANDS.md
      LICENSE
      src
    ]

    (bin/"att").write <<~SH
      #!/bin/bash
      export ATTENDANCE_HOME="${ATTENDANCE_HOME:-$HOME/AttendanceSystem}"
      export ATTENDANCE_EXECUTABLE="${ATTENDANCE_EXECUTABLE:-#{opt_bin}/att}"
      export ATTENDANCE_MANAGED_BY="homebrew"
      export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-#{var}/attendance/browsers}"
      export PYTHONNOUSERSITE="1"
      unset PLAYWRIGHT_DOWNLOAD_HOST PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT
      exec "#{libexec}/bin/python" "#{app_dir}/main.py" "$@"
    SH
    chmod 0755, bin/"att"
    bin.install_symlink "att" => "attendance"
  end

  post_install_steps do
    mkdir_p "attendance/browsers", base: :var
    run "/usr/bin/env",
        args:           [
          "-u",
          "PLAYWRIGHT_DOWNLOAD_HOST",
          "-u",
          "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
          "-u",
          "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
          "-u",
          "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
          "{{libexec}}/bin/python",
          "-m",
          "playwright",
          "install",
          "chromium",
        ],
        env:            { "PLAYWRIGHT_BROWSERS_PATH" => "{{var}}/attendance/browsers" },
        writable_paths: ["attendance/browsers"],
        writable_base:  :var,
        network_access: true
  end

  def caveats
    <<~EOS
      設定ファイル・予定・ログは既定で次のフォルダに保存されます（環境変数 ATTENDANCE_HOME で変更可能）:
        ~/AttendanceSystem

      初期設定と起動:
        att user add
        att --ui

      アップデート:
        brew upgrade attendance
    EOS
  end

  test do
    ENV["ATTENDANCE_HOME"] = (testpath/"attendance-home").to_s
    ENV["ATTENDANCE_NO_UPDATE"] = "1"
    assert_match version.to_s, shell_output("#{bin}/att --version")
    assert_match "att", shell_output("#{bin}/att --help")
    assert_match "att user add", shell_output("#{bin}/att user list")
    assert_path_exists testpath/"attendance-home/config.json"
    assert_equal 0600, (testpath/"attendance-home/config.json").stat.mode & 0777
    assert_path_exists testpath/"attendance-home/start.command"
    assert_predicate testpath/"attendance-home/start.command", :executable?
    system libexec/"bin/python", "-c", "import keyring, playwright"
  end
end
