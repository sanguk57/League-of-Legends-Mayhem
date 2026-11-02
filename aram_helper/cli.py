"""명령줄 실행.

  python -m aram_helper live [--watch 2] [--speak] [--serve 8765]
  python -m aram_helper efficiency
"""

import argparse
import json
import sys
import time

from . import augments as aug_data
from . import ddragon, live, notify, opgg
from .analysis import (ROLE_LABEL, auto_damage_type, auto_role, describe_team, recommend_augments,
                       recommend_boots, recommend_items)
from .stats import gold_values, item_stats, stat_gold


def resolve_augments(units, use_opgg, my_augment_stats=()):
    """증강 이름을 카탈로그에서 찾아 효과를 분류한다. 설명은 OP.GG 챔피언별 목록으로 보강한다."""
    if not any(u.augment_names for u in units):
        return
    try:
        catalog = aug_data.load_catalog()
    except OSError as e:
        print(f"[경고] 증강 데이터를 가져오지 못했습니다: {e}", file=sys.stderr)
        return
    catalog.add_descriptions(my_augment_stats)
    for u in units:
        if not u.augment_names:
            continue
        if use_opgg:
            try:
                catalog.add_descriptions(opgg.aram_augments(u.champion["key"]))
            except opgg.OpggError:
                pass
        u.augments = []
        for name in u.augment_names:
            try:
                u.augments.append(catalog.find(name))
            except ValueError as e:
                print(f"[경고] {u.champion['name']}: {e}", file=sys.stderr)


def fetch_opgg(champion, use_opgg):
    if not use_opgg:
        return {}, None, []
    try:
        build = opgg.aram_build(champion["id"])
        champ_wr = (build.get("summary", {}).get("average_stats") or {}).get("win_rate")
        popularity = opgg.item_popularity(build)
    except opgg.OpggError as e:
        print(f"[경고] OP.GG 빌드 데이터를 가져오지 못했습니다: {e}", file=sys.stderr)
        popularity, champ_wr = {}, None
    try:
        augments = opgg.aram_augments(champion["key"])
    except opgg.OpggError as e:
        print(f"[경고] OP.GG 증강 데이터를 가져오지 못했습니다: {e}", file=sys.stderr)
        augments = []
    return popularity, champ_wr, augments


