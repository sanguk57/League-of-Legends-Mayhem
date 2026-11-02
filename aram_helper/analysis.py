"""팀 분석, 아이템 추천, 증강 추천.

피해 공식과 스탯 중심의 단순화한 모델이다. 챔피언 스킬 계수와 아이템 고유 효과는
대부분 반영되지 않으므로 상황 규칙과 OP.GG 통계로 보정한다.
"""

from dataclasses import dataclass, field

from .augments import EFFECT_LABEL as EFFECT_NAMES
from .augments import classify, effect_counts
from .augments import describe as describe_augment
from .stats import (DEFENSE, OFFENSE_AD, OFFENSE_AP, compute_stats, gold_values, item_flags,
                    item_stats, stat_gold, strip_tags)

_traits_cache = {}


def champion_traits(gd, champion):
    """챔피언 스킬 설명(Data Dragon)에서 회복·보호막·고정 피해 성향을 읽는다.
    효과마다 해당하는 스킬 수를 센다 (패시브 포함 최대 5)."""
    cid = champion["id"]
    if cid not in _traits_cache:
        try:
            texts = gd.champion_texts(cid)
        except (OSError, KeyError):
            texts = []
        counts = {}
        for text in texts:
            for effect in classify(text):
                counts[effect] = counts.get(effect, 0) + 1
        _traits_cache[cid] = counts
    return _traits_cache[cid]


@dataclass
class TeamProfile:
    mix: tuple = (0.5, 0.5, 0.0)  # (물리, 마법, 고정) 피해 비율
    lethality: float = 0.0
    apen_pct: float = 0.0
    mpen: float = 0.0
    mpen_pct: float = 0.0
    heal: float = 0.0
    shield: float = 0.0
    crit: float = 0.0
    marksmen: int = 0
    frontline: int = 0
    anti_heal: bool = False
    shield_break: bool = False
    augments: list = field(default_factory=list)
    spells: dict = field(default_factory=dict)   # 소환사 주문 개수 ("ignite": 2 등)
    killers: list = field(default_factory=list)  # 나를 처치한 (챔피언 이름, 횟수), 많은 순


@dataclass
class ItemPick:
    item_id: str
    name: str
    gold: int
    score: float
    model: float
    bonus: float = 0.0
    reasons: list = field(default_factory=list)
    opgg: dict = None


def damage_mix(unit, values):
    """챔피언 성향(info)과 아이템으로 피해 구성을 추정한다."""
    info = unit.champion["info"]
    attack, magic = max(info["attack"], 1), max(info["magic"], 1)
    phys = attack / (attack + magic)
    total = unit.item_total()
    ad_gold = stat_gold(total, values, OFFENSE_AD)
    ap_gold = stat_gold(total, values, OFFENSE_AP)
    if ad_gold + ap_gold > 0:
        phys = 0.4 * phys + 0.6 * ad_gold / (ad_gold + ap_gold)
    # 고정 피해 스킬 1개당 7.5%, 최대 20%
    true = min(0.2, 0.075 * champion_traits(unit.gd, unit.champion).get("true_dmg", 0))
    true = min(0.5, true + 0.1 * effect_counts(unit.augments)["true_dmg"])
    return (phys * (1 - true), (1 - phys) * (1 - true), true), ad_gold + ap_gold


def is_frontline(unit, values):
    tags = unit.champion["tags"]
    defense_gold = stat_gold(unit.item_total(), values, DEFENSE)
    return "Tank" in tags or defense_gold >= 2500 or effect_counts(unit.augments)["tank"] >= 2 or (
        "Fighter" in tags and unit.champion["info"]["defense"] >= 6)


