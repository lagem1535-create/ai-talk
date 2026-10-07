#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Talk 화면 열기
=================
index.html 을 http://localhost:8765 로 보여 주는 아주 작은 서버입니다.
(대화 내용과 로그인은 Firebase 가 맡습니다. 이 서버는 화면 파일만 전달합니다.)

    python server.py                ->  http://localhost:8765
    python server.py --port 9000    ->  포트 바꾸기

Google 로그인은 localhost 주소에서만 되므로, index.html 을 직접 열지 말고 이 서버로 여세요.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
CONFIG_PATH = os.path.join(BASE_DIR, "firebase-config.json")
DEFAULT_PORT = 8765


def pick_folder():
    """이 컴퓨터에서 폴더 선택 창을 띄운다. 고르지 않으면 빈 문자열."""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)   # 다른 창 뒤에 숨지 않게
        path = filedialog.askdirectory(title="AI가 고칠 프로젝트 폴더를 고르세요", mustexist=True)
        root.destroy()
        return os.path.normpath(path) if path else ""
    except Exception:
        return ""


PICK_LOCK = threading.Lock()   # 폴더 선택 창은 한 번에 하나만


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/firebase-config.json":   # 내 Firebase 프로젝트 설정 (GitHub 에는 올리지 않는 파일)
            try:
                with open(CONFIG_PATH, "rb") as f:
                    return self.reply(200, f.read(), "application/json; charset=utf-8")
            except OSError:
                return self.reply(404, b"{}", "application/json; charset=utf-8")
        if path == "/mcp_catalog.json":   # MCP 서버 목록
            try:
                with open(os.path.join(BASE_DIR, "mcp_catalog.json"), "rb") as f:
                    return self.reply(200, f.read(), "application/json; charset=utf-8")
            except OSError:
                return self.reply(404, b"[]", "application/json; charset=utf-8")
        if urllib.parse.unquote(path) == "/설명서.pdf":
            try:
                with open(os.path.join(BASE_DIR, "설명서.pdf"), "rb") as f:
                    return self.reply(200, f.read(), "application/pdf")
            except OSError:
                return self.reply(404, b"not found", "text/plain; charset=utf-8")
        if path.rstrip("/") not in ("", "/index.html", "/talk", "/code", "/login", "/setting", "/admin", "/openchat", "/feed", "/board"):   # 화면은 하나고 주소만 다르다
            return self.reply(404, b"not found", "text/plain; charset=utf-8")
        try:
            with open(INDEX_PATH, "rb") as f:
                page = f.read()
        except OSError:
            return self.reply(500, "index.html 파일을 찾을 수 없습니다.".encode("utf-8"), "text/plain; charset=utf-8")
        # 화면이 'AI 초대 명령'에 이 폴더의 경로를 넣어 보여 줄 수 있게 알려 준다
        page = page.replace(b"__APP_DIR__", json.dumps(BASE_DIR)[1:-1].encode("utf-8"))
        self.reply(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        """화면의 '폴더 선택' 버튼: 이 컴퓨터에 폴더 선택 창을 띄우고 고른 경로를 돌려준다."""
        host = (self.headers.get("Host") or "").lower()
        origin = (self.headers.get("Origin") or "").lower()
        local = host.split(":")[0] in ("localhost", "127.0.0.1")
        if self.path != "/pick-folder" or not local or origin not in (f"http://{host}", ""):
            return self.reply(404, b"not found", "text/plain; charset=utf-8")   # 다른 사이트가 부르는 것은 받지 않는다
        with PICK_LOCK:
            path = pick_folder()
        self.reply(200, json.dumps({"path": path}).encode("utf-8"), "application/json; charset=utf-8")

    def reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 윈도우에서는 이 옵션이 켜져 있으면 같은 포트에 서버가 두 개 떠 버릴 수 있다.
    allow_reuse_address = os.name != "nt"


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace") if stream.isatty() else stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="AI Talk 화면을 여는 작은 서버")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = ap.parse_args()

    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"서버를 켤 수 없습니다: {e}")
        print(f"이미 켜져 있거나 {args.port}번 포트를 다른 프로그램이 쓰고 있습니다.")
        sys.exit(1)

    url = f"http://localhost:{args.port}"
    print("=" * 56)
    print("  AI Talk 화면 서버가 켜졌습니다")
    print(f"  주소: {url}")
    print("  이 창을 닫으면 화면이 열리지 않습니다. (끄기: Ctrl+C)")
    print("=" * 56, flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")


if __name__ == "__main__":
    main()
