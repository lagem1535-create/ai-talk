#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Talk 서버
============
사람과 CLI 기반 AI(Claude Code, Antigravity CLI 등)가 함께 대화하는 오픈채팅방 서버입니다.
파이썬 표준 라이브러리만 사용하므로 따로 설치할 것이 없습니다.

    python server.py                     ->  http://127.0.0.1:8765
    python server.py --port 9000         ->  포트 바꾸기
    python server.py --host 0.0.0.0      ->  같은 와이파이의 다른 기기에서도 접속 허용

웹 화면은 index.html, AI 참가 프로그램은 ai_agent.py 입니다.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024      # 요청 본문 최대 크기(바이트)
MAX_TEXT = 8000           # 메시지 한 개 최대 글자 수
POLL_MAX = 25.0           # 롱폴링 최대 대기(초)
TYPING_TTL = 30.0         # '입력 중' 표시가 유지되는 시간(초)
ONLINE_TTL = 40.0         # 이 시간 안에 요청이 있었으면 '온라인'
CHAIN_DEFAULT = 6         # 사람 메시지 뒤에 AI 메시지가 연속으로 이어질 수 있는 기본 한도
CHAIN_MAX = 100
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 헷갈리는 0/O, 1/I 는 뺀다

NAME_MODES = ("anonymous", "realname", "alias")   # 익명방 / AI 이름방 / 가명방
ROOM_KINDS = ("open", "private")                  # 오픈채팅방 / 개인대화방(1:1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('human', 'ai')),
    salt        BLOB NOT NULL,
    pw_hash     BLOB NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('open', 'private')),
    name_mode       TEXT NOT NULL CHECK (name_mode IN ('anonymous', 'realname', 'alias')),
    code            TEXT UNIQUE,
    owner_id        INTEGER NOT NULL REFERENCES users(id),
    dm_key          TEXT UNIQUE,
    ai_paused       INTEGER NOT NULL DEFAULT 0,
    ai_chain_limit  INTEGER NOT NULL DEFAULT 6,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id       INTEGER NOT NULL REFERENCES rooms(id),
    user_id       INTEGER NOT NULL REFERENCES users(id),
    label         TEXT NOT NULL,
    anon_no       INTEGER,
    joined_at     REAL NOT NULL,
    left_at       REAL,
    last_read_id  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL REFERENCES rooms(id),
    member_id   INTEGER REFERENCES members(id),
    kind        TEXT NOT NULL CHECK (kind IN ('chat', 'system')),
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages (room_id, id);
CREATE INDEX IF NOT EXISTS idx_members_user ON members (user_id);
"""

# 가명방에서 가명을 비워 두면 지어 주는 이름
ALIAS_ADJ = ["졸린", "용감한", "수줍은", "배고픈", "느긋한", "엉뚱한", "씩씩한", "조용한",
             "반짝이는", "다정한", "산책하는", "노래하는", "춤추는", "꼼꼼한", "호기심 많은", "잠 못 드는"]
ALIAS_NOUN = ["고양이", "수달", "부엉이", "여우", "고래", "펭귄", "다람쥐", "너구리",
              "판다", "거북이", "두루미", "해파리", "고슴도치", "문어", "참새", "사막여우"]

DB = None       # Database
HUB = None      # Hub
VERBOSE = False
ALLOWED_HOSTS = {"localhost"}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ─────────────────────────────────────────────────────────────── 저장소

class Database:
    """SQLite 연결 하나를 락으로 보호해서 여러 스레드가 함께 쓴다."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """락을 잡고 트랜잭션 하나를 실행한다. (중첩해서 쓰지 말 것)"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")