def team_profile(units, values):
    prof = TeamProfile()
    if not units:
        return prof
    weights, mixes = [], []
    for u in units:
        mix, offense_gold = damage_mix(u, values)
        threat = 1 + offense_gold / 2500 + u.level / 18
        if "Tank" in u.champion["tags"] and offense_gold < 1500:
            threat *= 0.6
        # 이번 게임에서 킬이 많거나 나를 자주 처치한 상대의 피해 구성을 더 무겁게 본다
        threat *= 1 + 0.08 * min(u.kills, 10) + 0.25 * min(u.killed_me, 4)
        weights.append(threat)
        mixes.append(mix)
    total_w = sum(weights)
    prof.mix = tuple(sum(w * m[i] for w, m in zip(weights, mixes)) / total_w for i in range(3))

    phys_w = [w * m[0] for w, m in zip(weights, mixes)]
    mag_w = [w * m[1] for w, m in zip(weights, mixes)]
    for u, pw, mw in zip(units, phys_w, mag_w):
        st = u.stats()
        prof.lethality += st["lethality"] * pw / (sum(phys_w) or 1)
        prof.apen_pct += st["apen_pct"] * pw / (sum(phys_w) or 1)
        prof.mpen += st["mpen"] * mw / (sum(mag_w) or 1)
        prof.mpen_pct += st["mpen_pct"] * mw / (sum(mag_w) or 1)

        flags = u.flags()
        aug = effect_counts(u.augments)
        # 회복·보호막 스킬이 2개 이상이면 1점, 1개면 0.5점
        traits = champion_traits(u.gd, u.champion)
        prof.heal += (min(1.0, traits.get("heal", 0) / 2) + (st["lifesteal"] + st["omnivamp"] >= 0.15)
                      + (st["heal_shield"] >= 0.1) + aug["heal"] + 0.5 * ("heal" in u.spells))
        prof.shield += (min(1.0, traits.get("shield", 0) / 2) + 0.5 * ("shield" in flags)
                        + 0.5 * (st["heal_shield"] >= 0.1) + aug["shield"] + 0.5 * ("barrier" in u.spells))
        for spell in u.spells:
            prof.spells[spell] = prof.spells.get(spell, 0) + 1
        if u.killed_me:
            prof.killers.append((u.champion["name"], u.killed_me))
        prof.crit += st["crit"] + 0.2 * aug["crit"]
        prof.marksmen += "Marksman" in u.champion["tags"]
        prof.frontline += is_frontline(u, values)
        prof.anti_heal |= "anti_heal" in flags or aug["anti_heal"] > 0
        prof.shield_break |= "shield_break" in flags or aug["shield_break"] > 0
        prof.augments += u.augments
    prof.killers.sort(key=lambda k: -k[1])
    return prof


SPELL_LABEL = {"ignite": "점화", "exhaust": "탈진", "heal": "회복", "barrier": "방어막", "cleanse": "정화"}


def describe_team(prof, label):
    p, m, t = prof.mix
    lines = [f"{label} 피해 구성: 물리 {p:.0%} / 마법 {m:.0%} / 고정 {t:.0%}",
             f"{label} 탱커·전열: {prof.frontline}명, 원거리 딜러: {prof.marksmen}명"]
    if prof.lethality or prof.apen_pct or prof.mpen or prof.mpen_pct:
        lines.append(f"{label} 관통력(평균): 물리 관통 {prof.lethality:.0f}, 방어구 관통 {prof.apen_pct:.0%}, "
                     f"마법 관통 {prof.mpen:.0f} + {prof.mpen_pct:.0%}")
    extras = []
    if prof.heal >= 2:
        extras.append(f"회복 강함({prof.heal:.0f})")
    if prof.shield >= 2:
        extras.append(f"보호막 많음({prof.shield:.1f})")
    if prof.crit >= 0.6:
        extras.append(f"치명타 확률 합계 {prof.crit:.0%}")
    if prof.anti_heal:
        extras.append("치유 감소 보유")
    if extras:
        lines.append(f"{label} 특징: " + ", ".join(extras))
    spells = [f"{name} {prof.spells[key]}" for key, name in SPELL_LABEL.items() if prof.spells.get(key)]
    if spells:
        lines.append(f"{label} 소환사 주문: " + ", ".join(spells))
    if prof.killers:
        lines.append("나를 처치한 상대: " + ", ".join(f"{name} {n}회" for name, n in prof.killers)
                     + " (피해 구성에 더 크게 반영)")
    if prof.augments:
        lines.append(f"{label} 증강: " + ", ".join(describe_augment(a) for a in prof.augments))
    return lines


