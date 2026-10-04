#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Talk - 내 컴퓨터 연결 프로그램
=================================
이 프로그램을 컴퓨터에 켜 두면, 휴대폰의 AI Talk 화면에서 「컴퓨터에서 바로 실행」을 눌러
이 컴퓨터에 AI 참가 프로그램(ai_agent.py)을 대신 켤 수 있습니다. (터미널에 명령을 직접 붙여 넣지 않아도 됨)

    python pc_helper.py --owner 내아이디

- 이 컴퓨터가 켜져 있고 이 프로그램이 실행 중일 때만 동작합니다.
- '컴퓨터 키'로 서명된 요청만 받습니다. 키는 처음 실행할 때 만들어지며, 앱의 설정에 한 번 입력합니다.
- 실행하는 것은 ai_agent.py 뿐이고, 아무 명령이나 실행하지는 않습니다.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import subprocess
import sys
import time

import ai_agent as A

KEY_PATH = os.path.join(A.BASE_DIR, "data", "agent", "pc-key.txt")
SESSIONS = os.path.join(A.BASE_DIR, "data", "agent", "sessions.json")
MAX_AGE_MS = 5 * 60000   # 이보다 오래된 요청은 받지 않는다
# ai_agent.py 에 넘겨도 되는 옵션 (값이 있는 것 / 없는 것)
WITH_VALUE = {"--cli", "--code", "--count", "--id", "--name", "--alias", "--persona", "--model", "--mcp", "--mcp-add",
              "--project", "--github", "--key", "--site", "--history", "--timeout"}
NO_VALUE = {"--yes", "--shell", "--full", "--mcp-own"}


def confirm(question):
    try:
        return input(f"{question} (y/n) [n]: ").strip().lower() in ("y", "yes", "예", "ㅛ")
    except EOFError:
        return False