class Hub:
    """접속 상태(온라인·입력 중)와 롱폴링 깨우기를 맡는 메모리 상태."""

    def __init__(self):
        self.cond = threading.Condition()
        self.base = int(time.time())   # 서버를 다시 켜면 버전이 달라지도록
        self.versions = {}             # user_id -> 변경 횟수
        self.polls = {}                # user_id -> 진행 중인 롱폴링 수
        self.seen = {}                 # user_id -> 마지막 요청 시각
        self.online = set()            # 온라인으로 알려진 user_id
        self.typing = {}               # room_id -> {member_id: (label, is_ai, 만료 시각)}

    def version(self, uid):
        with self.cond:
            return self.base + self.versions.get(uid, 0)

    def bump(self, user_ids):
        """이 사용자들의 화면이 바뀌어야 함을 알린다."""
        with self.cond:
            for uid in user_ids:
                self.versions[uid] = self.versions.get(uid, 0) + 1
            self.cond.notify_all()

    def touch(self, uid):
        """요청이 올 때마다 호출. 방금 온라인이 되었으면 True."""
        with self.cond:
            self.seen[uid] = time.time()
            if uid in self.online:
                return False
            self.online.add(uid)
            return True

    def poll_begin(self, uid):
        with self.cond:
            self.polls[uid] = self.polls.get(uid, 0) + 1

    def poll_end(self, uid):
        with self.cond:
            self.polls[uid] = max(0, self.polls.get(uid, 1) - 1)
            self.seen[uid] = time.time()

    def is_online(self, uid, now):
        with self.cond:
            return self.polls.get(uid, 0) > 0 or now - self.seen.get(uid, 0) < ONLINE_TTL

    def sweep(self, now):
        """오프라인이 된 사용자와 '입력 중'이 만료된 방을 찾아 돌려준다."""
        with self.cond:
            gone = [u for u in self.online
                    if not (self.polls.get(u, 0) > 0 or now - self.seen.get(u, 0) < ONLINE_TTL)]
            self.online.difference_update(gone)
            rooms = []
            for rid, entries in list(self.typing.items()):
                expired = [k for k, v in entries.items() if v[2] <= now]
                for k in expired:
                    del entries[k]
                if expired:
                    rooms.append(rid)
                if not entries:
                    del self.typing[rid]
            return gone, rooms

    def typing_on(self, rid, mid, label, is_ai, now):
        """'입력 중' 표시를 켠다. AI는 한 방에서 한 번에 하나만 말할 수 있다(발언권).
        반환: (허용 여부, 지금 말하는 중인 다른 AI 이름, 새로 켜졌는지)"""
        with self.cond:
            entries = self.typing.setdefault(rid, {})
            for k in [k for k, v in entries.items() if v[2] <= now]:
                del entries[k]
            if is_ai:
                for k, v in entries.items():
                    if k != mid and v[1]:
                        return False, v[0], False
            changed = mid not in entries
            entries[mid] = (label, is_ai, now + TYPING_TTL)
            return True, None, changed

    def typing_off(self, rid, mid):
        with self.cond:
            entries = self.typing.get(rid)
            if not entries or mid not in entries:
                return False
            del entries[mid]
            if not entries:
                del self.typing[rid]
            return True

    def typing_labels(self, rid, exclude_mid, now):
        with self.cond:
            entries = self.typing.get(rid) or {}
            return [v[0] for k, v in entries.items() if k != exclude_mid and v[2] > now]

    def forget_room(self, rid):
        with self.cond:
            self.typing.pop(rid, None)


def bump_room(rid, extra=()):
    with DB.lock:
        ids = {r[0] for r in DB.conn.execute(
            "SELECT user_id FROM members WHERE room_id = ? AND left_at IS NULL", (rid,))}
    HUB.bump(ids | set(extra))


def bump_user_rooms(uid):
    """이 사용자와 같은 방에 있는 모두에게 알린다. (온라인 표시가 바뀔 때)"""
    with DB.lock:
        ids = {r[0] for r in DB.conn.execute(
            "SELECT DISTINCT b.user_id FROM members a JOIN members b ON b.room_id = a.room_id "
            "WHERE a.user_id = ? AND a.left_at IS NULL AND b.left_at IS NULL", (uid,))}
    HUB.bump(ids | {uid})


def janitor():
    while True:
        time.sleep(5)
        try:
            gone, rooms = HUB.sweep(time.time())
            for uid in gone:
                bump_user_rooms(uid)
            for rid in rooms:
                bump_room(rid)
        except Exception:
            traceback.print_exc()


# ─────────────────────────────────────────────────────────────── 도우미

CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
USERNAME_RE = re.compile(r"^[\w.\-]{2,20}$")
ROOM_RE = re.compile(r"^/api/rooms/(\d+)/([a-z]+)$")


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(value, max_len):
    """이름류 입력: 제어문자를 지우고 공백을 한 칸으로 줄인다. 잘못된 값이면 빈 문자열."""
    if not isinstance(value, str):
        return ""
    return " ".join(CTRL_RE.sub("", value).split())[:max_len].strip()


def clean_message(value):
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", "메시지가 올바르지 않습니다.")
    value = CTRL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not value:
        raise ApiError(400, "empty", "빈 메시지는 보낼 수 없습니다.")
    if len(value) > MAX_TEXT:
        raise ApiError(400, "too_long", f"메시지가 너무 깁니다. (최대 {MAX_TEXT}자)")
    return value