# ---------------------------------------------------------------- 피해·생존 모델

def effective_resist(resist, pct_pen, flat_pen):
    return max(0.0, resist * (1 - pct_pen) - flat_pen)


def effective_hp(st, prof):
    """상대 팀 피해 구성과 관통력을 반영한 유효 체력."""
    p, m, t = prof.mix
    hp = st["hp"]
    armor = effective_resist(st["armor"], prof.apen_pct, prof.lethality)
    mr = effective_resist(st["mr"], prof.mpen_pct, prof.mpen)
    return 1 / (p / (hp * (1 + armor / 100)) + m / (hp * (1 + mr / 100)) + t / hp)


def attack_weight(champion, dmg_type):
    """피해 중 기본 공격 비중 (나머지는 스킬)."""
    tags = champion["tags"]
    if "Marksman" in tags:
        return 0.8 if dmg_type == "ad" else 0.5
    if dmg_type == "ad" and ("Fighter" in tags or "Assassin" in tags):
        return 0.4
    return 0.1


def _raw_damage(st, level, dmg_type):
    attack = st["ad"] * st["as"] * (1 + st["crit"] * st["crit_dmg"])
    if dmg_type == "ad":
        spell = 20 * level + 1.0 * st["bonus_ad"]
    else:
        spell = 25 * level + 0.8 * st["ap"]
    spell *= 1 + 0.6 * st["ah"] / 100  # 스킬 가속: 스킬을 더 자주 쓴다
    return attack, spell


def damage_score(st, base_st, level, dmg_type, aa_weight, targets):
    """대상들의 체력 대비 실제로 들어가는 피해 (기본 스탯 대비 상대값)."""
    attack0, spell0 = _raw_damage(base_st, level, dmg_type)
    attack, spell = _raw_damage(st, level, dmg_type)
    score = 0.0
    for t in targets:
        armor = effective_resist(t["armor"], st["apen_pct"], st["lethality"])
        if dmg_type == "ad":
            resist = armor
        else:
            resist = effective_resist(t["mr"], st["mpen_pct"], st["mpen"])
        attack_part = attack / attack0 * 100 / (100 + armor)
        spell_part = spell / spell0 * 100 / (100 + resist)
        score += (aa_weight * attack_part + (1 - aa_weight) * spell_part) / t["hp"]
    return score / len(targets)


def support_power(st):
    """인챈터가 아군에게 주는 회복·보호막의 크기 (상대값)."""
    return (1 + st["heal_shield"]) * (1 + 0.6 * st["ah"] / 100) * (1 + st["ap"] / 300)


# ---------------------------------------------------------------- 역할 판단

def auto_role(champion):
    tags = champion["tags"]
    if tags[0] == "Tank" or (tags[0] == "Support" and "Tank" in tags):
        return "tank"
    if tags[0] == "Support":
        return "enchanter"
    if tags[0] == "Fighter":
        return "bruiser"
    return "dealer"


def auto_damage_type(unit, values):
    total = unit.item_total()
    ad_gold = stat_gold(total, values, OFFENSE_AD)
    ap_gold = stat_gold(total, values, OFFENSE_AP)
    if ad_gold or ap_gold:
        return "ad" if ad_gold >= ap_gold else "ap"
    info = unit.champion["info"]
    if "Marksman" in unit.champion["tags"]:
        return "ad"
    return "ad" if info["attack"] >= info["magic"] else "ap"


ROLE_LABEL = {"tank": "탱커", "dealer": "딜러", "bruiser": "브루저(탱커+딜러)", "enchanter": "인챈터(서포터)"}


# ---------------------------------------------------------------- 아이템 추천