def report(gd, me, allies, enemies, role, dmg_type, use_opgg, top):
    values = gold_values(gd)
    role = role if role != "auto" else auto_role(me.champion)
    dmg_type = dmg_type if dmg_type != "auto" else auto_damage_type(me, values)
    popularity, champ_wr, augments = fetch_opgg(me.champion, use_opgg)
    resolve_augments([me] + allies + enemies, use_opgg, augments)

    picks, enemy, ally = recommend_items(gd, me, allies, enemies, role, dmg_type,
                                         popularity, champ_wr, top)
    out = []
    owned = ", ".join(gd.items[i]["name"] for i in me.item_ids) or "없음"
    out.append(f"== {me.champion['name']} (레벨 {me.level}) / 역할: {ROLE_LABEL[role]} / "
               f"피해: {'물리' if dmg_type == 'ad' else '마법'} / 패치 데이터 {gd.version}")
    out.append(f"   보유 아이템: {owned}")
    if me.augments:
        out.append("   보유 증강: " + ", ".join(aug_data.describe(a) for a in me.augments))
    if champ_wr:
        out.append(f"   OP.GG 칼바람 승률: {champ_wr:.1%}")
    core = [iid for iid, info in popularity.items() if info["source"] == "코어" and iid in gd.items]
    if core:
        out.append("   OP.GG 칼바람 코어 빌드: " + " → ".join(gd.items[i]["name"] for i in core))
        if not set(core) & {p.item_id for p in picks} - set(me.item_ids):
            out.append("   (현재 역할 기준 추천과 겹치지 않습니다. 다른 빌드를 보려면 --role 또는 --damage를 바꿔 보세요)")

    out.append("\n[팀 분석]")
    out += ["  " + line for line in describe_team(enemy, "상대")]
    out += ["  " + line for line in describe_team(ally, "아군")]

    boots = recommend_boots(gd, me, role, dmg_type, enemy, popularity)
    if boots:
        line = f"\n[신발] {boots['name']} ({boots['reason']})"
        if boots.get("opgg") and boots["opgg"]["name"] != boots["name"]:
            line += f" / OP.GG 최다 선택: {boots['opgg']['name']} (픽률 {boots['opgg']['pick_rate']:.0%})"
        out.append(line)

    out.append("\n[추천 아이템] 점수 = 모델(1000골드당 효율 %) + 상황·OP.GG 보정")
    for n, p in enumerate(picks, 1):
        out.append(f" {n}. {p.name} ({p.gold}g)  점수 {p.score:.1f}  (모델 {p.model:.1f} + 보정 {p.bonus:+.1f})")
        details = list(p.reasons)
        if p.opgg:
            details.append(f"OP.GG {p.opgg['source']} 픽률 {p.opgg['pick_rate']:.0%}, "
                           f"승률 {p.opgg['win_rate']:.1%}")
        if details:
            out.append("      - " + " / ".join(details))

    aug_picks = recommend_augments(augments, role, dmg_type, enemy, ally, me, top)
    if aug_picks:
        out.append("\n[추천 증강] OP.GG 성능 점수(인기 낮으면 평균 쪽으로 보정) + 상황 보정")
        for n, a in enumerate(aug_picks, 1):
            reasons = f"  - {', '.join(a['reasons'])}" if a["reasons"] else ""
            out.append(f" {n}. {a['name']}  점수 {a['score']:.1f} (성능 {a['performance']}, "
                       f"인기 {a['popular']}, 등급 {a['tier']}){reasons}")
    elif use_opgg:
        out.append("\n[추천 증강] OP.GG 증강 데이터 없음")
    summary = {"items": [p.name for p in picks], "boots": boots["name"] if boots else None,
               "augments": [a["name"] for a in aug_picks]}
    return "\n".join(out), summary


# 게임 시작 후 이 시간(초) 안에는 우물에서 아이템을 살 수 있으므로 추천을 보여준다
START_SHOPPING_SECONDS = 90


def should_report(info, prev_dead, first, always, changed):
    """칼바람은 죽었을 때(또는 게임 시작 직후)만 아이템을 살 수 있으므로 그때만 계산한다."""
    if always:
        return changed
    if first:
        return info["dead"] or info["time"] < START_SHOPPING_SECONDS
    return info["dead"] and not prev_dead


