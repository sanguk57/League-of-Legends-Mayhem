"""증강 이름·설명 데이터와 효과 분류.

- 이름과 ID: CommunityDragon cherry-augments.json (아수라장 증강 포함, OP.GG와 같은 ID)
- 설명: OP.GG 챔피언별 증강 목록, 없으면 CommunityDragon 아레나 데이터
"""

import json
import re
import time
from collections import Counter

from .ddragon import DATA_DIR, http_json, normalize
from .stats import strip_tags

CDRAGON = "https://raw.communitydragon.org/latest"
NAMES_URL = f"{CDRAGON}/plugins/rcp-be-lol-game-data/global/ko_kr/v1/cherry-augments.json"
ARENA_URL = f"{CDRAGON}/cdragon/arena/ko_kr.json"
CACHE_DIR = DATA_DIR / "cdragon"
CACHE_TTL = 3 * 24 * 3600

# 효과 이름 -> (설명에서 찾을 패턴, 표시 이름)
EFFECTS = {
    "anti_heal": (r"상처|치유 감소|회복량[^.]{0,10}감소", "치유 감소"),
    "shield_break": (r"보호막[^.]{0,20}(파괴|감소)", "보호막 파괴"),
    "heal": (r"(?<!치유 )(회복|흡혈|생명력 흡수)", "회복"),
    "shield": (r"보호막 ?(을|이)? ?(얻|부여|획득|씌|충전|발동|생|재생성)|피해를 흡수", "보호막"),
    "tank": (r"방어력|마법 저항력|최대 체력이 \d|받는 피해[^.]{0,15}감소|강인함", "탱커"),
    "crit": (r"치명타", "치명타"),
    "true_dmg": (r"고정 피해", "고정 피해"),
    # "최대 체력 10%만큼의 피해를 흡수"(보호막)는 제외
    "max_hp_dmg": (r"최대 체력(의|에)?[^.]{0,20}(비례|%)[^.]{0,15}피해(?!를 흡수)", "체력 비례 피해"),
    "ad": (r"공격력|공격 속도|물리 관통력|방어구 관통력", "물리 딜"),
    "ap": (r"주문력|마법 관통력|스킬 가속", "마법 딜"),
}
EFFECT_LABEL = {k: label for k, (_, label) in EFFECTS.items()}


def classify(text):
    text = re.sub(r"\s+", " ", strip_tags(text or ""))
    found = set()
    for key, (pattern, _) in EFFECTS.items():
        if re.search(pattern, text):
            found.add(key)
    # "최대 체력이 30% 감소"처럼 체력을 깎는 증강은 탱커로 보지 않는다
    if "tank" in found and re.search(r"체력이 ?\d+% ?감소", text) and not re.search(r"방어력|마법 저항력", text):
        found.discard("tank")
    return found


def _cached(name, url, refresh=False):
    path = CACHE_DIR / name
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < CACHE_TTL:
        return json.loads(path.read_text("utf-8"))
    try:
        data = http_json(url, timeout=60)
    except OSError:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


class AugmentCatalog:
    def __init__(self, entries, arena_descs):
        self.by_id = {}
        self._by_name = {}
        for e in entries:
            aug = {"id": e["id"], "name": e["nameTRA"], "api": e.get("augmentNameId", ""),
                   "rarity": e.get("rarity", ""), "desc": arena_descs.get(e.get("augmentNameId"), "")}
            self.by_id[e["id"]] = aug
            self._by_name.setdefault(normalize(aug["name"]), []).append(aug)

    def add_descriptions(self, augments):
        """OP.GG 증강 목록의 설명을 덮어쓴다 (아수라장 기준 설명이라 더 정확하다)."""
        for a in augments:
            aug = self.by_id.get(a.get("id"))
            if aug is None and a.get("name"):
                aug = {"id": a["id"], "name": a["name"], "api": "", "rarity": "", "desc": ""}
                self.by_id[a["id"]] = aug
                self._by_name.setdefault(normalize(a["name"]), []).append(aug)
            if aug is not None and a.get("desc"):
                aug["desc"] = a["desc"]

    def find(self, name_or_id):
        text = str(name_or_id).strip()
        if text.isdigit() and int(text) in self.by_id:
            aug = self.by_id[int(text)]
        else:
            matches = self._by_name.get(normalize(text))
            if not matches:
                raise ValueError(f"증강을 찾을 수 없습니다: {text}")
            # 같은 이름이 여러 개면 설명이 있는 쪽을 쓴다
            aug = next((m for m in matches if m["desc"]), matches[0])
        return {**aug, "effects": classify(aug["name"] + " " + aug["desc"])}


def load_catalog(refresh=False):
    entries = _cached("cherry-augments.json", NAMES_URL, refresh)
    try:
        arena = _cached("arena.json", ARENA_URL, refresh).get("augments", [])
    except OSError:
        arena = []
    arena_descs = {a.get("apiName"): a.get("desc", "") for a in arena}
    return AugmentCatalog(entries, arena_descs)


def effect_counts(augments):
    counts = Counter()
    for a in augments:
        counts.update(a.get("effects", ()))
    return counts


def describe(aug):
    effects = ", ".join(EFFECT_LABEL[e] for e in sorted(aug.get("effects", ())))
    return f"{aug['name']}({effects})" if effects else aug["name"]