def candidate_items(gd, role, dmg_type, owned):
    aram = gd.aram_items()
    offense = OFFENSE_AD if dmg_type == "ad" else OFFENSE_AP
    result = {}
    for iid, item in aram.items():
        upgradable = any(i in aram for i in item.get("into", []))
        if (iid in owned or upgradable or item["gold"]["total"] < 2000
                or "Boots" in item.get("tags", []) or item.get("requiredChampion")):
            continue
        st = item_stats(item)
        has_offense = any(st.get(k) for k in offense)
        has_defense = any(st.get(k) for k in DEFENSE)
        wrong_offense = any(st.get(k) for k in (OFFENSE_AP if dmg_type == "ad" else OFFENSE_AD))
        if role == "tank":
            keep = has_defense and not (wrong_offense and not has_offense)
        elif role == "enchanter":
            keep = (bool(st.get("heal_shield") or "ally_buff" in item_flags(item)
                         or (st.get("ap") and st.get("ah")))
                    and not any(st.get(k) for k in OFFENSE_AD))
        elif role == "dealer":
            keep = has_offense
        else:
            keep = has_offense or (has_defense and not wrong_offense)
        if keep:
            result[iid] = item
    return result


def _situational(item, flags, st, role, dmg_type, enemy, ally):
    """상황에 따른 가산점과 이유."""
    bonus, reasons = 0.0, []
    if "anti_heal" in flags and enemy.heal >= 2:
        if ally.anti_heal:
            bonus += 3
            reasons.append("상대 회복 강함 (아군에 치유 감소 있음)")
        elif ally.spells.get("ignite"):
            bonus += 6
            reasons.append("상대 회복 강함 (아군 치유 감소는 점화뿐)")
        else:
            bonus += 12
            reasons.append("상대 회복 강함, 아군에 치유 감소 없음")
    if "shield_break" in flags and enemy.shield >= 2:
        bonus += 10 if not ally.shield_break else 3
        reasons.append("상대 보호막 많음")
    if "anti_crit" in flags and (enemy.crit >= 0.6 or enemy.marksmen >= 2):
        bonus += 8
        reasons.append("상대 치명타·원거리 딜러 많음")
    if "max_hp_dmg" in flags and enemy.frontline >= 2:
        bonus += 4
        reasons.append(f"상대 전열 {enemy.frontline}명 (체력 비례 피해)")
    if role in ("tank", "bruiser") and ally.frontline == 0 and any(st.get(k) for k in DEFENSE):
        bonus += 3
        reasons.append("아군에 탱커가 없음")
    ignites = enemy.spells.get("ignite", 0)
    if ignites >= 2 and (st.get("lifesteal") or st.get("omnivamp") or st.get("heal_shield")):
        bonus -= 2
        reasons.append(f"상대 점화 {ignites}개 (회복 효율 감소)")
    if role == "enchanter":
        if "ally_buff" in flags:
            bonus += 3
            reasons.append("아군 지원 효과")
        if "ManaRegen" in item.get("tags", []):
            bonus += 1
            reasons.append("마나 회복")
    return bonus, reasons


# 증강 효과 -> 시너지가 나는 아이템 스탯
AUGMENT_ITEM_SYNERGY = {
    "crit": ("crit",), "ad": ("ad", "as", "lethality", "apen_pct"), "ap": ("ap", "mpen", "mpen_pct"),
    "tank": ("hp", "armor", "mr"), "heal": ("lifesteal", "omnivamp", "heal_shield"),
}


def _augment_synergy(st, augments):
    """내 증강과 같은 방향의 스탯을 주는 아이템에 가산점."""
    names = []
    for effect, keys in AUGMENT_ITEM_SYNERGY.items():
        if any(st.get(k) for k in keys):
            names += [a["name"] for a in augments if effect in a["effects"] and a["name"] not in names]
    if not names:
        return 0.0, []
    return min(4.0, 2.0 * len(names)), [f"내 증강({', '.join(names)})과 시너지"]


def _opgg_bonus(info, champ_win_rate):
    if not info:
        return 0.0
    bonus = 4 * min(1.0, info["pick_rate"] / 0.3)
    if champ_win_rate:
        bonus += max(-3.0, min(3.0, (info["win_rate"] - champ_win_rate) * 100))
    if info["source"] == "코어":
        bonus += 2
    return bonus