def int_of(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first(q, key, default=""):
    return (q.get(key) or [default])[0]


def norm_code(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper()) if isinstance(value, str) else ""


def fmt_code(code):
    return f"{code[:4]}-{code[4:]}" if code else None


def new_code(c):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if not c.execute("SELECT 1 FROM rooms WHERE code = ?", (code,)).fetchone():
            return code


def random_alias():
    return f"{random.choice(ALIAS_ADJ)} {random.choice(ALIAS_NOUN)}"


def unique_label(c, rid, base):
    taken = {r[0] for r in c.execute("SELECT label FROM members WHERE room_id = ?", (rid,))}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def make_label(c, room, user, alias):
    """방의 이름 방식에 따라 이 참가자가 방에서 보일 이름을 정한다. 반환: (이름, 익명 번호)"""
    rid = room["id"]
    if room["name_mode"] == "anonymous" and user["kind"] == "ai":
        n = c.execute("SELECT COALESCE(MAX(anon_no), 0) + 1 FROM members WHERE room_id = ?",
                      (rid,)).fetchone()[0]
        return unique_label(c, rid, f"익명 {n}"), n
    if room["name_mode"] == "alias":
        return unique_label(c, rid, alias or random_alias()), None
    return unique_label(c, rid, user["name"]), None


def add_system(c, rid, text, now):
    return c.execute(
        "INSERT INTO messages (room_id, member_id, kind, text, created_at) VALUES (?, NULL, 'system', ?, ?)",
        (rid, text, now)).lastrowid


def require_member(c, rid, uid):
    """방과 내 참가 정보를 돌려준다. 참가자가 아니면 방이 없는 것처럼 답한다."""
    room = c.execute("SELECT * FROM rooms WHERE id = ?", (rid,)).fetchone()
    me = room and c.execute(
        "SELECT * FROM members WHERE room_id = ? AND user_id = ? AND left_at IS NULL",
        (rid, uid)).fetchone()
    if not me:
        raise ApiError(404, "no_room", "대화방을 찾을 수 없습니다.")
    return room, me


def require_owner(room, user):
    if room["owner_id"] != user["id"]:
        raise ApiError(403, "not_owner", "방장만 할 수 있습니다.")


def last_human_id(c, rid):
    return c.execute(
        "SELECT COALESCE(MAX(m.id), 0) FROM messages m "
        "JOIN members mb ON mb.id = m.member_id JOIN users u ON u.id = mb.user_id "
        "WHERE m.room_id = ? AND m.kind = 'chat' AND u.kind = 'human'", (rid,)).fetchone()[0]


def ai_streak(c, rid, after_id):
    """마지막 사람 메시지 뒤에 이어진 AI 메시지 수."""
    return c.execute(
        "SELECT COUNT(*) FROM messages WHERE room_id = ? AND kind = 'chat' AND id > ?",
        (rid, after_id)).fetchone()[0]


def delete_room(c, rid):
    c.execute("DELETE FROM messages WHERE room_id = ?", (rid,))
    c.execute("DELETE FROM members WHERE room_id = ?", (rid,))
    c.execute("DELETE FROM rooms WHERE id = ?", (rid,))


# ─────────────────────────────────────────────────────────────── 화면용 데이터

MSG_SELECT = (
    "SELECT m.id, m.kind, m.text, m.created_at, m.member_id, mb.label, u.kind AS ukind "
    "FROM messages m LEFT JOIN members mb ON mb.id = m.member_id LEFT JOIN users u ON u.id = mb.user_id "
)


def user_view(u):
    return {"username": u["username"], "name": u["name"], "kind": u["kind"]}


def message_view(row, viewer_mid):
    if row["kind"] == "system":
        return {"id": row["id"], "kind": "system", "mid": None, "name": "", "mine": False,
                "text": row["text"], "ts": row["created_at"]}
    return {"id": row["id"], "kind": row["ukind"], "mid": row["member_id"], "name": row["label"],
            "mine": row["member_id"] == viewer_mid, "text": row["text"], "ts": row["created_at"]}


def room_view(c, room, uid, now):
    """보는 사람 기준의 방 정보. 익명방에서는 AI의 실제 계정 정보가 절대 나가지 않는다."""
    rid = room["id"]
    rows = c.execute(
        "SELECT m.id, m.user_id, m.label, m.left_at, m.last_read_id, u.kind "
        "FROM members m JOIN users u ON u.id = m.user_id WHERE m.room_id = ? ORDER BY m.id",
        (rid,)).fetchall()
    me = next(m for m in rows if m["user_id"] == uid)
    active = [m for m in rows if m["left_at"] is None]

    if room["name"]:
        title = room["name"]
    else:  # 참가자 목록에서 시작한 1:1 대화는 상대 이름이 곧 방 이름
        others = [m for m in rows if m["user_id"] != uid]
        pick = [m for m in others if m["left_at"] is None] or others
        title = pick[0]["label"] if pick else "1:1 대화"

    last = c.execute(MSG_SELECT + "WHERE m.room_id = ? ORDER BY m.id DESC LIMIT 1", (rid,)).fetchone()
    unread = c.execute(
        "SELECT COUNT(*) FROM messages WHERE room_id = ? AND kind = 'chat' AND id > ? AND member_id != ?",
        (rid, me["last_read_id"], me["id"])).fetchone()[0]
    return {
        "id": rid,
        "name": title,
        "kind": room["kind"],
        "is_dm": bool(room["dm_key"]),
        "name_mode": room["name_mode"],
        "code": fmt_code(room["code"]),
        "is_owner": room["owner_id"] == uid,
        "ai_paused": bool(room["ai_paused"]),
        "ai_chain_limit": room["ai_chain_limit"],
        "ai_streak": ai_streak(c, rid, last_human_id(c, rid)),
        "last_id": last["id"] if last else 0,
        "last": ({"name": last["label"] or "", "kind": last["kind"],
                  "text": last["text"][:120], "ts": last["created_at"]} if last else None),
        "sort": last["created_at"] if last else room["created_at"],
        "unread": unread,
        "me": {"mid": me["id"], "label": me["label"], "last_read_id": me["last_read_id"]},
        "members": [{"mid": m["id"], "label": m["label"], "kind": m["kind"],
                     "online": HUB.is_online(m["user_id"], now),
                     "owner": m["user_id"] == room["owner_id"], "me": m["user_id"] == uid}
                    for m in active],
        "typing": HUB.typing_labels(rid, me["id"], now),
    }


# ─────────────────────────────────────────────────────────────── API: 계정

def new_session(c, uid, now):
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
              (token_hash(token), uid, now))
    return token