def load_key():
    """컴퓨터 키를 읽는다. 없으면 경고를 두 번 보여 주고 새로 만든다."""
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) >= 20:
            return key
    except OSError:
        pass
    print("=" * 62)
    print("  위험: 이 프로그램은 휴대폰에서 이 컴퓨터에 AI를 켤 수 있게 합니다.")
    print("=" * 62)
    print("  - 컴퓨터 키를 가진 사람은 이 컴퓨터에서 AI 참가 프로그램을 켤 수 있습니다.")
    print("    코딩방 옵션(파일 수정, 명령 실행, 모든 명령 허용)과 직접 추가한 MCP 명령도 포함됩니다.")
    print("  - 즉, 키가 새면 남이 이 컴퓨터의 파일을 고치거나 프로그램을 실행할 수 있습니다.")
    print("  - 키는 이 컴퓨터와, 키를 입력한 내 휴대폰·브라우저에만 저장됩니다.")
    if not confirm("\n계속할까요?"):
        A.die("취소했습니다.")
    print("\n  한 번 더 확인합니다.")
    print("  - 이 창을 켜 둔 동안에는 내가 자리에 없어도 요청이 오면 바로 실행됩니다.")
    print("  - 쓰지 않을 때는 이 창을 닫아 두세요. 키가 새었다면 data/agent/pc-key.txt 를 지우고 다시 실행하세요.")
    if not confirm("정말 켤까요?"):
        A.die("취소했습니다.")
    key = secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    with open(KEY_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def clean_args(args):
    """요청으로 온 옵션 중 ai_agent.py 에 넘겨도 되는 것만 남긴다. 이상하면 None."""
    out, i = [], 0
    while i < len(args):
        flag = args[i]
        if flag in NO_VALUE:
            out.append(flag)
            i += 1
        elif flag in WITH_VALUE and i + 1 < len(args):
            out += [flag, args[i + 1]]
            i += 2
        else:
            return None
    return out if "--cli" in out else None


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="AI Talk - 휴대폰에서 이 컴퓨터에 AI를 켤 수 있게 해 주는 프로그램")
    ap.add_argument("--owner", required=True, help="내 AI Talk 아이디 (왼쪽 아래 @아이디). 이 사람의 요청만 받는다")
    ap.add_argument("--id", default="mypc", help="이 컴퓨터가 로그인할 계정 아이디 (기본 mypc)")
    ap.add_argument("--pw", help="그 계정의 비밀번호 (생략하면 물어봄. 한 번 로그인하면 다시 묻지 않음)")
    ap.add_argument("--site", help="배포된 AI Talk 주소. firebase-config.json 이 없을 때 여기서 설정을 받아 온다")
    args = ap.parse_args()

    if args.site:
        site = args.site.rstrip("/")
        site = site if re.match(r"^https?://", site) else "https://" + site
        try:
            remote = A.Firebase().http("GET", site + "/firebase-config.json") or {}
        except (A.ApiError, A.NetError) as e:
            A.die(f"{site} 에서 Firebase 설정을 받아 오지 못했습니다: {e}")
        A.API_KEY, A.DB_URL = remote.get("apiKey", ""), remote.get("databaseURL", "").rstrip("/")
    if not A.API_KEY or not A.DB_URL:
        A.die("Firebase 설정이 없습니다. firebase-config.json 을 두거나 --site 주소 를 붙여 주세요.")

    key = load_key()
    fb = A.Firebase()
    interactive = bool(sys.stdin and sys.stdin.isatty())

    def ask_password():
        if not interactive:
            A.die("비밀번호가 필요합니다. --pw 로 지정해 주세요.")
        import getpass
        return getpass.getpass(f"컴퓨터 계정 '{args.id}' 의 비밀번호 (처음이면 6자 이상으로 새로 정하세요): ")

    try:
        A.login(fb, args.id, args.pw, ask_password, "내 컴퓨터", SESSIONS)
        fb.sync_clock()
        owner = fb.get("usernames/" + A.handle_key(args.owner.lstrip("@")))
    except A.NetError as e:
        A.die(f"인터넷에 연결하지 못했습니다: {e}")
    except A.ApiError as e:
        A.die(f"Firebase 오류: {e.message}")
    if not owner:
        A.die(f"'{args.owner}' 아이디를 찾을 수 없습니다. AI Talk 화면 왼쪽 아래의 @아이디 를 확인해 주세요.")

    print()
    print("=" * 62)
    print("  컴퓨터 키 (앱의 설정 → 내 컴퓨터 연결에 한 번 입력하세요)")
    print(f"      {key}")
    print("  이 키를 남에게 보여 주지 마세요. 이 창을 닫으면 연결이 끊깁니다.")
    print("=" * 62, flush=True)
    A.log(f"'{args.owner}' 님의 요청을 기다리는 중… (끄기: Ctrl+C)")

    base = f"pc/{owner}"
    seen, beat, delay = set(), 0.0, 1
    name = os.environ.get("COMPUTERNAME") or "내 컴퓨터"
    try:
        while True:
            try:
                if time.time() - beat > 15:   # 앱에 '컴퓨터 켜짐'으로 보이게 한다
                    beat = time.time()
                    fb.db("PUT", f"{base}/status", {"name": name, "seen": A.SV})
                for rid, req in (fb.get(f"{base}/requests") or {}).items():
                    if not isinstance(req, dict) or req.get("state"):
                        continue
                    state = handle(fb, owner, key, rid, req, seen, args.site)
                    fb.db("PATCH", f"{base}/requests/{rid}", {"state": state, "done": A.SV})
                delay = 1
                time.sleep(2)
            except (A.ApiError, A.NetError) as e:
                A.log(f"연결 문제: {e} ({delay}초 뒤 다시 시도)")
                time.sleep(delay)
                delay = min(delay * 2, 30)
    except KeyboardInterrupt:
        try:
            fb.db("DELETE", f"{base}/status")
        except Exception:
            pass
        print("\n컴퓨터 연결을 끝냅니다.")


def handle(fb, owner, key, rid, req, seen, site):
    """요청 하나를 확인하고 ai_agent.py 를 새 창에서 켠다. 반환: 처리 결과 글자."""
    args, nonce, t, sig = req.get("args"), req.get("nonce"), req.get("t"), req.get("sig")
    if not (isinstance(args, list) and all(isinstance(a, str) for a in args)
            and isinstance(nonce, str) and isinstance(t, int) and isinstance(sig, str)):
        return "rejected"
    want = hmac.new(key.encode("utf-8"), f"{owner}\n{nonce}\n{t}\n".encode("utf-8") + "\x1f".join(args).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or nonce in seen or abs(fb.now() - t) > MAX_AGE_MS:
        A.log("서명이 맞지 않거나 오래된 요청을 받아 무시했습니다.")
        return "rejected"
    seen.add(nonce)
    safe = clean_args(args)
    if safe is None:
        A.log("받을 수 없는 옵션이 들어 있어 무시했습니다.")
        return "rejected"
    if site and "--site" not in safe:
        safe += ["--site", site]
    shown = " ".join(a if a.startswith("--") else "…" if len(a) > 24 else a for a in safe)
    A.log(f"요청을 받아 AI를 켭니다: {shown}")
    flags = subprocess.CREATE_NEW_CONSOLE if A.IS_WINDOWS else 0   # 새 창에서 켜서, 닫으면 그 AI만 꺼지게 한다
    try:
        subprocess.Popen([sys.executable, os.path.join(A.BASE_DIR, "ai_agent.py"), *safe], cwd=A.BASE_DIR, creationflags=flags)
    except OSError as e:
        A.log(f"켜지 못했습니다: {e}")
        return "failed"
    return "started"


if __name__ == "__main__":
    main()
