import json
import unittest
from pathlib import Path

from aram_helper import ddragon, history, live, notify
from aram_helper.analysis import (TeamProfile, auto_role, buy_plan, champion_traits, effective_hp,
                                  effective_resist, recommend_augments, support_power)
from aram_helper.augments import classify
from aram_helper.cli import should_report
from aram_helper.opgg import item_popularity, parse_response_text
from aram_helper.stats import item_flags, item_stats

ROOT = Path(__file__).resolve().parent.parent


class OpggParserTest(unittest.TestCase):
    def test_nested_objects_and_lists(self):
        text = ('class R: data\nclass Data: core,list\nclass Item: ids,name,pick_rate\n\n'
                'R(Data(Item([3070,3078],"여신의 \\"눈물\\"",0.28),[Item([1],"a",1e-05),Item([],null,true)]))')
        data = parse_response_text(text)
        self.assertEqual(data["data"]["core"]["ids"], [3070, 3078])
        self.assertEqual(data["data"]["core"]["name"], '여신의 "눈물"')
        self.assertEqual(data["data"]["list"][0]["pick_rate"], 1e-05)
        self.assertIsNone(data["data"]["list"][1]["name"])
        self.assertTrue(data["data"]["list"][1]["pick_rate"])

    def test_item_popularity_keeps_highest_pick_rate(self):
        build = {"core_items": {"ids": [1, 2], "play": 100, "win": 55, "pick_rate": 0.2},
                 "fourth_items": [{"ids": [2], "play": 50, "win": 20, "pick_rate": 0.4}]}
        pop = item_popularity(build)
        self.assertEqual(pop["1"]["source"], "코어")
        self.assertEqual(pop["2"]["source"], "4번째")
        self.assertAlmostEqual(pop["1"]["win_rate"], 0.55)


class StatsTest(unittest.TestCase):
    def test_parse_stats_and_flags(self):
        item = {"description": "<mainText><stats>공격력 <attention>35</attention><br>방어구 관통력 "
                               "<attention>30%</attention><br>마법 관통력 <attention>40%</attention>"
                               "</stats><br><br><passive>고통스러운 상처</passive></mainText>"}
        st = item_stats(item)
        self.assertEqual(st["ad"], 35)
        self.assertAlmostEqual(st["apen_pct"], 0.30)
        self.assertAlmostEqual(st["mpen_pct"], 0.40)
        self.assertIn("anti_heal", item_flags(item))

    def test_ap_multiplier(self):
        item = {"description": "<stats>주문력 <attention>130</attention></stats><br>"
                               "총 <scaleAP>주문력이 30%</scaleAP> 증가합니다."}
        self.assertAlmostEqual(item_stats(item)["ap_mult"], 0.30)


class ModelTest(unittest.TestCase):
    def test_effective_resist_applies_percent_then_flat(self):
        self.assertAlmostEqual(effective_resist(100, 0.3, 10), 60)
        self.assertEqual(effective_resist(10, 0, 20), 0)

    def test_effective_hp_pure_physical(self):
        prof = TeamProfile(mix=(1.0, 0.0, 0.0))
        self.assertAlmostEqual(effective_hp({"hp": 1000, "armor": 100, "mr": 0}, prof), 2000)

    def test_armor_more_valuable_against_physical_team(self):
        st = {"hp": 2000, "armor": 50, "mr": 50}
        more_armor = dict(st, armor=100)
        more_mr = dict(st, mr=100)
        prof = TeamProfile(mix=(0.8, 0.2, 0.0))
        self.assertGreater(effective_hp(more_armor, prof), effective_hp(more_mr, prof))

    def test_augments_skip_zero_popularity(self):
        augments = [{"name": "A", "desc": "", "performance": 170, "popular": 0, "tier": 5},
                    {"name": "B", "desc": "", "performance": 90, "popular": 0.1, "tier": 4}]
        picks = recommend_augments(augments, "dealer", "ap", TeamProfile(), TeamProfile())
        self.assertEqual([p["name"] for p in picks], ["B"])


class AugmentTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("<scaleHealth>최대 체력이 30%</scaleHealth> 감소합니다. "
                                  "<trueDamage>고정 피해</trueDamage>를 입힙니다."), {"true_dmg"})
        self.assertIn("shield", classify("잠시 동안 <shield>보호막</shield>을 획득합니다."))
        self.assertIn("anti_heal", classify("적에게 40%의 상처를 남깁니다."))
        self.assertIn("tank", classify("방어력과 마법 저항력이 20 증가합니다."))

    def test_enemy_heal_augments_boost_anti_heal_augment(self):
        enemy = TeamProfile(heal=3)
        augments = [{"name": "치감", "desc": "적에게 40%의 상처를 남깁니다.", "performance": 80, "popular": 0.1, "tier": 4},
                    {"name": "보통", "desc": "이동 속도가 증가합니다.", "performance": 81, "popular": 0.1, "tier": 4}]
        picks = recommend_augments(augments, "dealer", "ap", enemy, TeamProfile())
        self.assertEqual(picks[0]["name"], "치감")
        self.assertIn("상대 회복 강함", picks[0]["reasons"])

    def test_live_augment_fields(self):
        player = {"augments": [{"displayName": "둠"}, {"id": 1325}], "items": []}
        self.assertEqual(live._augment_names(player), ["둠", "1325"])
        self.assertEqual(live._augment_names({"items": []}), [])


class ReportTimingTest(unittest.TestCase):
    def test_report_only_when_just_died(self):
        alive = {"dead": False, "time": 600}
        dead = {"dead": True, "time": 610}
        self.assertTrue(should_report(dead, prev_dead=False, first=False, always=False, changed=False))
        self.assertFalse(should_report(dead, prev_dead=True, first=False, always=False, changed=True))
        self.assertFalse(should_report(alive, prev_dead=True, first=False, always=False, changed=True))

    def test_report_at_game_start(self):
        self.assertTrue(should_report({"dead": False, "time": 20}, False, True, False, True))
        self.assertFalse(should_report({"dead": False, "time": 600}, False, True, False, True))

    def test_always_reports_on_change(self):
        self.assertTrue(should_report({"dead": False, "time": 600}, False, False, True, True))


def _cached_game_data():
    versions = [p.name for p in (ddragon.DATA_DIR / "ddragon").glob("*") if p.is_dir()]
    if not versions:
        return None
    return ddragon.load(max(versions, key=ddragon._version_key))


@unittest.skipIf(_cached_game_data() is None, "Data Dragon 캐시가 없음 (한 번 실행하면 생성됨)")
class LiveParseTest(unittest.TestCase):
    def test_sample_game(self):
        gd = _cached_game_data()
        data = json.loads((ROOT / "samples" / "sample_allgamedata.json").read_text("utf-8"))
        me, allies, enemies, info = live.parse_game(gd, data)
        self.assertEqual(me.champion["id"], "Ahri")
        self.assertEqual(len(allies), 4)
        self.assertEqual(len(enemies), 5)
        self.assertTrue(info["dead"])
        # 실제 스탯(증강 등 포함)이 계산값 대신 쓰인다
        self.assertAlmostEqual(me.stats()["hp"], 1650.0)
        self.assertEqual(info["gold"], 1300.0)
        ashe = next(u for u in enemies if u.champion["id"] == "Ashe")
        self.assertEqual(ashe.killed_me, 2)
        self.assertEqual(ashe.kills, 9)

    def test_champion_traits_from_spell_text(self):
        gd = _cached_game_data()
        self.assertGreaterEqual(champion_traits(gd, gd.champions["Soraka"]).get("heal", 0), 2)
        self.assertGreaterEqual(champion_traits(gd, gd.champions["Lulu"]).get("shield", 0), 1)
        self.assertGreaterEqual(champion_traits(gd, gd.champions["Vayne"]).get("true_dmg", 0), 1)
        self.assertEqual(champion_traits(gd, gd.champions["Jinx"]).get("heal", 0), 0)


