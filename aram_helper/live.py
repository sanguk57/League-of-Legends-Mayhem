"""게임 중 Live Client Data API(https://127.0.0.1:2999)에서 10명의 챔피언·아이템을 읽는다."""

import json
import ssl
import urllib.request
from collections import Counter

from .stats import Unit


def fetch_game(host="127.0.0.1", port=2999, timeout=3):
    # 롤 클라이언트는 자체 서명 인증서를 쓰므로 검증을 끈다 (로컬 접속 전용)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://{host}:{port}/liveclientdata/allgamedata"
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
        return json.load(resp)


def _champion(gd, player):
    raw = player.get("rawChampionName", "")
    if "displayname_" in raw:
        try:
            return gd.champion(raw.split("displayname_")[-1])
        except ValueError:
            pass
    return gd.champion(player["championName"])


def _item_ids(gd, player):
    ids = []
    for it in player.get("items", []):
        iid = str(it.get("itemID"))
        if iid in gd.items:
            ids.append(iid)
    return ids


def _augment_names(player):
    """증강 필드가 있으면 읽는다. Live Client Data API가 증강을 주는지 확인되지 않아
    'augment'가 들어간 키를 모두 살펴본다. 없으면 빈 목록."""
    names = []
    for key, value in player.items():
        if "augment" not in key.lower():
            continue
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, dict):
                entry = entry.get("displayName") or entry.get("name") or entry.get("id")
            if entry not in (None, "", 0):
                names.append(str(entry))
    return names


def _player_names(player):
    """비교용 이름들. 이벤트 로그에는 태그(#KR1) 없이 나오기도 하므로 둘 다 넣는다."""
    names = {player.get(k) for k in ("riotId", "summonerName", "riotIdGameName") if player.get(k)}
    return names | {n.split("#")[0] for n in names}


# 소환사 주문: rawDisplayName에 들어 있는 내부 이름 또는 한국어 표시 이름 -> 키
SPELL_KEYS = {"SummonerDot": "ignite", "SummonerExhaust": "exhaust", "SummonerHeal": "heal",
              "SummonerBarrier": "barrier", "SummonerBoost": "cleanse"}
SPELL_NAMES = {"점화": "ignite", "탈진": "exhaust", "회복": "heal", "방어막": "barrier", "정화": "cleanse"}


def _spells(player):
    out = []
    for slot in (player.get("summonerSpells") or {}).values():
        raw = slot.get("rawDisplayName", "")
        key = next((v for k, v in SPELL_KEYS.items() if k in raw), None)
        key = key or SPELL_NAMES.get(slot.get("displayName", ""))
        if key:
            out.append(key)
    return out


def _events(data):
    events = data.get("events") or {}
    return events.get("Events", []) if isinstance(events, dict) else events


def _killers_of(events, names):
    """names(나)를 처치한 플레이어 이름별 횟수."""
    return Counter(e.get("KillerName") for e in events
                   if e.get("EventName") == "ChampionKill" and e.get("VictimName") in names)


def parse_game(gd, data):
    """(나, 아군 목록, 적 목록, 게임 정보)를 반환한다."""
    active = data.get("activePlayer", {})
    my_names = _player_names(active)
    players = data.get("allPlayers", [])
    me_raw = next((p for p in players if _player_names(p) & my_names), None)
    if me_raw is None:
        raise ValueError("게임 데이터에서 내 챔피언을 찾지 못했습니다.")

    events = _events(data)
    killers = _killers_of(events, _player_names(me_raw))

    def unit(p, live_stats=None):
        u = Unit(gd, _champion(gd, p), p.get("level", 1), _item_ids(gd, p),
                 live_stats=live_stats, name=p.get("riotId") or p.get("summonerName"),
                 augment_names=_augment_names(p))
        scores = p.get("scores") or {}
        u.kills, u.deaths, u.assists = (scores.get(k, 0) for k in ("kills", "deaths", "assists"))
        u.spells = _spells(p)
        u.killed_me = sum(killers[n] for n in _player_names(p))
        return u

    me = unit(me_raw, active.get("championStats"))
    me.augment_names += [n for n in _augment_names(active) if n not in me.augment_names]
    allies = [unit(p) for p in players if p is not me_raw and p.get("team") == me_raw.get("team")]
    enemies = [unit(p) for p in players if p.get("team") != me_raw.get("team")]
    game = data.get("gameData", {})
    result = next((e.get("Result") for e in events if e.get("EventName") == "GameEnd"), None)
    info = {"mode": game.get("gameMode"), "time": game.get("gameTime", 0),
            "dead": bool(me_raw.get("isDead")), "respawn": me_raw.get("respawnTimer", 0),
            "gold": active.get("currentGold"), "result": result}
    return me, allies, enemies, info