class Model:
    """내 스탯 변화가 얼마나 이득인지 계산한다 (역할에 따라 생존·피해·지원을 섞음)."""

    def __init__(self, gd, me, allies, enemies, role, dmg_type):
        values = gold_values(gd)
        self.gd, self.me, self.role, self.dmg_type = gd, me, role, dmg_type
        self.enemy = team_profile(enemies, values)
        self.ally = team_profile(allies + [me], values)
        # 기본 공격과 스킬 피해를 같은 기준으로 비교하려고 아이템 없는 스탯으로 정규화한다
        self.reference = compute_stats(gd, me.champion, me.level, [])
        self.targets = [e.stats() for e in enemies] or [me.stats()]
        self.aa_weight = attack_weight(me.champion, dmg_type)
        self.tank_weight = {"tank": 1.0, "dealer": 0.0, "bruiser": 0.5, "enchanter": 0.3}[role]
        if role == "bruiser" and self.ally.frontline == 0:
            self.tank_weight = 0.7

    def _offense(self, st):
        if self.role == "enchanter":
            return support_power(st)
        return damage_score(st, self.reference, self.me.level, self.dmg_type, self.aa_weight, self.targets)

    def gain(self, st, base):
        """base 스탯에서 st 스탯으로 바뀔 때의 이득 (%)."""
        tank = (effective_hp(st, self.enemy) / effective_hp(base, self.enemy) - 1) * 100
        offense = (self._offense(st) / self._offense(base) - 1) * 100
        return self.tank_weight * tank + (1 - self.tank_weight) * offense

    def bonus(self, item):
        """상황·증강 보정 (OP.GG 제외)."""
        st = item_stats(item)
        bonus, reasons = _situational(item, item_flags(item), st, self.role, self.dmg_type,
                                      self.enemy, self.ally)
        syn_bonus, syn_reasons = _augment_synergy(st, self.me.augments)
        return bonus + syn_bonus, reasons + syn_reasons


def recommend_items(gd, me, allies, enemies, role, dmg_type, popularity=None,
                    champ_win_rate=None, top=6, model=None):
    m = model or Model(gd, me, allies, enemies, role, dmg_type)
    base_st = me.stats()
    picks = []
    for iid, item in candidate_items(gd, role, dmg_type, set(me.item_ids)).items():
        gold_k = item["gold"]["total"] / 1000
        score = m.gain(me.stats([iid]), base_st) / gold_k
        bonus, reasons = m.bonus(item)
        info = (popularity or {}).get(iid)
        op = _opgg_bonus(info, champ_win_rate)
        picks.append(ItemPick(iid, item["name"], item["gold"]["total"], score + bonus + op,
                              score, bonus + op, reasons, info))
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:top], m.enemy, m.ally


INVENTORY_SLOTS = 6


def buy_plan(gd, target_id, inventory, gold):
    """보유 골드로 target을 향해 지금 살 것.
    보유한 하위 아이템은 완성 비용에서 빼고, 골드가 모자라면 비싼 재료부터 산다."""
    pool = list(inventory)

    def node(iid):
        if iid in pool:
            pool.remove(iid)
            return {"id": iid, "cost": 0, "children": []}
        children = [node(c) for c in gd.items[iid].get("from", []) if c in gd.items]
        cost = gd.items[iid]["gold"]["base"] + sum(c["cost"] for c in children)
        return {"id": iid, "cost": cost, "children": children}

    root = node(target_id)
    used = len(inventory) - len(pool)
    free = INVENTORY_SLOTS - len(inventory)
    if root["cost"] <= gold:
        # 완성하면 재료 칸이 비므로 필요한 칸은 1 - (사용한 재료 수)
        ok = 1 - used <= free
        return {"target": target_id, "cost": root["cost"], "buy": [(target_id, root["cost"])] if ok else [],
                "complete": ok, "left": gold - root["cost"] if ok else gold, "full": not ok}
    buys = []

    def take(n):
        # 살 수 있는 큰 재료를 먼저 사고, 남은 골드로 못 산 재료의 하위 재료를 산다
        nonlocal gold
        rest = []
        for c in sorted(n["children"], key=lambda c: -c["cost"]):
            if c["cost"] == 0:
                continue
            if c["cost"] <= gold and len(buys) < free:
                buys.append((c["id"], c["cost"]))
                gold -= c["cost"]
            else:
                rest.append(c)
        for c in rest:
            take(c)

    take(root)
    return {"target": target_id, "cost": root["cost"], "buy": buys, "complete": False, "left": gold,
            "full": free <= 0}