def api_register(body):
    username, password = body.get("username"), body.get("password")
    kind = body.get("kind") or "human"
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise ApiError(400, "bad_username", "아이디는 2~20자의 글자·숫자·밑줄(_)·점(.)·하이픈(-)만 쓸 수 있습니다.")
    if not isinstance(password, str) or not 4 <= len(password) <= 100:
        raise ApiError(400, "bad_password", "비밀번호는 4자 이상이어야 합니다.")
    if kind not in ("human", "ai"):
        raise ApiError(400, "bad_request", "계정 종류가 올바르지 않습니다.")
    name = clean_name(body.get("name"), 20) or username
    salt = secrets.token_bytes(16)
    pw = hash_pw(password, salt)
    now = time.time()
    with DB.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ApiError(409, "username_taken", "이미 사용 중인 아이디입니다.")
        uid = c.execute(
            "INSERT INTO users (username, name, kind, salt, pw_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (username, name, kind, salt, pw, now)).lastrowid
        token = new_session(c, uid, now)
        user = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return {"token": token, "user": user_view(user)}


def api_login(body):
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ApiError(400, "bad_request", "아이디와 비밀번호를 입력하세요.")
    with DB.lock:
        user = DB.conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    digest = hash_pw(password[:100], user["salt"] if user else b"\0" * 16)
    if not user or not hmac.compare_digest(digest, user["pw_hash"]):
        time.sleep(0.3)
        raise ApiError(401, "bad_credentials", "아이디 또는 비밀번호가 올바르지 않습니다.")
    with DB.tx() as c:
        token = new_session(c, user["id"], time.time())
    return {"token": token, "user": user_view(user)}


def api_updates(user, q):
    """롱폴링: 내 화면에 바뀐 것이 생길 때까지(최대 25초) 기다렸다가 내 방 목록을 돌려준다."""
    uid = user["id"]
    client_v = int_of(first(q, "v"), -1)
    try:
        wait = max(0.0, min(float(first(q, "wait", str(POLL_MAX))), POLL_MAX))
    except ValueError:
        wait = POLL_MAX
    HUB.poll_begin(uid)
    try:
        with HUB.cond:
            HUB.cond.wait_for(lambda: HUB.version(uid) != client_v, timeout=wait)
            v = HUB.version(uid)
    finally:
        HUB.poll_end(uid)
    now = time.time()
    with DB.lock:
        rows = DB.conn.execute(
            "SELECT r.* FROM rooms r JOIN members m ON m.room_id = r.id "
            "WHERE m.user_id = ? AND m.left_at IS NULL", (uid,)).fetchall()
        rooms = [room_view(DB.conn, r, uid, now) for r in rows]
    rooms.sort(key=lambda r: r["sort"], reverse=True)
    return {"v": v, "rooms": rooms}


# ─────────────────────────────────────────────────────────────── API: 방

def api_create_room(user, body):
    kind = body.get("kind") or "open"
    mode = body.get("name_mode") or "realname"
    name = clean_name(body.get("name"), 40)
    if kind not in ROOM_KINDS or mode not in NAME_MODES:
        raise ApiError(400, "bad_request", "방 설정이 올바르지 않습니다.")
    if not name:
        raise ApiError(400, "bad_name", "방 이름을 입력하세요.")
    alias = clean_name(body.get("alias"), 20)
    now = time.time()
    with DB.tx() as c:
        rid = c.execute(
            "INSERT INTO rooms (name, kind, name_mode, code, owner_id, ai_chain_limit, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, kind, mode, new_code(c), user["id"], CHAIN_DEFAULT, now)).lastrowid
        room = c.execute("SELECT * FROM rooms WHERE id = ?", (rid,)).fetchone()
        label, anon = make_label(c, room, user, alias)
        msg_id = add_system(c, rid, "대화방이 만들어졌습니다. 초대코드를 AI에게 전해 주세요.", now)
        c.execute(
            "INSERT INTO members (room_id, user_id, label, anon_no, joined_at, last_read_id) "
            "VALUES (?, ?, ?, ?, ?, ?)", (rid, user["id"], label, anon, now, msg_id))
        view = room_view(c, room, user["id"], now)
    HUB.bump([user["id"]])
    return {"room": view}


def api_join(user, body):
    code = norm_code(body.get("code"))
    alias = clean_name(body.get("alias"), 20)
    now = time.time()
    with DB.tx() as c:
        room = c.execute("SELECT * FROM rooms WHERE code = ?", (code,)).fetchone() if code else None
        if not room:
            raise ApiError(404, "bad_code", "초대코드에 해당하는 대화방이 없습니다.")
        rid = room["id"]
        me = c.execute("SELECT * FROM members WHERE room_id = ? AND user_id = ?",
                       (rid, user["id"])).fetchone()
        joined = not (me and me["left_at"] is None)
        if joined:
            count = c.execute("SELECT COUNT(*) FROM members WHERE room_id = ? AND left_at IS NULL",
                              (rid,)).fetchone()[0]
            if room["kind"] == "private" and count >= 2:
                raise ApiError(409, "room_full", "이미 두 명이 대화 중인 개인대화방입니다.")
            if me:  # 나갔다가 다시 들어오면 예전 이름을 그대로 쓴다
                label = me["label"]
                c.execute("UPDATE members SET left_at = NULL, joined_at = ? WHERE id = ?", (now, me["id"]))
                mid = me["id"]
            else:
                label, anon = make_label(c, room, user, alias)
                mid = c.execute(
                    "INSERT INTO members (room_id, user_id, label, anon_no, joined_at) VALUES (?, ?, ?, ?, ?)",
                    (rid, user["id"], label, anon, now)).lastrowid
            msg_id = add_system(c, rid, f"{label} 님이 들어왔습니다.", now)
            c.execute("UPDATE members SET last_read_id = ? WHERE id = ?", (msg_id, mid))
        view = room_view(c, room, user["id"], now)
    if joined:
        bump_room(rid)
    return {"room": view, "joined": joined}


def api_messages(user, rid, q):
    after, before = int_of(first(q, "after")), int_of(first(q, "before"))
    limit = max(1, min(int_of(first(q, "limit"), 60), 200))
    now = time.time()
    with DB.lock:
        c = DB.conn
        room, me = require_member(c, rid, user["id"])
        if after > 0:
            rows = c.execute(MSG_SELECT + "WHERE m.room_id = ? AND m.id > ? ORDER BY m.id LIMIT ?",
                             (rid, after, limit)).fetchall()
            has_more = False
        else:
            cond, args = ("AND m.id < ? ", (rid, before)) if before > 0 else ("", (rid,))
            rows = c.execute(MSG_SELECT + "WHERE m.room_id = ? " + cond + "ORDER BY m.id DESC LIMIT ?",
                             args + (limit + 1,)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit][::-1]
        view = room_view(c, room, user["id"], now)
    return {"messages": [message_view(r, me["id"]) for r in rows], "has_more": has_more, "room": view}


def api_send(user, rid, body):
    text = clean_message(body.get("text"))
    now = time.time()
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        if user["kind"] == "ai":
            # AI가 끝없이 떠들지 않도록 서버에서도 한 번 더 막는다.
            if room["ai_paused"]:
                raise ApiError(409, "ai_paused", "방장이 AI 응답을 일시정지했습니다.")
            human_id = last_human_id(c, rid)
            answering_human = human_id > me["last_read_id"]   # 아직 답하지 않은 사람 메시지가 있음
            if not answering_human and ai_streak(c, rid, human_id) >= room["ai_chain_limit"]:
                raise ApiError(409, "ai_chain_limit", "AI끼리의 연속 대화 한도에 도달했습니다.")
        msg_id = c.execute(
            "INSERT INTO messages (room_id, member_id, kind, text, created_at) VALUES (?, ?, 'chat', ?, ?)",
            (rid, me["id"], text, now)).lastrowid
        if user["kind"] == "human":
            c.execute("UPDATE members SET last_read_id = ? WHERE id = ?", (msg_id, me["id"]))
        row = c.execute(MSG_SELECT + "WHERE m.id = ?", (msg_id,)).fetchone()
    HUB.typing_off(rid, me["id"])
    bump_room(rid)
    return {"message": message_view(row, me["id"])}


def api_typing(user, rid, body):
    with DB.lock:
        room, me = require_member(DB.conn, rid, user["id"])
    if not body.get("on"):
        if HUB.typing_off(rid, me["id"]):
            bump_room(rid)
        return {"ok": True}
    is_ai = user["kind"] == "ai"
    if is_ai and room["ai_paused"]:
        return {"ok": True, "granted": False, "busy": None}
    granted, busy, changed = HUB.typing_on(rid, me["id"], me["label"], is_ai, time.time())
    if changed:
        bump_room(rid)
    return {"ok": True, "granted": granted, "busy": busy}


def api_read(user, rid, body):
    last_id = int_of(body.get("last_id"), -1)
    if last_id < 0:
        raise ApiError(400, "bad_request", "last_id 가 필요합니다.")
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        top = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", (rid,)).fetchone()[0]
        new = max(me["last_read_id"], min(last_id, top))
        changed = new != me["last_read_id"]
        if changed:
            c.execute("UPDATE members SET last_read_id = ? WHERE id = ?", (new, me["id"]))
    if changed and user["kind"] == "human":
        HUB.bump([user["id"]])   # 다른 탭의 안 읽음 숫자도 맞춘다
    return {"ok": True, "last_read_id": new}


def api_settings(user, rid, body):
    now = time.time()
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        require_owner(room, user)
        if "ai_paused" in body:
            paused = 1 if body["ai_paused"] else 0
            if paused != room["ai_paused"]:
                c.execute("UPDATE rooms SET ai_paused = ? WHERE id = ?", (paused, rid))
                add_system(c, rid, "AI 응답을 일시정지했습니다." if paused else "AI 응답을 다시 시작했습니다.", now)
        if "ai_chain_limit" in body:
            limit = int_of(body["ai_chain_limit"], -1)
            if not 0 <= limit <= CHAIN_MAX:
                raise ApiError(400, "bad_request", f"연속 대화 한도는 0~{CHAIN_MAX} 사이여야 합니다.")
            c.execute("UPDATE rooms SET ai_chain_limit = ? WHERE id = ?", (limit, rid))
        if "name" in body:
            name = clean_name(body["name"], 40)
            if not name or not room["name"]:
                raise ApiError(400, "bad_name", "방 이름을 바꿀 수 없습니다.")
            if name != room["name"]:
                c.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, rid))
                add_system(c, rid, f"방 이름이 '{name}'(으)로 바뀌었습니다.", now)
        room = c.execute("SELECT * FROM rooms WHERE id = ?", (rid,)).fetchone()
        view = room_view(c, room, user["id"], now)
    bump_room(rid)
    return {"room": view}


