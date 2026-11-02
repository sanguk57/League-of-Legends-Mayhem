"""OP.GG MCP 서버에서 칼바람 빌드와 증강 통계를 가져온다.

OP.GG가 AI 에이전트용으로 공개한 서버라 응답 형식이 예고 없이 바뀔 수 있다.
"""

import hashlib
import json
import re
import time
import urllib.request

from .ddragon import DATA_DIR

MCP_URL = "https://mcp-api.op.gg/mcp"
CACHE_DIR = DATA_DIR / "cache" / "opgg"
CACHE_TTL = 6 * 3600

ITEM_FIELDS = "{ids[],ids_names[],pick_rate,play,win}"
BUILD_FIELDS = [
    "data.summary.average_stats.{play,win_rate}",
    f"data.core_items.{ITEM_FIELDS}",
    f"data.boots.{ITEM_FIELDS}",
    f"data.starter_items.{ITEM_FIELDS}",
    f"data.fourth_items[].{ITEM_FIELDS}",
    f"data.fifth_items[].{ITEM_FIELDS}",
    f"data.sixth_items[].{ITEM_FIELDS}",
]
AUGMENT_FIELDS = ["data.augments[].{desc,id,name,performance,popular,tier}"]


class OpggError(RuntimeError):
    pass


class _Parser:
    """OP.GG 응답 텍스트(`class X: a,b` 헤더 + `X(1,"a",[...])` 본문)를 dict로 바꾼다."""

    NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
    IDENT = re.compile(r"[A-Za-z_]\w*")

    def __init__(self, text):
        self.classes = {}
        body = []
        for line in text.splitlines():
            m = re.match(r"class (\w+):\s*(.*)$", line)
            if m:
                self.classes[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
            elif line.strip():
                body.append(line)
        self.src = "\n".join(body)
        self.pos = 0

    def parse(self):
        value = self._value()
        self._skip()
        if self.pos != len(self.src):
            raise OpggError(f"OP.GG 응답 해석 실패 (위치 {self.pos})")
        return value

    def _skip(self):
        while self.pos < len(self.src) and self.src[self.pos] in " \n\t\r":
            self.pos += 1

    def _value(self):
        self._skip()
        ch = self.src[self.pos]
        if ch == '"':
            return self._string()
        if ch == "[":
            return self._sequence("[", "]")
        m = self.NUMBER.match(self.src, self.pos)
        if m:
            self.pos = m.end()
            text = m.group()
            return float(text) if any(c in text for c in ".eE") else int(text)
        m = self.IDENT.match(self.src, self.pos)
        if not m:
            raise OpggError(f"OP.GG 응답 해석 실패 (위치 {self.pos})")
        self.pos = m.end()
        name = m.group()
        if self.pos < len(self.src) and self.src[self.pos] == "(":
            args = self._sequence("(", ")")
            fields = self.classes.get(name, [f"_{i}" for i in range(len(args))])
            return dict(zip(fields, args))
        return {"null": None, "None": None, "true": True, "false": False}.get(name, name)

    def _string(self):
        end = self.pos + 1
        while self.src[end] != '"':
            end += 2 if self.src[end] == "\\" else 1
        text = self.src[self.pos:end + 1]
        self.pos = end + 1
        return json.loads(text)

    def _sequence(self, open_ch, close_ch):
        self.pos += 1
        items = []
        self._skip()
        if self.src[self.pos] == close_ch:
            self.pos += 1
            return items
        while True:
            items.append(self._value())
            self._skip()
            ch = self.src[self.pos]
            self.pos += 1
            if ch == close_ch:
                return items
            if ch != ",":
                raise OpggError(f"OP.GG 응답 해석 실패 (위치 {self.pos})")


def parse_response_text(text):
    return _Parser(text).parse()


def call_tool(name, arguments, timeout=40, use_cache=True):
    key = hashlib.sha1(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"{name}_{key}.json"
    if use_cache and cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL:
        return json.loads(cache_file.read_text("utf-8"))

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "User-Agent": "aram-helper/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except OSError as e:
        raise OpggError(f"OP.GG 서버 연결 실패: {e}") from e

    # 응답이 SSE 형식(data: {...})으로 올 수도 있다
    m = re.search(r"^data:\s*(\{.*\})\s*$", raw, re.M)
    message = json.loads(m.group(1) if m else raw)
    if "error" in message:
        raise OpggError(f"OP.GG 오류: {message['error'].get('message')}")
    result = message["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise OpggError(f"OP.GG 오류: {text[:200]}")
    data = parse_response_text(text)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


def aram_build(champion_id, lang="ko_KR"):
    """칼바람 챔피언 통계와 아이템 빌드. champion_id는 Data Dragon id (예: MissFortune)."""
    # 칼바람에는 라인이 없지만 OP.GG가 position을 필수로 요구한다 (값은 결과에 영향 없음)
    data = call_tool("lol_get_champion_analysis", {
        "champion": champion_id.upper(), "game_mode": "aram", "position": "mid",
        "lang": lang, "desired_output_fields": BUILD_FIELDS})
    return data.get("data", {})


def aram_augments(champion_key, lang="ko_KR"):
    """챔피언별 칼바람 증강 통계. champion_key는 숫자 id (예: 81)."""
    data = call_tool("lol_list_aram_augments", {
        "champion_id": int(champion_key), "lang": lang,
        "desired_output_fields": AUGMENT_FIELDS})
    return data.get("data", {}).get("augments", [])


def item_popularity(build):
    """OP.GG 빌드 데이터에서 아이템별 (픽률, 승률, 판수, 출처)를 모은다. 같은 아이템은 픽률이 높은 쪽."""
    result = {}

    def add(entry, source):
        if not entry or not entry.get("play"):
            return
        win_rate = entry["win"] / entry["play"]
        for iid in entry.get("ids", []):
            iid = str(iid)
            current = result.get(iid)
            if current is None or entry["pick_rate"] > current["pick_rate"]:
                result[iid] = {"pick_rate": entry["pick_rate"], "win_rate": win_rate,
                               "play": entry["play"], "source": source}

    add(build.get("core_items"), "코어")
    add(build.get("boots"), "신발")
    for key, label in (("fourth_items", "4번째"), ("fifth_items", "5번째"), ("sixth_items", "6번째")):
        for entry in build.get(key) or []:
            add(entry, label)
    return result