def cmd_live(args):
    gd = ddragon.load()
    board = None
    if args.serve:
        board = notify.WebBoard(args.serve)
        print("추천 웹페이지: " + " , ".join(board.urls()))
    last_key = None
    prev_dead = False
    first = True
    while True:
        try:
            if args.file:
                with open(args.file, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = live.fetch_game(args.host, args.port)
        except OSError as e:
            print(f"게임에 연결하지 못했습니다 ({e}). 게임이 진행 중인지 확인하세요.", file=sys.stderr)
            if board:
                board.update("게임을 기다리는 중")
            if not args.watch:
                return 1
            time.sleep(args.watch)
            continue

        me, allies, enemies, info = live.parse_game(gd, data)
        key = [(u.champion["id"], u.level, tuple(u.item_ids)) for u in [me] + allies + enemies]
        changed = key != last_key
        last_key = key
        if first and args.watch and not args.always:
            # 죽었을 때 바로 결과가 나오도록 OP.GG 데이터를 미리 받아 캐시에 둔다
            fetch_opgg(me.champion, not args.no_opgg)
            print(f"{me.champion['name']} 감시 중: 죽거나 게임 시작 직후에 추천을 보여줍니다.")

        minutes, seconds = divmod(int(info["time"]), 60)
        status = f"사망 (부활까지 {info['respawn']:.0f}초)" if info["dead"] else "생존"
        if board:
            board.update(f"{me.champion['name']} / {minutes}:{seconds:02d} / {status}", dead=info["dead"])

        if not args.watch or should_report(info, prev_dead, first, args.always, changed):
            lines = [f"\n######## 게임 모드 {info['mode']} / {minutes}:{seconds:02d} / {status} ########"]
            for label, team in (("아군", allies), ("상대", enemies)):
                for u in team:
                    items = ", ".join(gd.items[i]["name"] for i in u.item_ids) or "-"
                    augs = f" | 증강: {', '.join(u.augment_names)}" if u.augment_names else ""
                    lines.append(f"  [{label}] {u.champion['name']} Lv{u.level}: {items}{augs}")
            text, summary = report(gd, me, allies, enemies, args.role, args.damage,
                                   not args.no_opgg, args.top)
            print("\n".join(lines))
            print(text)
            if board:
                board.update(board.status, f"{minutes}:{seconds:02d} 기준 추천\n\n{text}", dead=info["dead"])
            if args.speak:
                notify.speak(notify.speech_text(summary))
        prev_dead = info["dead"]
        first = False
        if not args.watch:
            return 0
        time.sleep(args.watch)


def cmd_efficiency(args):
    gd = ddragon.load()
    values = gold_values(gd)
    rows = []
    aram = gd.aram_items()
    for iid, item in aram.items():
        upgradable = any(i in aram for i in item.get("into", []))
        if upgradable or item["gold"]["total"] < 2000:
            continue
        st = item_stats(item)
        value = stat_gold(st, values)
        rows.append((value / item["gold"]["total"], item["name"], item["gold"]["total"]))
    rows.sort(reverse=True)
    print("칼바람 완성 아이템 골드 효율 (스탯만 계산, 고유 효과와 일부 스탯은 미반영)")
    print("스탯 1단위 가치: " + ", ".join(f"{k} {v:.1f}g" for k, v in values.items()))
    for eff, name, gold in rows[:args.top]:
        print(f"  {eff:6.1%}  {name} ({gold}g)")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aram_helper", description="칼바람 나락 아이템·증강 추천")
    sub = parser.add_subparsers(dest="command", required=True)

    lv = sub.add_parser("live", help="게임 중 Live Client Data API로 자동 분석")
    lv.add_argument("--role", choices=["auto", "tank", "dealer", "bruiser"], default="auto",
                    help="내 역할 (기본: 챔피언 분류로 자동 판단)")
    lv.add_argument("--damage", choices=["auto", "ad", "ap"], default="auto", help="내 피해 유형")
    lv.add_argument("--top", type=int, default=6, help="추천 개수")
    lv.add_argument("--no-opgg", action="store_true", help="OP.GG 데이터 없이 계산")
    lv.add_argument("--host", default="127.0.0.1")
    lv.add_argument("--port", type=int, default=2999)
    lv.add_argument("--watch", type=int, default=0,
                    help="N초마다 확인하고 죽었을 때 추천 (0이면 한 번만 바로 계산). 권장: 2")
    lv.add_argument("--always", action="store_true",
                    help="죽었을 때만이 아니라 아이템·레벨이 바뀔 때마다 계산")
    lv.add_argument("--speak", action="store_true",
                    help="추천을 음성으로 읽어줌 (전체 화면 게임 중에도 사용 가능, Windows 음성 합성)")
    lv.add_argument("--serve", type=int, metavar="PORT",
                    help="휴대폰·보조 모니터에서 볼 수 있는 웹페이지를 이 포트로 띄움 (예: 8765)")
    lv.add_argument("--file", help="API 대신 저장된 allgamedata JSON 파일 사용 (테스트용)")
    lv.set_defaults(func=cmd_live)

    ef = sub.add_parser("efficiency", help="아이템 골드 효율표")
    ef.add_argument("--top", type=int, default=30)
    ef.set_defaults(func=cmd_efficiency)

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