def api_new_code(user, rid, body):
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        require_owner(room, user)
        if not room["code"]:
            raise ApiError(400, "no_code", "초대코드가 없는 대화방입니다.")
        code = new_code(c)
        c.execute("UPDATE rooms SET code = ? WHERE id = ?", (code, rid))
    bump_room(rid)
    return {"code": fmt_code(code)}


def api_leave(user, rid, body):
    now = time.time()
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        if room["owner_id"] == user["id"] and not room["dm_key"]:
            raise ApiError(400, "owner_cannot_leave", "방장은 나갈 수 없습니다. 대신 방을 삭제할 수 있습니다.")
        c.execute("UPDATE members SET left_at = ? WHERE id = ?", (now, me["id"]))
        left = c.execute("SELECT COUNT(*) FROM members WHERE room_id = ? AND left_at IS NULL",
                         (rid,)).fetchone()[0]
        if left == 0:
            delete_room(c, rid)
        else:
            add_system(c, rid, f"{me['label']} 님이 나갔습니다.", now)
    HUB.typing_off(rid, me["id"])
    bump_room(rid, extra=[user["id"]])
    return {"ok": True}


def api_kick(user, rid, body):
    now = time.time()
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        require_owner(room, user)
        target = c.execute("SELECT * FROM members WHERE id = ? AND room_id = ? AND left_at IS NULL",
                           (int_of(body.get("mid"), -1), rid)).fetchone()
        if not target or target["user_id"] == user["id"]:
            raise ApiError(400, "bad_request", "내보낼 참가자를 찾을 수 없습니다.")
        c.execute("UPDATE members SET left_at = ? WHERE id = ?", (now, target["id"]))
        add_system(c, rid, f"{target['label']} 님을 내보냈습니다.", now)
    HUB.typing_off(rid, target["id"])
    bump_room(rid, extra=[target["user_id"]])
    return {"ok": True}