class _FakeGameData:
    """buy_plan용: 아이템 트리만 있는 게임 데이터."""

    def __init__(self, items):
        self.items = {iid: {"name": iid, "from": frm, "gold": {"base": base}}
                      for iid, (base, frm) in items.items()}


class BuyPlanTest(unittest.TestCase):
    # T(조합 800) = A + B, A(조합 300) = a + a, B(조합 500) = a, a = 400
    # 전체 비용: A 1100, B 900, T 2800
    GD =_FakeGameData({"T": (800, ["A", "B"]), "A": (300, ["a", "a"]), "B": (500, ["a"]), "a": (400, [])})

    def test_owned_component_reduces_cost(self):
        plan = buy_plan(self.GD, "T", ["a"], 3000)
        # 전체 2800 - 보유한 a 400
        self.assertTrue(plan["complete"])
        self.assertEqual(plan["cost"], 2400)
        self.assertEqual(plan["buy"], [("T", 2400)])

    def test_partial_buys_most_expensive_affordable_component(self):
        plan = buy_plan(self.GD, "T", [], 1000)
        self.assertFalse(plan["complete"])
        # A(1100)는 못 사므로 B(900)를 사고, 남은 100으로는 아무것도 못 산다
        self.assertEqual(plan["buy"], [("B", 900)])
        self.assertEqual(plan["left"], 100)

    def test_full_inventory_blocks_components(self):
        plan = buy_plan(self.GD, "T", ["x1", "x2", "x3", "x4", "x5", "x6"], 1000)
        self.assertEqual(plan["buy"], [])
        self.assertTrue(plan["full"])


class SupportAndHistoryTest(unittest.TestCase):
    def test_support_power_grows_with_heal_shield(self):
        base = {"heal_shield": 0.0, "ah": 0.0, "ap": 0.0}
        self.assertGreater(support_power(dict(base, heal_shield=0.1)), support_power(base))

    def test_auto_role_enchanter(self):
        self.assertEqual(auto_role({"tags": ["Support", "Mage"]}), "enchanter")
        self.assertEqual(auto_role({"tags": ["Support", "Tank"]}), "tank")

    def test_augment_without_stats_uses_mean(self):
        augments = [{"name": "통계", "desc": "", "performance": 100, "popular": 0.1, "tier": 4},
                    {"name": "없음", "desc": "", "performance": 0, "popular": 0, "tier": "-"}]
        picks = recommend_augments(augments, "dealer", "ap", TeamProfile(), TeamProfile(),
                                   top=2, min_popular=0, mean=90)
        self.assertAlmostEqual(next(p for p in picks if p["name"] == "없음")["score"], 90)

    def test_follow_rate(self):
        game = {"reports": [{"items": ["신발"], "recommended": ["라바돈", "존야"]},
                            {"items": ["신발", "라바돈"], "recommended": ["존야"]}],
                "final_items": ["신발", "라바돈", "공허"]}
        self.assertEqual(history.follow_rate(game), 0.5)
        self.assertIsNone(history.follow_rate({"reports": [], "final_items": []}))

    def test_speech_starts_with_buy_now(self):
        text = notify.speech_text({"items": ["라바돈"], "augments": [], "buy_now": ["증폭의 고서"]})
        self.assertTrue(text.startswith("지금 구매 증폭의 고서"))


class LiveExtrasTest(unittest.TestCase):
    def test_spells_and_killers(self):
        player = {"summonerSpells": {
            "summonerSpellOne": {"displayName": "점화", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerDot_DisplayName"},
            "summonerSpellTwo": {"displayName": "표식", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerSnowball_DisplayName"}}}
        self.assertEqual(live._spells(player), ["ignite"])
        events = [{"EventName": "ChampionKill", "KillerName": "적", "VictimName": "나"},
                  {"EventName": "ChampionKill", "KillerName": "적", "VictimName": "남"},
                  {"EventName": "ChampionKill", "KillerName": "적", "VictimName": "나"}]
        self.assertEqual(live._killers_of(events, {"나#KR1", "나"})["적"], 2)
        self.assertEqual(live._player_names({"riotId": "나#KR1"}), {"나#KR1", "나"})


if __name__ == "__main__":
    unittest.main()
