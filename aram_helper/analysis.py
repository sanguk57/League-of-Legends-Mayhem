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
                      + (st["heal_shield"] >= 0.1) + aug["heal"])
        prof.shield += (min(1.0, traits.get("shield", 0) / 2) + 0.5 * ("shield" in flags)
                        + 0.5 * (st["heal_shield"] >= 0.1) + aug["shield"])
        prof.crit += st["crit"] + 0.2 * aug["crit"]
        prof.marksmen += "Marksman" in u.champion["tags"]
        prof.frontline += is_frontline(u, values)
        prof.anti_heal |= "anti_heal" in flags or aug["anti_heal"] > 0
        prof.shield_break |= "shield_break" in flags or aug["shield_break"] > 0
        prof.augments += u.augments
    return prof


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


# ---------------------------------------------------------------- 역할 판단

def auto_role(champion):
    tags = champion["tags"]
    if tags[0] == "Tank" or (tags[0] == "Support" and "Tank" in tags):
        return "tank"
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


ROLE_LABEL = {"tank": "탱커", "dealer": "딜러", "bruiser": "브루저(탱커+딜러)"}


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
    if role != "dealer" and ally.frontline == 0 and any(st.get(k) for k in DEFENSE):
        bonus += 3
        reasons.append("아군에 탱커가 없음")
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


def recommend_items(gd, me, allies, enemies, role, dmg_type, popularity=None,
                    champ_win_rate=None, top=6):
    values = gold_values(gd)
    enemy = team_profile(enemies, values)
    ally = team_profile(allies + [me], values)
    owned = set(me.item_ids)
    base_st = me.stats()
    # 기본 공격과 스킬 피해를 같은 기준으로 비교하려고 아이템 없는 스탯으로 정규화한다
    reference = compute_stats(gd, me.champion, me.level, [])
    targets = [e.stats() for e in enemies] or [base_st]
    aa_weight = attack_weight(me.champion, dmg_type)

    tank_weight = {"tank": 1.0, "dealer": 0.0, "bruiser": 0.5}[role]
    if role == "bruiser" and ally.frontline == 0:
        tank_weight = 0.7

    ehp0 = effective_hp(base_st, enemy)
    dmg0 = damage_score(base_st, reference, me.level, dmg_type, aa_weight, targets)

    picks = []
    for iid, item in candidate_items(gd, role, dmg_type, owned).items():
        gold_k = item["gold"]["total"] / 1000
        st = me.stats([iid])
        tank = (effective_hp(st, enemy) / ehp0 - 1) * 100 / gold_k
        dealer = (damage_score(st, reference, me.level, dmg_type, aa_weight, targets) / dmg0 - 1) * 100 / gold_k
        model = tank_weight * tank + (1 - tank_weight) * dealer
        bonus, reasons = _situational(item, item_flags(item), item_stats(item), role, dmg_type, enemy, ally)
        syn_bonus, syn_reasons = _augment_synergy(item_stats(item), me.augments)
        bonus += syn_bonus
        reasons += syn_reasons
        info = (popularity or {}).get(iid)
        op = _opgg_bonus(info, champ_win_rate)
        picks.append(ItemPick(iid, item["name"], item["gold"]["total"], model + bonus + op,
                              model, bonus + op, reasons, info))
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:top], enemy, ally


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


def recommend_augments(augments, role, dmg_type, enemy, ally, me=None, top=6,
                       min_popular=0.01, shrink=0.03):
    """OP.GG 증강 통계 + 상대·아군의 아이템과 증강 + 내 아이템·증강 시너지."""
    owned = {a["name"] for a in me.augments} if me else set()
    # 인기 0인 증강은 표본이 거의 없어 성능 점수가 극단값(170 등)으로 튄다
    usable = [a for a in augments if a.get("name") and a["name"] not in owned
              and (a.get("popular") or 0) >= min_popular]
    if not usable:
        return []
    total_pop = sum(a["popular"] for a in usable)
    mean = sum(a["performance"] * a["popular"] for a in usable) / total_pop
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

        if role != "dealer" and "tank" in eff:
            add(2, "탱커 스탯")
        if role != "tank" and dmg_type in eff:
            add(2, "물리 딜" if dmg_type == "ad" else "마법 딜")
        other = "ap" if dmg_type == "ad" else "ad"
        if role == "dealer" and other in eff and dmg_type not in eff:
            add(-2, "피해 유형이 맞지 않음")

        # 내 아이템·증강과의 시너지
        build_match = [EFFECT_NAMES[e] for e in SYNERGY_EFFECTS
                       if e in eff and e in my_build and not (e == "tank" and role == "dealer")]
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
        if ally.frontline == 0 and role != "dealer" and "tank" in eff:
            add(1, "아군 탱커 없음")
        picks.append({**aug, "score": score, "reasons": reasons})
    picks.sort(key=lambda a: a["score"], reverse=True)
    return picks[:top]