def recommend_swap(gd, me, model, picks, min_gain=3.0, gold=None):
    """칸이 가득 찼을 때: 보유 완성 아이템 하나를 팔고 추천 아이템을 넣는 가장 좋은 조합.
    하위 아이템은 다른 아이템의 재료로 쓸 수 있으므로 판매 후보에서 뺀다.
    gold를 주면 판매 금액을 더해 바로 완성할 수 있는 교체만 추천한다."""
    inventory = me.inventory_ids()
    if len(inventory) < INVENTORY_SLOTS:
        return None
    # 보유 재료를 합쳐 팔지 않고도 추천 아이템을 완성할 수 있으면 교체할 필요가 없다
    if gold is not None and any(buy_plan(gd, p.item_id, inventory, gold)["complete"] for p in picks[:3]):
        return None
    aram = gd.aram_items()
    base = me.stats()
    best = None
    for old in dict.fromkeys(inventory):
        old_item = gd.items[old]
        if "Boots" in old_item.get("tags", []) or any(i in aram for i in old_item.get("into", [])):
            continue
        old_bonus, _ = model.bonus(old_item)
        rest = list(inventory)
        rest.remove(old)
        for p in picks[:3]:
            if gold is not None and not buy_plan(gd, p.item_id, rest, gold + old_item["gold"]["sell"])["complete"]:
                continue
            gain = model.gain(me.stats([p.item_id], without=[old]), base) + p.bonus - old_bonus
            if best is None or gain > best["gain"]:
                best = {"sell": old_item["name"], "sell_id": old, "sell_gold": old_item["gold"]["sell"],
                        "buy": p.name, "buy_id": p.item_id, "gain": gain}
    return best if best and best["gain"] >= min_gain else None


def recommend_boots(gd, me, role, dmg_type, enemy, popularity=None):
    """신발이 없을 때만 추천한다."""
    aram = gd.aram_items()
    if any("Boots" in gd.items[i].get("tags", []) and gd.items[i]["gold"]["total"] > 300
           for i in me.item_ids):
        return None
    names = {it["name"]: iid for iid, it in aram.items() if "Boots" in it.get("tags", [])}

    if enemy.mix[1] >= 0.6:
        choice, reason = "헤르메스의 발걸음", f"상대 마법 피해 {enemy.mix[1]:.0%}"
    elif enemy.mix[0] >= 0.65 and (enemy.marksmen >= 2 or role != "dealer"):
        choice, reason = "판금 장화", f"상대 물리 피해 {enemy.mix[0]:.0%}"
    elif role == "enchanter":
        choice, reason = "명석함의 아이오니아 장화", "스킬 가속 (서포터)"
    elif dmg_type == "ap":
        choice, reason = "마법사의 신발", "마법 피해 딜러"
    elif "Marksman" in me.champion["tags"]:
        choice, reason = "광전사의 군화", "원거리 딜러"
    else:
        choice, reason = "명석함의 아이오니아 장화", "스킬 가속"
    result = {"name": choice, "item_id": names.get(choice), "reason": reason}

    for iid, info in (popularity or {}).items():
        if info["source"] == "신발" and iid in gd.items:
            result["opgg"] = {"name": gd.items[iid]["name"], **info}
    return result


# ---------------------------------------------------------------- 증강 추천

