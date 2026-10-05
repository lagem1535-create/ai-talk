#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Talk - 내 컴퓨터 연결 프로그램
=================================
이 프로그램을 컴퓨터에 켜 두면, 휴대폰의 AI Talk 화면에서 [컴퓨터에서 바로 실행]을 눌러
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


def crypt(key, nonce, label, data):
    """컴퓨터 키로 글을 암호화·복호화한다 (같은 함수로 양쪽 다). 데이터베이스에는 암호문만 올라간다.
    HMAC-SHA256 으로 만든 열쇠 흐름과 XOR 하는 방식이며, label 로 명령('c')과 출력('o')의 열쇠 흐름을 나눈다."""
    out = bytearray()
    for i in range(0, len(data), 32):
        block = hmac.new(key.encode("utf-8"), f"{label}\n{nonce}\n{i // 32}".encode("utf-8"), hashlib.sha256).digest()
        out += bytes(a ^ b for a, b in zip(data[i:i + 32], block))
    return bytes(out)


def run_command(fb, owner, key, req, seen, allowed):
    """휴대폰에서 보낸 명령 한 줄을 이 컴퓨터에서 실행하고 출력을 돌려준다. 반환: 요청에 적을 값들."""
    nonce, t, sig, data = req.get("nonce"), req.get("t"), req.get("sig"), req.get("data")
    if not (isinstance(nonce, str) and isinstance(t, int) and isinstance(sig, str) and isinstance(data, str)):
        return {"state": "rejected"}
    want = hmac.new(key.encode("utf-8"), f"{owner}\n{nonce}\n{t}\ncmd\n{data}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or nonce in seen or abs(fb.now() - t) > MAX_AGE_MS:
        A.log("서명이 맞지 않거나 오래된 명령 요청을 받아 무시했습니다.")
        return {"state": "rejected"}
    seen.add(nonce)
    if not allowed:
        A.log("명령 실행 요청을 받았지만, --commands 없이 켜서 거절했습니다.")
        return {"state": "disabled"}
    try:
        command = crypt(key, nonce, "c", bytes.fromhex(data)).decode("utf-8")
    except ValueError:
        return {"state": "rejected"}
    A.log(f"명령 실행: {command[:120]}")
    try:
        p = subprocess.run(command, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           cwd=os.path.expanduser("~"), timeout=120)
        code, raw = p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        code, raw = -1, (e.stdout or b"") + "\n(120초가 지나 중단했습니다)".encode("utf-8")
    for enc in ("utf-8", "cp949"):   # 윈도우 명령의 출력은 cp949 인 경우가 많다
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
    text = text[-6000:]
    return {"state": "done", "code": code, "out": crypt(key, nonce, "o", text.encode("utf-8")).hex()}


SAVE_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AI Talk 저장")   # 휴대폰에서 보낸 파일을 두는 곳
RUNNERS = {".py": [sys.executable], ".js": ["node"], ".mjs": ["node"], ".bat": ["cmd", "/c"], ".cmd": ["cmd", "/c"],
           ".ps1": ["powershell", "-ExecutionPolicy", "Bypass", "-File"]}


def save_file(fb, owner, key, req, seen, allowed):
    """휴대폰에서 보낸 코드를 파일로 저장하고, 요청하면 실행한다. 반환: 요청에 적을 값들."""
    nonce, t, sig, data = req.get("nonce"), req.get("t"), req.get("sig"), req.get("data")
    if not (isinstance(nonce, str) and isinstance(t, int) and isinstance(sig, str) and isinstance(data, str)):
        return {"state": "rejected"}
    want = hmac.new(key.encode("utf-8"), f"{owner}\n{nonce}\n{t}\nfile\n{data}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or nonce in seen or abs(fb.now() - t) > MAX_AGE_MS:
        A.log("서명이 맞지 않거나 오래된 저장 요청을 받아 무시했습니다.")
        return {"state": "rejected"}
    seen.add(nonce)
    try:
        import json
        item = json.loads(crypt(key, nonce, "c", bytes.fromhex(data)).decode("utf-8"))
        name, content, run = str(item["name"]), str(item["content"]), bool(item.get("run"))
    except (ValueError, KeyError, TypeError):
        return {"state": "rejected"}
    # 파일 이름만 쓴다 (폴더 경로를 넣어 다른 곳에 쓰는 것을 막음)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(name.replace("\\", "/"))).strip(" .")[:80] or "file.txt"
    if run and not allowed:
        A.log("파일 실행 요청을 받았지만, --commands 없이 켜서 거절했습니다.")
        return {"state": "disabled"}
    os.makedirs(SAVE_DIR, exist_ok=True)
    path = os.path.join(SAVE_DIR, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    A.log(f"파일 저장: {path}" + (" (실행)" if run else ""))
    text, code = f"저장했습니다: {path}", 0
    if run:
        ext = os.path.splitext(name)[1].lower()
        if ext in (".html", ".htm", ".svg", ".txt", ".md", ".json", ".css"):
            os.startfile(path) if A.IS_WINDOWS else None   # 브라우저나 기본 프로그램으로 연다
            text += "\n컴퓨터에서 이 파일을 열었습니다."
        elif ext in RUNNERS:
            try:
                p = subprocess.run([*RUNNERS[ext], path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, cwd=SAVE_DIR, timeout=120,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                code, raw = p.returncode, p.stdout
            except subprocess.TimeoutExpired as e:
                code, raw = -1, (e.stdout or b"") + "\n(120초가 지나 중단했습니다)".encode("utf-8")
            except OSError as e:
                code, raw = -1, f"실행하지 못했습니다: {e}".encode("utf-8")
            try:
                out = raw.decode("utf-8")
            except UnicodeDecodeError:
                out = raw.decode("cp949", errors="replace")
            text += "\n\n[실행 결과]\n" + out[-5500:]
        else:
            text += f"\n{ext or '이 종류의'} 파일은 실행 방법을 몰라서 저장만 했습니다."
    return {"state": "done", "code": code, "out": crypt(key, nonce, "o", text.encode("utf-8")).hex()}


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
    ap.add_argument("--owner", help="내 AI Talk 아이디 (왼쪽 아래 @아이디). 이 사람의 요청만 받는다. 한 번 적으면 기억한다")
    ap.add_argument("--awake", action="store_true", help="켜져 있는 동안 컴퓨터가 자동으로 절전 모드에 들어가지 않게 함")
    ap.add_argument("--id", default="mypc", help="이 컴퓨터가 로그인할 계정 아이디 (기본 mypc)")
    ap.add_argument("--pw", help="그 계정의 비밀번호 (생략하면 물어봄. 한 번 로그인하면 다시 묻지 않음)")
    ap.add_argument("--site", help="배포된 AI Talk 주소. firebase-config.json 이 없을 때 여기서 설정을 받아 온다")
    ap.add_argument("--commands", action="store_true",
                    help="휴대폰에서 보낸 명령을 이 컴퓨터에서 그대로 실행하는 것을 허용 (매우 위험)")
    args = ap.parse_args()

    owner_path = os.path.join(A.BASE_DIR, "data", "agent", "pc-owner.txt")   # 한 번 적은 아이디는 기억해 둔다
    if not args.owner:
        try:
            with open(owner_path, encoding="utf-8") as f:
                args.owner = f.read().strip()
        except OSError:
            pass
    if not args.owner:
        args.owner = A.ask("내 AI Talk 아이디 (화면 왼쪽 아래 @ 뒤의 글자)").lstrip("@")
    if not args.owner:
        A.die("아이디가 필요합니다.  예: python pc_helper.py --owner 내아이디")
    os.makedirs(os.path.dirname(owner_path), exist_ok=True)
    with open(owner_path, "w", encoding="utf-8") as f:
        f.write(args.owner)
    if args.awake:
        print("절전 방지: 이 창이 켜져 있는 동안 컴퓨터가 자동으로 잠들지 않습니다." if A.keep_awake()
              else "절전 방지를 켜지 못했습니다. (윈도우에서만 됩니다)")

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
    if args.commands:   # 켤 때마다 두 번 확인받는다
        print()
        print("=" * 62)
        print("  매우 위험: 휴대폰에서 보낸 명령을 이 컴퓨터에서 그대로 실행합니다.")
        print("=" * 62)
        print("  - 파일 삭제, 프로그램 설치·실행, 컴퓨터 끄기 등 무엇이든 실행됩니다. 되돌릴 수 없습니다.")
        print("  - 컴퓨터 키를 가진 사람이면 누구나 이 컴퓨터를 마음대로 다룰 수 있습니다.")
        if not confirm("\n명령 실행을 허용할까요?"):
            A.die("취소했습니다. (--commands 를 빼고 다시 실행하면 AI 켜기만 됩니다)")
        print("\n  한 번 더 확인합니다. 이 창이 켜져 있는 동안 계속 허용됩니다. 다 쓰면 꼭 창을 닫으세요.")
        if not confirm("정말 허용할까요?"):
            A.die("취소했습니다.")
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
                    fb.db("PUT", f"{base}/status", {"name": name, "seen": A.SV, "commands": bool(args.commands)})
                for rid, req in (fb.get(f"{base}/requests") or {}).items():
                    if not isinstance(req, dict) or req.get("state"):
                        continue
                    if req.get("kind") == "cmd":
                        result = run_command(fb, owner, key, req, seen, args.commands)
                    elif req.get("kind") == "file":
                        result = save_file(fb, owner, key, req, seen, args.commands)
                    else:
                        result = {"state": handle(fb, owner, key, rid, req, seen, args.site)}
                    fb.db("PATCH", f"{base}/requests/{rid}", {**result, "done": A.SV})
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
