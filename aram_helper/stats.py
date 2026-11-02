"""아이템 설명에서 스탯을 읽고 챔피언의 최종 스탯을 계산한다."""

import re
from functools import lru_cache

# 아이템 설명 <stats> 블록의 한국어 스탯 이름 -> 내부 키
STAT_NAMES = {
    "체력": "hp", "공격력": "ad", "주문력": "ap", "스킬 가속": "ah",
    "방어력": "armor", "마법 저항력": "mr", "공격 속도": "as",
    "치명타 확률": "crit", "치명타 피해량": "crit_dmg", "마나": "mana",
    "이동 속도": "ms", "물리 관통력": "lethality", "방어구 관통력": "apen_pct",
    "마법 관통력": "mpen", "생명력 흡수": "lifesteal", "모든 피해 흡혈": "omnivamp",
    "체력 회복 및 보호막": "heal_shield", "강인함": "tenacity",
}
# 퍼센트 값이 붙으면 다른 키로 저장하는 스탯
PERCENT_VARIANT = {"mpen": "mpen_pct", "ms": "ms_pct"}

OFFENSE_AD = ("ad", "as", "crit", "lethality", "apen_pct")
OFFENSE_AP = ("ap", "mpen", "mpen_pct")
DEFENSE = ("hp", "armor", "mr")

# 스탯 1단위의 골드 가치를 계산할 기본 아이템 (스탯 하나만 올려주는 아이템)
BASIC_ITEMS = {"ad": "1036", "ap": "1052", "hp": "1028", "armor": "1029", "mr": "1033",
               "as": "1042", "crit": "1018", "mana": "1027", "ms": "1001"}
# 기본 아이템이 없는 스탯: (아이템, 함께 붙은 스탯)으로 역산
DERIVED_ITEMS = {"ah": ("3067", "hp"), "lethality": ("3134", "ad")}


def strip_tags(text):
    return re.sub(r"<[^>]+>", " ", text)


@lru_cache(maxsize=None)
def _parse_stats(description):
    stats = {}
    block = re.search(r"<stats>(.*?)</stats>", description, re.S)
    if block:
        for line in re.split(r"<br\s*/?>", block.group(1)):
            m = re.match(r"\s*([^<]+?)\s*<[^>]+>\s*([\d.]+)(%?)\s*<", line)
            if not m or m.group(1) not in STAT_NAMES:
                continue
            key = STAT_NAMES[m.group(1)]
            value = float(m.group(2))
            if m.group(3):
                key = PERCENT_VARIANT.get(key, key)
                value /= 100
            stats[key] = stats.get(key, 0.0) + value
    amp = re.search(r"총\s*<scaleAP>주문력이\s*(\d+)%", description)
    if amp:
        stats["ap_mult"] = int(amp.group(1)) / 100
    return stats


def item_stats(item):
    return dict(_parse_stats(item["description"]))


# 고유 효과를 같은 가치의 스탯으로 바꾼 근사값 (아이템 ID -> 추가 스탯).
# 스탯 블록에 없는 기본 공격 추가 피해, 스킬 강화, 보호막 등을 대략 반영한다.
UNIQUE_EFFECTS = {
    "3153": {"ad": 25},                  # 몰락한 왕의 검: 현재 체력 비례 적중 피해
    "3115": {"ap": 35},                  # 내셔의 이빨: 적중 시 마법 피해
    "3091": {"ad": 20},                  # 마법사의 최후: 적중 시 마법 피해
    "3124": {"as": 0.2},                 # 구인수의 격노검: 적중 효과 강화
    "6672": {"ad": 25},                  # 크라켄 학살자: 세 번째 공격 추가 피해
    "3078": {"ad": 30},                  # 삼위일체: 주문검
    "3508": {"ad": 20},                  # 정수 약탈자: 주문검
    "3100": {"ap": 40},                  # 리치베인: 주문검
    "4633": {"ap": 25, "omnivamp": 0.05},  # 균열 생성기: 전투 중 피해 증폭
    "6653": {"ap": 30},                  # 리안드리의 고통: 최대 체력 비례 화상
    "3074": {"ad": 20},                  # 굶주린 히드라: 광역 피해
    "3748": {"ad": 25},                  # 거대한 히드라: 체력 비례 광역 피해
    "3071": {"apen_pct": 0.15},          # 칠흑의 양날 도끼: 방어력 감소 중첩
    "3036": {"apen_pct": 0.05},          # 도미닉 경의 인사: 체력 높은 대상 추가 피해
    "6676": {"ad": 15},                  # 징수의 총: 처형
    "3053": {"hp": 350},                 # 스테락의 도전: 보호막
    "3156": {"mr": 25},                  # 맬모셔스의 아귀: 마법 보호막
    "3102": {"mr": 15},                  # 밴시의 장막: 주문 보호막
    "3157": {"hp": 200},                 # 존야의 모래시계: 경직
    "3083": {"hp": 250},                 # 워모그의 갑옷: 체력 회복
}


def model_stats(item_id, item):
    """계산 모델에 쓰는 스탯: 스탯 블록 + 고유 효과 근사값."""
    st = item_stats(item)
    for k, v in UNIQUE_EFFECTS.get(str(item_id), {}).items():
        st[k] = st.get(k, 0.0) + v
    return st


def is_inventory_item(item):
    """6칸을 차지하는 아이템인지 (소모품·장신구·포로 간식 제외)."""
    tags = item.get("tags", [])
    return item["gold"]["total"] > 0 and "Consumable" not in tags and "Trinket" not in tags