def build_effects(unit):
    """보유 아이템이 어느 방향인지 (증강 효과와 같은 이름으로)."""
    st = unit.item_total()
    effects = set()
    if st.get("crit", 0) >= 0.2:
        effects.add("crit")
    if st.get("ap", 0) >= 60:
        effects.add("ap")
    if st.get("ad", 0) >= 40 or st.get("as", 0) >= 0.3:
        effects.add("ad")
    if st.get("hp", 0) >= 600 or st.get("armor", 0) + st.get("mr", 0) >= 60:
        effects.add("tank")
    if st.get("lifesteal", 0) + st.get("omnivamp", 0) >= 0.1:
        effects.add("heal")
    return effects


SYNERGY_EFFECTS = ("crit", "ad", "ap", "tank", "heal")


def augment_mean(augments, min_popular=0.01):
    """인기로 가중한 평균 성능 점수 (표본이 적은 증강을 이 값 쪽으로 당긴다)."""
    usable = [a for a in augments if (a.get("popular") or 0) >= min_popular]
    total = sum(a["popular"] for a in usable)
    return sum(a["performance"] * a["popular"] for a in usable) / total if total else 0.0


def recommend_augments(augments, role, dmg_type, enemy, ally, me=None, top=6,
                       min_popular=0.01, shrink=0.03, mean=None):
    """OP.GG 증강 통계 + 상대·아군의 아이템과 증강 + 내 아이템·증강 시너지."""
    owned = {a["name"] for a in me.augments} if me else set()
    # 인기 0인 증강은 표본이 거의 없어 성능 점수가 극단값(170 등)으로 튄다
    usable = [a for a in augments if a.get("name") and a["name"] not in owned
              and (a.get("popular") or 0) >= min_popular]
    if not usable:
        return []
    if mean is None:
        mean = augment_mean(usable, min_popular)
    my_build = build_effects(me) if me else set()
    my_augs = effect_counts(me.augments) if me else {}

    picks = []
    for aug in usable:
        text = strip_tags(aug.get("desc") or "")
        eff = classify(aug["name"] + " " + text)
        # 인기가 낮을수록 평균 쪽으로 당긴다
        pop = aug["popular"]
        score = (aug["performance"] * pop + mean * shrink) / (pop + shrink)
        reasons = []

        def add(points, reason):
            nonlocal score
            score += points
            reasons.append(reason)

        if role in ("tank", "bruiser") and "tank" in eff:
            add(2, "탱커 스탯")
        if role == "enchanter" and eff & {"heal", "shield"}:
            add(2, "회복·보호막 강화")
        if role != "tank" and dmg_type in eff:
            add(2, "물리 딜" if dmg_type == "ad" else "마법 딜")
        other = "ap" if dmg_type == "ad" else "ad"
        if role == "dealer" and other in eff and dmg_type not in eff:
            add(-2, "피해 유형이 맞지 않음")

        # 내 아이템·증강과의 시너지
        build_match = [EFFECT_NAMES[e] for e in SYNERGY_EFFECTS
                       if e in eff and e in my_build and not (e == "tank" and role in ("dealer", "enchanter"))]
        if build_match:
            add(2, f"내 아이템({', '.join(build_match)})과 시너지")
        aug_match = [EFFECT_NAMES[e] for e in SYNERGY_EFFECTS if e in eff and my_augs.get(e)]
        if aug_match:
            add(1, f"보유 증강({', '.join(aug_match)})과 시너지")

        # 상대 팀 (아이템 + 증강 + 챔피언)
        if enemy.mix[1] >= 0.6 and "마법 저항력" in text:
            add(2, "상대 마법 피해 많음")
        if enemy.mix[0] >= 0.65 and "방어력" in text:
            add(2, "상대 물리 피해 많음")
        if enemy.heal >= 2 and "anti_heal" in eff:
            add(3 if not ally.anti_heal else 1, "상대 회복 강함")
        if enemy.shield >= 2 and "shield_break" in eff and not ally.shield_break:
            add(2, "상대 보호막 많음")
        if enemy.frontline >= 2 and role != "tank" and eff & {"max_hp_dmg", "true_dmg"}:
            add(2, f"상대 전열 {enemy.frontline}명")
        if enemy.anti_heal and "heal" in eff and not eff & {"tank", "ad", "ap"}:
            add(-1, "상대에 치유 감소 있음")

        # 아군 팀
        if ally.frontline == 0 and role in ("tank", "bruiser") and "tank" in eff:
            add(1, "아군 탱커 없음")
        picks.append({**aug, "score": score, "reasons": reasons})
    picks.sort(key=lambda a: a["score"], reverse=True)
    return picks[:top]