def api_dm(user, rid, body):
    """참가자 목록에서 고른 상대와 1:1 개인대화방을 연다. 원래 방에서 쓰던 이름(익명·가명)을 그대로 쓴다."""
    now = time.time()
    with DB.tx() as c:
        origin, me = require_member(c, rid, user["id"])
        target = c.execute("SELECT * FROM members WHERE id = ? AND room_id = ? AND left_at IS NULL",
                           (int_of(body.get("mid"), -1), rid)).fetchone()
        if not target or target["user_id"] == user["id"]:
            raise ApiError(400, "bad_request", "1:1 대화를 할 참가자를 찾을 수 없습니다.")
        if origin["kind"] == "private":
            raise ApiError(400, "bad_request", "이미 1:1 대화방입니다.")
        a, b = sorted((user["id"], target["user_id"]))
        key = f"{rid}:{a}:{b}"
        room = c.execute("SELECT * FROM rooms WHERE dm_key = ?", (key,)).fetchone()
        if room:
            c.execute("UPDATE members SET left_at = NULL WHERE room_id = ?", (room["id"],))
        else:
            new_id = c.execute(
                "INSERT INTO rooms (name, kind, name_mode, code, owner_id, dm_key, ai_chain_limit, created_at) "
                "VALUES ('', 'private', ?, NULL, ?, ?, ?, ?)",
                (origin["name_mode"], user["id"], key, CHAIN_DEFAULT, now)).lastrowid
            for m in (me, target):
                c.execute(
                    "INSERT INTO members (room_id, user_id, label, anon_no, joined_at) VALUES (?, ?, ?, ?, ?)",
                    (new_id, m["user_id"], m["label"], m["anon_no"], now))
            add_system(c, new_id, f"'{origin['name']}' 방에서 시작한 1:1 대화입니다.", now)
            room = c.execute("SELECT * FROM rooms WHERE id = ?", (new_id,)).fetchone()
        view = room_view(c, room, user["id"], now)
    bump_room(room["id"])
    return {"room": view}


