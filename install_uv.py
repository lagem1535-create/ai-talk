#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""uv 설치 도우미. 목록에서 'uv' 표시가 붙은 MCP 서버(파이썬으로 만든 것)를 쓰려면 필요합니다.
'3. uv 설치.bat' 이 이 파일을 실행합니다."""
import shutil
import subprocess
import sys
import sysconfig
import os


def find_uvx():
    found = shutil.which("uvx")
    if found:
        return found
    for scheme in (None, f"{os.name}_user"):
        try:
            folder = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except KeyError:
            continue
        for name in ("uvx.exe", "uvx"):
            if os.path.isfile(os.path.join(folder, name)):
                return os.path.join(folder, name)
    return None


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    print("=" * 60)
    print("  uv 설치")
    print("=" * 60)
    if find_uvx():
        print(f"이미 설치되어 있습니다: {find_uvx()}")
        return
    print("uv 는 파이썬으로 만든 MCP 서버를 실행해 주는 프로그램입니다 (만든 곳: Astral).")
    print()
    print("주의")
    print("  - 인터넷(PyPI)에서 uv 를 내려받아 이 컴퓨터에 설치합니다.")
    print("  - 설치하면 MCP 서버를 쓸 때마다 uv 가 그 서버 프로그램을 내려받아 실행합니다.")
    print("    MCP 서버는 다른 곳에서 만든 프로그램이니, 믿을 수 있는 것만 연결하세요.")
    print("  - 지우려면:  python -m pip uninstall uv")
    print()
    try:
        answer = input("설치할까요? (y/n) [n]: ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes", "예", "ㅛ"):
        print("설치하지 않았습니다.")
        return
    code = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "uv"])
    if code != 0:
        print("\n설치하지 못했습니다. 위의 오류를 확인해 주세요.")
        sys.exit(code)
    print()
    print(f"설치했습니다: {find_uvx() or '(위치를 찾지 못했습니다)'}")
    print("이제 AI를 다시 부르면 'uv' 표시가 붙은 MCP 도 연결됩니다.")


if __name__ == "__main__":
    main()