# 리롤로 1순위보다 좋은 증강이 나올 확률이 이 이상이면 리롤을 추천한다
REROLL_WORTH = 0.15


def reroll_advice(cards, pools):
    """증강 선택 화면에서 카드별로 리롤할지 정한다.

    cards: 점수 높은 순으로 정렬한 [{"name", "score", "rerolled"}]
    pools: 카드마다 리롤하면 나올 수 있는 증강 점수 목록 (같은 등급)
    리롤은 카드마다 한 번이고, 리롤하면 원래 카드는 사라진다.
    1순위가 아닌 카드는 어차피 고르지 않으므로 리롤해도 손해가 없다.
    1순위 카드는 리롤하면 2순위와 새 카드 중 하나를 고르게 되므로 그 기댓값과 비교한다.
    """
    best = cards[0]["score"]
    second = cards[1]["score"] if len(cards) > 1 else float("-inf")
    # 손해 없는 나머지 칸 리롤을 먼저 하고, 1순위 리롤은 그 결과를 보고 정한다
    others_open = any(not c["rerolled"] and pools[i] for i, c in enumerate(cards) if i > 0)
    result = []
    for i, card in enumerate(cards):
        xs = pools[i]
        if card["rerolled"] or not xs:
            result.append({"action": "리롤함" if card["rerolled"] else "정보 없음", "p_better": None})
            continue
        p = sum(x > best for x in xs) / len(xs)
        if i == 0:
            gain = sum(max(second, x) for x in xs) / len(xs) - best
            reroll = gain > 0 and not others_open
            result.append({"action": "리롤" if reroll else "선택", "p_better": p, "gain": gain})
        else:
            result.append({"action": "리롤" if p >= REROLL_WORTH else "유지", "p_better": p})

    names = [c["name"] for c, r in zip(cards, result) if r["action"] == "리롤"]
    if result[0]["action"] == "리롤":
        keep = f" ({cards[1]['name']}은(는) 남아 있음)" if len(cards) > 1 else ""
        verdict = (f"리롤 추천: 1순위 {cards[0]['name']}도 리롤하는 편이 기대값이 높습니다{keep}. "
                   f"리롤 후 다시 비교하세요.")
    elif names:
        p = max(r["p_better"] for r in result[1:] if r["action"] == "리롤")
        verdict = (f"리롤 추천: {', '.join(names)} 리롤 (최대 {p:.0%} 확률로 {cards[0]['name']}보다 좋은 증강). "
                   f"리롤 후 새 증강으로 다시 비교하세요.")
    else:
        rest = [r["p_better"] for r in result[1:] if r["p_better"] is not None]
        chance = (f" (나머지 칸은 리롤해도 손해는 없지만 더 좋은 증강이 나올 확률 {max(rest):.0%})"
                  if rest else "")
        verdict = f"그냥 뽑기: {cards[0]['name']}{chance}"
    return result, verdict


# 증강 등급: 내 챔피언이 받을 수 있는 증강 중 지금 상황 점수가 상위 몇 %인지로 나눈다
GRADE_CUTS = (("S", 0.10), ("A", 0.30), ("B", 0.60), ("C", 0.85))


def augment_grade(score, pool):
    """pool(같은 기준으로 매긴 증강 점수들) 안에서 score의 등급 S~D."""
    if not pool:
        return None
    above = sum(x > score for x in pool) / len(pool)
    return next((grade for grade, cut in GRADE_CUTS if above < cut), "D")