def api_delete(user, rid, body):
    with DB.tx() as c:
        room, me = require_member(c, rid, user["id"])
        require_owner(room, user)
        ids = [r[0] for r in c.execute("SELECT user_id FROM members WHERE room_id = ?", (rid,))]
        delete_room(c, rid)
    HUB.forget_room(rid)
    HUB.bump(ids)
    return {"ok": True}


ROOM_ACTIONS = {
    ("GET", "messages"): api_messages,
    ("POST", "messages"): api_send,
    ("POST", "typing"): api_typing,
    ("POST", "read"): api_read,
    ("POST", "settings"): api_settings,
    ("POST", "code"): api_new_code,
    ("POST", "leave"): api_leave,
    ("POST", "kick"): api_kick,
    ("POST", "dm"): api_dm,
    ("POST", "delete"): api_delete,
}


# ─────────────────────────────────────────────────────────────── HTTP

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def host_allowed(header):
    """DNS 리바인딩을 막기 위해 IP 주소·localhost·이 컴퓨터 이름으로 온 요청만 받는다."""
    host = (header or "").strip().lower()
    if not host:
        return True
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AITalk/" + VERSION
    sys_version = ""
    timeout = 75

    def log_message(self, fmt, *args):
        if VERBOSE:
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_bytes(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def read_json(self):
        length = int_of(self.headers.get("Content-Length"), 0)
        if length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "too_large", "요청이 너무 큽니다.")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            raise ApiError(415, "bad_request", "JSON 형식으로 보내야 합니다.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_request", "요청 형식이 올바르지 않습니다.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_request", "요청 형식이 올바르지 않습니다.")
        return data

    def check_origin(self):
        """다른 사이트가 이 서버로 몰래 요청을 보내는 것을 막는다."""
        if not host_allowed(self.headers.get("Host")):
            raise ApiError(403, "bad_host", "허용되지 않은 주소로 접속했습니다.")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            raise ApiError(403, "bad_origin", "다른 사이트에서 온 요청은 받지 않습니다.")

    def auth(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            raise ApiError(401, "unauthorized", "로그인이 필요합니다.")
        self.token_hash = token_hash(header[7:].strip())
        with DB.lock:
            user = DB.conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (self.token_hash,)).fetchone()
        if not user:
            raise ApiError(401, "unauthorized", "로그인이 필요합니다.")
        if HUB.touch(user["id"]):
            bump_user_rooms(user["id"])
        return user

    def route(self, method, path, q, body):
        if method == "GET" and path == "/api/health":
            return {"ok": True, "app": "AI Talk", "version": VERSION}
        if method == "POST" and path == "/api/register":
            return api_register(body)
        if method == "POST" and path == "/api/login":
            return api_login(body)

        user = self.auth()
        if method == "GET" and path == "/api/me":
            return {"user": user_view(user)}
        if method == "POST" and path == "/api/logout":
            with DB.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash = ?", (self.token_hash,))
            return {"ok": True}
        if method == "GET" and path == "/api/updates":
            return api_updates(user, q)
        if method == "POST" and path == "/api/rooms":
            return api_create_room(user, body)
        if method == "POST" and path == "/api/join":
            return api_join(user, body)
        m = ROOM_RE.match(path)
        fn = m and ROOM_ACTIONS.get((method, m.group(2)))
        if fn:
            return fn(user, int(m.group(1)), q if method == "GET" else body)
        raise ApiError(404, "not_found", "없는 주소입니다.")

    def dispatch(self, method):
        try:
            try:
                body = self.read_json() if method == "POST" else {}
                self.check_origin()
                url = urlparse(self.path)
                if method == "GET" and url.path in ("/", "/index.html"):
                    return self.serve_index()
                if method == "GET" and url.path == "/favicon.ico":
                    return self.send_bytes(204, b"", "image/x-icon")
                if not url.path.startswith("/api/"):
                    raise ApiError(404, "not_found", "없는 주소입니다.")
                self.send_json(200, self.route(method, url.path, parse_qs(url.query), body))
            except ApiError as e:
                self.send_json(e.status, {"error": e.code, "message": e.message})
        except (ConnectionError, TimeoutError):
            self.close_connection = True   # 기다리는 동안 브라우저가 연결을 끊은 경우
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            try:
                self.send_json(500, {"error": "server_error", "message": "서버 오류가 발생했습니다."})
            except Exception:
                pass

    def serve_index(self):
        try:
            with open(INDEX_PATH, "rb") as f:
                page = f.read()
        except OSError:
            return self.send_bytes(500, "index.html 파일을 찾을 수 없습니다.".encode("utf-8"),
                                   "text/plain; charset=utf-8")
        self.send_bytes(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    # 윈도우에서는 이 옵션이 켜져 있으면 같은 포트에 서버가 두 개 떠 버릴 수 있다.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # 실제로 패킷을 보내지는 않는다
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    global DB, HUB, VERBOSE
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="AI Talk 서버")
    ap.add_argument("--host", default="127.0.0.1",
                    help="접속을 받을 주소. 같은 와이파이의 다른 기기에서도 쓰려면 0.0.0.0 (기본: 이 컴퓨터만)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    ap.add_argument("--data", default=os.path.join(BASE_DIR, "data"), help="대화 기록을 저장할 폴더")
    ap.add_argument("--allow-host", action="append", default=[], help="추가로 허용할 접속 주소(도메인 이름)")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--verbose", action="store_true", help="요청 기록을 화면에 출력")
    args = ap.parse_args()

    VERBOSE = args.verbose
    ALLOWED_HOSTS.update(h.lower() for h in [socket.gethostname(), *args.allow_host] if h)
    DB = Database(os.path.join(args.data, "chat.db"))
    HUB = Hub()

    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"서버를 켤 수 없습니다: {e}")
        print(f"이미 서버가 켜져 있거나 {args.port}번 포트를 다른 프로그램이 쓰고 있습니다.")
        print("다른 포트로 켜려면:  python server.py --port 9000")
        sys.exit(1)

    threading.Thread(target=janitor, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 56)
    print("  AI Talk 서버가 켜졌습니다")
    print(f"  주소: {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        ip = lan_ip()
        if ip:
            print(f"  다른 기기에서: http://{ip}:{args.port}")
    print("  이 창을 닫으면 서버가 꺼집니다. (끄기: Ctrl+C)")
    print("=" * 56, flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")


if __name__ == "__main__":
    main()
