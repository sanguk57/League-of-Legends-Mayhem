"""Data Dragon 정적 데이터(아이템, 챔피언) 다운로드와 캐시."""

import json
import re
import urllib.request
from pathlib import Path

BASE_URL = "https://ddragon.leagueoflegends.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARAM_MAP = "12"


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "aram-helper/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def normalize(name):
    """이름 비교용: 공백, 따옴표, 마침표 등을 지우고 소문자로."""
    return re.sub(r"[\s'.&:\-]", "", str(name)).lower()


def _version_key(version):
    return tuple(int(p) for p in version.split(".") if p.isdigit())


def latest_version():
    try:
        return http_json(f"{BASE_URL}/api/versions.json")[0]
    except OSError:
        cached = [p.name for p in (DATA_DIR / "ddragon").glob("*") if p.is_dir()]
        if cached:
            return max(cached, key=_version_key)
        raise


def _cached_json(path, url, refresh=False):
    if path.exists() and not refresh:
        return json.loads(path.read_text("utf-8"))
    data = http_json(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


class GameData:
    def __init__(self, version, items, champions, lang="ko_KR"):
        self.version = version
        self.lang = lang
        self.items = items
        self.champions = champions
        self.champions_by_key = {c["key"]: c for c in champions.values()}

        self._champ_index = {}
        for cid, champ in champions.items():
            self._champ_index[normalize(cid)] = cid
            self._champ_index[normalize(champ["name"])] = cid

        # 같은 이름의 아이템이 여러 모드용으로 존재하므로 일반 칼바람 아이템을 우선한다.
        self._item_index = {}
        for iid, item in sorted(items.items(), key=lambda kv: not self.is_standard(kv[0])):
            self._item_index.setdefault(normalize(item["name"]), iid)

    def is_standard(self, item_id):
        item = self.items[item_id]
        return (len(item_id) <= 4 and item.get("maps", {}).get(ARAM_MAP, False)
                and item["gold"]["purchasable"])

    def aram_items(self):
        return {iid: it for iid, it in self.items.items() if self.is_standard(iid)}

    def champion(self, name):
        cid = self._champ_index.get(normalize(name))
        if cid is None:
            raise ValueError(f"챔피언을 찾을 수 없습니다: {name}")
        return self.champions[cid]

    def champion_texts(self, champion_id):
        """패시브와 스킬 4개의 설명 (챔피언별 상세 파일, 캐시)."""
        path = DATA_DIR / "ddragon" / self.version / self.lang / "champion" / f"{champion_id}.json"
        url = f"{BASE_URL}/cdn/{self.version}/data/{self.lang}/champion/{champion_id}.json"
        detail = _cached_json(path, url)["data"][champion_id]
        return [detail["passive"]["description"]] + [
            s["description"] + " " + s.get("tooltip", "") for s in detail["spells"]]

    def item_id(self, name_or_id):
        text = str(name_or_id).strip()
        if text in self.items:
            return text
        iid = self._item_index.get(normalize(text))
        if iid is None:
            raise ValueError(f"아이템을 찾을 수 없습니다: {text}")
        return iid


def load(version=None, lang="ko_KR", refresh=False):
    version = version or latest_version()
    base = DATA_DIR / "ddragon" / version / lang
    items = _cached_json(base / "item.json",
                         f"{BASE_URL}/cdn/{version}/data/{lang}/item.json", refresh)["data"]
    champions = _cached_json(base / "champion.json",
                             f"{BASE_URL}/cdn/{version}/data/{lang}/champion.json", refresh)["data"]
    return GameData(version, items, champions, lang)