def item_flags(item):
    """상황 판단에 쓰는 아이템 특징."""
    text = strip_tags(item["description"])
    stats = item_stats(item)
    offensive = any(stats.get(k) for k in OFFENSE_AD + OFFENSE_AP)
    flags = set()
    if re.search(r"\d+%의\s*상처", text) or "고통스러운 상처" in text:
        flags.add("anti_heal")
    if "<shield>" in item["description"]:
        flags.add("shield")
    if re.search(r"보호막[^.]{0,30}(감소|파괴)", text):
        flags.add("shield_break")
    if offensive and re.search(r"(최대 체력의|현재 체력의|추가 체력에 비례)", text):
        flags.add("max_hp_dmg")
    if "치명타로 받는 피해" in text:
        flags.add("anti_crit")
    if re.search(r"아군[^.]{0,40}(보호막|회복|공격 속도|이동 속도|주문력)", text):
        flags.add("ally_buff")
    return flags


def gold_values(gd):
    """스탯 1단위당 골드 가치. 기본 아이템 가격에서 계산한다."""
    values = {}
    for key, iid in BASIC_ITEMS.items():
        item = gd.items.get(iid)
        stat = item_stats(item).get(key) if item else None
        if stat:
            values[key] = item["gold"]["total"] / stat
    for key, (iid, other) in DERIVED_ITEMS.items():
        item = gd.items.get(iid)
        if not item or other not in values:
            continue
        st = item_stats(item)
        if st.get(key):
            values[key] = (item["gold"]["total"] - st.get(other, 0) * values[other]) / st[key]
    return values


def stat_gold(stats, values, keys=None):
    return sum(values.get(k, 0) * v for k, v in stats.items() if keys is None or k in keys)


def _growth(level):
    # 롤의 레벨당 성장 공식
    return (level - 1) * (0.7025 + 0.0175 * (level - 1))


def compute_stats(gd, champion, level, item_ids, unique=True):
    """챔피언 기본 스탯 + 아이템 스탯 합계. unique=True면 고유 효과 근사값을 더한다."""
    base = champion["stats"]
    g = _growth(level)
    total = {}
    for iid in item_ids:
        st = model_stats(iid, gd.items[iid]) if unique else item_stats(gd.items[iid])
        for k, v in st.items():
            total[k] = total.get(k, 0.0) + v

    def item(k):
        return total.get(k, 0.0)

    return {
        "hp": base["hp"] + base["hpperlevel"] * g + item("hp"),
        "armor": base["armor"] + base["armorperlevel"] * g + item("armor"),
        "mr": base["spellblock"] + base["spellblockperlevel"] * g + item("mr"),
        "ad": base["attackdamage"] + base["attackdamageperlevel"] * g + item("ad"),
        "bonus_ad": item("ad"),
        "ap": item("ap") * (1 + item("ap_mult")),
        "as": base["attackspeed"] * (1 + base["attackspeedperlevel"] / 100 * g + item("as")),
        "crit": min(1.0, item("crit")),
        "crit_dmg": 0.75 + item("crit_dmg"),
        "ah": item("ah"),
        "lethality": item("lethality"),
        "apen_pct": item("apen_pct"),
        "mpen": item("mpen"),
        "mpen_pct": item("mpen_pct"),
        "lifesteal": item("lifesteal"),
        "omnivamp": item("omnivamp"),
        "heal_shield": item("heal_shield"),
    }


# Live Client Data API의 championStats 키 -> 내부 키
LIVE_STAT_KEYS = {"maxHealth": "hp", "armor": "armor", "magicResist": "mr",
                  "attackDamage": "ad", "abilityPower": "ap", "attackSpeed": "as",
                  "critChance": "crit", "abilityHaste": "ah"}


class Unit:
    """한 명의 플레이어: 챔피언, 레벨, 보유 아이템."""

    def __init__(self, gd, champion, level, item_ids, live_stats=None, name=None, augment_names=()):
        self.gd = gd
        self.champion = champion
        self.level = level
        self.item_ids = list(item_ids)
        self.name = name or champion["name"]
        self.augment_names = list(augment_names)
        # 증강 카탈로그로 해석한 결과 ({name, desc, effects, ...})
        self.augments = []
        # 게임 중 기록: 킬/데스/어시스트, 나를 처치한 횟수, 소환사 주문 ("ignite" 등)
        self.kills = self.deaths = self.assists = 0
        self.killed_me = 0
        self.spells = []
        # 게임 중 실제 스탯(증강 효과 포함)과 계산값의 차이를 보정값으로 저장.
        # 고유 효과 근사값은 실제 스탯에 없으므로 빼고 비교한다.
        self._offset = {}
        if live_stats:
            calc = compute_stats(gd, champion, level, self.item_ids, unique=False)
            for live_key, key in LIVE_STAT_KEYS.items():
                if live_key in live_stats:
                    self._offset[key] = live_stats[live_key] - calc[key]

    def stats(self, extra_ids=(), without=()):
        ids = list(self.item_ids)
        for iid in without:
            ids.remove(iid)
        st = compute_stats(self.gd, self.champion, self.level, ids + list(extra_ids))
        for k, v in self._offset.items():
            st[k] += v
        st["crit"] = min(1.0, max(0.0, st["crit"]))
        return st

    def items(self):
        return [self.gd.items[i] for i in self.item_ids]

    def inventory_ids(self):
        return [i for i in self.item_ids if is_inventory_item(self.gd.items[i])]

    def item_total(self):
        total = {}
        for it in self.items():
            for k, v in item_stats(it).items():
                total[k] = total.get(k, 0.0) + v
        return total

    def flags(self):
        out = set()
        for it in self.items():
            out |= item_flags(it)
        return out
