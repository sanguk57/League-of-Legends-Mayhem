"""명령줄 실행.

  python -m aram_helper live [--watch 2] [--speak] [--serve 8765]
  python -m aram_helper pick 증강1 증강2 증강3
  python -m aram_helper history
  python -m aram_helper efficiency
"""

import argparse
import json
import re
import sys
import threading
import time

from . import augments as aug_data
from . import ddragon, history, live, notify, opgg, screen
from .analysis import (INVENTORY_SLOTS, ROLE_LABEL, Model, augment_mean, auto_damage_type, auto_role, buy_plan,
                       describe_team, recommend_augments, recommend_boots, recommend_items,
                       recommend_swap, reroll_advice, augment_grade)
from .ddragon import normalize
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


def context(gd, me, allies, enemies, role, dmg_type, use_opgg):
    """추천에 필요한 것을 한 번에 준비한다: 역할·피해 유형, OP.GG 데이터, 증강 해석, 계산 모델."""
    values = gold_values(gd)
    role = role if role != "auto" else auto_role(me.champion)
    dmg_type = dmg_type if dmg_type != "auto" else auto_damage_type(me, values)
    popularity, champ_wr, augments = fetch_opgg(me.champion, use_opgg)
    resolve_augments([me] + allies + enemies, use_opgg, augments)
    model = Model(gd, me, allies, enemies, role, dmg_type)
    return {"gd": gd, "me": me, "allies": allies, "enemies": enemies, "role": role,
            "dmg_type": dmg_type, "popularity": popularity, "champ_wr": champ_wr,
            "augments": augments, "model": model, "use_opgg": use_opgg}


def _buy_now_lines(gd, plan, gold):
    if plan["full"] and not plan["buy"]:
        return [f"  칸이 가득 차서 살 수 없습니다 (보유 {gold:.0f}g). 아래 교체 추천을 참고하세요."], []
    target = gd.items[plan["target"]]["name"]
    if plan["complete"]:
        return [f"  → {target} 완성 ({plan['cost']}g, 남는 골드 {plan['left']:.0f}g)"], [target]
    names = [gd.items[i]["name"] for i, _ in plan["buy"]]
    if not names:
        return [f"  골드가 모자랍니다 (보유 {gold:.0f}g, {target}까지 {plan['cost']}g)."], []
    bought = ", ".join(f"{gd.items[i]['name']}({c}g)" for i, c in plan["buy"])
    spent = sum(c for _, c in plan["buy"])
    return [f"  → {bought} 구매 / {target}까지 {plan['cost'] - spent}g 남음"], names


def report(ctx, top, gold=None, augment_first=False):
    gd, me = ctx["gd"], ctx["me"]
    role, dmg_type, popularity = ctx["role"], ctx["dmg_type"], ctx["popularity"]
    picks, enemy, ally = recommend_items(gd, me, ctx["allies"], ctx["enemies"], role, dmg_type,
                                         popularity, ctx["champ_wr"], top, model=ctx["model"])
    out = []
    owned = ", ".join(gd.items[i]["name"] for i in me.item_ids) or "없음"
    out.append(f"== {me.champion['name']} (레벨 {me.level}) / 역할: {ROLE_LABEL[role]} / "
               f"피해: {'물리' if dmg_type == 'ad' else '마법'} / 패치 데이터 {gd.version}")
    out.append(f"   보유 아이템: {owned}")
    if me.augments:
        out.append("   보유 증강: " + ", ".join(aug_data.describe(a) for a in me.augments))
    if ctx["champ_wr"]:
        out.append(f"   OP.GG 칼바람 승률: {ctx['champ_wr']:.1%}")
    core = [iid for iid, info in popularity.items() if info["source"] == "코어" and iid in gd.items]
    if core:
        out.append("   OP.GG 칼바람 코어 빌드: " + " → ".join(gd.items[i]["name"] for i in core))
        if not set(core) & {p.item_id for p in picks} - set(me.item_ids):
            out.append("   (현재 역할 기준 추천과 겹치지 않습니다. 다른 빌드를 보려면 --role 또는 --damage를 바꿔 보세요)")

    aug_pool = augment_pool(ctx)
    pool_scores = [a["score"] for a in aug_pool]
    aug_picks = [{**a, "grade": augment_grade(a["score"], pool_scores)} for a in aug_pool[:top]]
    aug_lines = []
    if aug_picks:
        aug_lines.append("\n[추천 증강] 등급 = 받을 수 있는 증강 중 상위 %(S 10%, A 30%, B 60%, C 85%) / "
                         "점수 = OP.GG 성능(인기 낮으면 평균 쪽으로 보정) + 상황 보정")
        for n, a in enumerate(aug_picks, 1):
            reasons = f"  - {', '.join(a['reasons'])}" if a["reasons"] else ""
            aug_lines.append(f" {n}. [{a['grade']}] {a['name']}  점수 {a['score']:.1f} "
                             f"(성능 {a['performance']}, 인기 {a['popular']}){reasons}")
    elif ctx["use_opgg"]:
        aug_lines.append("\n[추천 증강] OP.GG 증강 데이터 없음")
    if augment_first:
        aug_lines[0:0] = ["\n★ 증강 선택 가능: 상점에서 증강을 고르세요 (선택지 비교는 웹페이지 또는 pick 명령)"]
        out += aug_lines

    buy_now, swap_text, plan = [], None, None
    swap = recommend_swap(gd, me, ctx["model"], picks, gold=gold)
    if swap:
        swap_text = f"{swap['sell']} 팔고 {swap['buy']}"
        out.append(f"\n[교체 추천] 칸이 가득 참: {swap['sell']} 판매(+{swap['sell_gold']}g) → "
                   f"{swap['buy']} (예상 이득 {swap['gain']:+.1f}점)")
    if picks and gold is not None:
        inventory, target, budget = me.inventory_ids(), picks[0].item_id, gold
        # 칸이 가득 찼으면 보유 재료로 완성할 수 있는 추천 아이템을 목표로 한다
        complete = [p.item_id for p in picks[:3] if buy_plan(gd, p.item_id, inventory, gold)["complete"]]
        if len(inventory) >= INVENTORY_SLOTS and complete:
            target = complete[0]
        if swap:
            # 판매한 칸과 골드로 교체 대상 아이템을 향해 산다
            inventory.remove(swap["sell_id"])
            target, budget = swap["buy_id"], gold + swap["sell_gold"]
        plan = buy_plan(gd, target, inventory, budget)
        lines, buy_now = _buy_now_lines(gd, plan, budget)
        sold = f" + 판매 {swap['sell_gold']}g" if swap else ""
        out.append(f"\n[지금 살 것] 보유 골드 {gold:.0f}g{sold}, 목표: {gd.items[target]['name']}")
        out += lines

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

    if not augment_first:
        out += aug_lines
    summary = {"items": [p.name for p in picks], "boots": boots["name"] if boots else None,
               "augments": [a["name"] for a in aug_picks], "buy_now": buy_now, "swap": swap_text,
               "augment_first": augment_first}
    summary["view"] = _view(ctx, picks, aug_picks, plan, swap, boots, core, enemy, ally, gold, augment_first)
    return "\n".join(out), summary


def _item_view(gd, iid, cost=None):
    item = gd.items[iid]
    return {"id": iid, "name": item["name"], "gold": item["gold"]["total"] if cost is None else cost}


def _unit_view(gd, u):
    return {"id": u.champion["id"], "name": u.champion["name"], "level": u.level,
            "kda": f"{u.kills}/{u.deaths}/{u.assists}", "items": [i for i in u.item_ids if i in gd.items]}


def _view(ctx, picks, aug_picks, plan, swap, boots, core, enemy, ally, gold, augment_first):
    """웹페이지에 그릴 추천 결과 (JSON으로 보냄)."""
    gd, me = ctx["gd"], ctx["me"]
    try:
        icons = {a["id"]: a.get("icon") for a in aug_data.load_catalog().by_id.values()}
    except OSError:
        icons = {}
    buy = None
    if plan:
        spent = sum(c for _, c in plan["buy"])
        buy = {"target": _item_view(gd, plan["target"], plan["cost"]),
               "items": [_item_view(gd, i, c) for i, c in plan["buy"]],
               "complete": plan["complete"], "full": plan["full"] and not plan["buy"],
               "remaining": plan["cost"] - spent, "left": round(plan["left"]),
               "sold": swap["sell_gold"] if swap else 0}
    items = []
    for p in picks:
        reasons = list(p.reasons)
        if p.opgg:
            reasons.append(f"OP.GG {p.opgg['source']} 픽률 {p.opgg['pick_rate']:.0%}, 승률 {p.opgg['win_rate']:.1%}")
        items.append({**_item_view(gd, p.item_id), "score": round(p.score, 1), "model": round(p.model, 1),
                      "bonus": round(p.bonus, 1), "reasons": reasons})
    return {
        "version": gd.version,
        "me": {**_unit_view(gd, me), "role": ROLE_LABEL[ctx["role"]],
               "dmg": "물리" if ctx["dmg_type"] == "ad" else "마법",
               "augments": [a["name"] for a in me.augments]},
        "gold": None if gold is None else round(gold),
        "champ_wr": ctx["champ_wr"],
        "augment_first": augment_first,
        "buy": buy,
        "swap": swap and {"sell": _item_view(gd, swap["sell_id"], swap["sell_gold"]),
                          "buy": _item_view(gd, swap["buy_id"]), "gain": round(swap["gain"], 1)},
        "items": items,
        "augments": [{"name": a["name"], "icon": icons.get(a.get("id")), "score": round(a["score"], 1),
                      "grade": a["grade"], "reasons": a["reasons"]} for a in aug_picks],
        "boots": boots and boots.get("item_id") and {**_item_view(gd, boots["item_id"]), "reason": boots["reason"]},
        "core": [_item_view(gd, i) for i in core],
        "teams": [
            {"label": "상대", "mix": enemy.mix, "lines": describe_team(enemy, "상대"),
             "units": [_unit_view(gd, u) for u in ctx["enemies"]]},
            {"label": "아군", "mix": ally.mix, "lines": describe_team(ally, "아군"),
             "units": [_unit_view(gd, u) for u in [me] + ctx["allies"]]},
        ],
    }


def augment_pool(ctx):
    """내 챔피언이 받을 수 있는 증강 전체를 지금 상황 점수로 매긴 목록 (점수 높은 순, 보유 증강 제외)."""
    model = ctx["model"]
    return recommend_augments(ctx["augments"], ctx["role"], ctx["dmg_type"], model.enemy, model.ally,
                              ctx["me"], top=len(ctx["augments"]))


def rank_choices(ctx, names):
    """증강 선택 화면에 나온 증강들의 순위와 리롤 여부. OP.GG 통계가 없는 증강은 평균 성능으로 본다.
    이름 끝에 *를 붙이면 이미 리롤한 칸으로 본다 (카드마다 리롤은 한 번)."""
    by_name = {normalize(a["name"]): a for a in ctx["augments"] if a.get("name")}
    try:
        catalog = aug_data.load_catalog()
        catalog.add_descriptions(ctx["augments"])
    except OSError:
        catalog = None
    rerolled = {normalize(n.rstrip("*")) for n in names if n.strip().endswith("*")}
    names = [n.strip().rstrip("*").strip() for n in names]
    cands, missing, unknown = [], [], []
    for slot, name in enumerate(names):
        aug = by_name.get(normalize(name))
        if aug is None:
            found = {"name": name, "desc": ""}
            try:
                found = catalog.find(name) if catalog else found
            except ValueError:
                unknown.append(name)
            aug = {"id": found.get("id"), "name": found["name"], "desc": found.get("desc", ""),
                   "performance": 0, "popular": 0, "tier": "-"}
            missing.append(aug["name"])
        # 입력한 칸 번호 (웹페이지가 칸마다 등급을 붙일 때 쓴다)
        cands.append({**aug, "slot": slot})
    enemy, ally = ctx["model"].enemy, ctx["model"].ally
    picks = recommend_augments(cands, ctx["role"], ctx["dmg_type"], enemy, ally, ctx["me"],
                               top=len(cands), min_popular=0, mean=augment_mean(ctx["augments"]))
    # 리롤하면 나올 수 있는 증강: 내 챔피언의 OP.GG 증강 중 같은 희귀도, 지금 화면에 없는 것
    pool = augment_pool(ctx)
    pool_scores = [p["score"] for p in pool]
    rarity = (lambda a: catalog.by_id.get(a.get("id"), {}).get("rarity")) if catalog else (lambda a: None)
    shown = {normalize(a["name"]) for a in picks}
    pools = []
    for a in picks:
        same = [p["score"] for p in pool if normalize(p["name"]) not in shown and rarity(p) == rarity(a)]
        # 등급 정보가 없거나 후보가 너무 적으면 전체에서 고른다고 본다
        pools.append(same if len(same) >= 5 else [p["score"] for p in pool if normalize(p["name"]) not in shown])
    cards = [{"name": a["name"], "score": a["score"], "rerolled": normalize(a["name"]) in rerolled} for a in picks]
    advice, verdict = reroll_advice(cards, pools)

    out = [f"{ctx['me'].champion['name']} ({ROLE_LABEL[ctx['role']]}) 기준 증강 순위"]
    rows = []
    for n, (a, adv) in enumerate(zip(picks, advice), 1):
        if a["name"] in unknown:
            stat = "이름을 찾지 못함, 평균으로 계산"
        elif a["name"] in missing:
            stat = "OP.GG 통계 없음, 평균으로 계산"
        else:
            stat = f"성능 {a['performance']}"
        reasons = f"  - {', '.join(a['reasons'])}" if a["reasons"] else ""
        chance = f", 리롤 시 1순위보다 좋을 확률 {adv['p_better']:.0%}" if adv["p_better"] is not None else ""
        grade = augment_grade(a["score"], pool_scores)
        out.append(f" {n}. [{grade}등급 · {adv['action']}] {a['name']}  점수 {a['score']:.1f} ({stat}{chance}){reasons}")
        icon = catalog.by_id.get(a.get("id"), {}).get("icon") if catalog else None
        rows.append({"name": a["name"], "slot": a["slot"], "icon": icon, "score": round(a["score"], 1),
                     "grade": grade, "stat": stat, "reasons": a["reasons"], "action": adv["action"],
                     "p_better": adv["p_better"]})
    out.append(f"\n→ {verdict}")
    return "\n".join(out), {"rows": rows, "verdict": verdict}


def augment_names(ctx):
    """웹페이지 입력 자동 완성용 증강 이름."""
    names = {a["name"] for a in ctx["augments"] if a.get("name")}
    try:
        names |= {a["name"] for a in aug_data.load_catalog().by_id.values() if a.get("name")}
    except OSError:
        pass
    # 게임 파일에는 "???", "404: ...", 내부 이름 같은 자리 표시 값도 있다
    return sorted(n for n in names if re.search("[가-힣]", n) and not n.startswith("404"))


# 게임 시작 후 이 시간(초) 안에는 우물에서 아이템을 살 수 있으므로 추천을 보여준다
START_SHOPPING_SECONDS = 90


def should_report(info, prev_dead, first, always, changed):
    """칼바람은 죽었을 때(또는 게임 시작 직후)만 아이템을 살 수 있으므로 그때만 계산한다."""
    if always:
        return changed
    if first:
        return info["dead"] or info["time"] < START_SHOPPING_SECONDS
    return info["dead"] and not prev_dead


# 아수라장에서 증강을 고르는 레벨 (첫 증강 외에는 죽어서 우물에 있을 때만 고를 수 있다)
AUGMENT_LEVELS = (3, 7, 11, 15)


def read_game(args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return json.load(f)
    return live.fetch_game(args.host, args.port)


def team_lines(gd, allies, enemies):
    lines = []
    for label, team in (("아군", allies), ("상대", enemies)):
        for u in team:
            items = ", ".join(gd.items[i]["name"] for i in u.item_ids) or "-"
            augs = f" | 증강: {', '.join(u.augment_names)}" if u.augment_names else ""
            kda = f" ({u.kills}/{u.deaths}/{u.assists})" if u.kills or u.deaths or u.assists else ""
            lines.append(f"  [{label}] {u.champion['name']} Lv{u.level}{kda}: {items}{augs}")
    return lines


def start_screen_reader(args, gd, latest, board, use_opgg):
    """증강 선택 화면을 읽는 백그라운드 스레드. 반환한 dict의 active가 True일 때만 읽는다."""
    state = {"active": False, "seq": 0}
    tracker = screen.ChoiceTracker()
    names = []

    def loop():
        warned = False
        while True:
            if not state["active"] or "units" not in latest:
                time.sleep(0.5)
                continue
            try:
                if not names:
                    names.extend(augment_names({"augments": []}))
                lines = screen.ocr(args.screen_image, monitor=args.screen_monitor)
            except screen.OcrError as e:
                if not warned:
                    print(f"[경고] {e}", file=sys.stderr)
                    warned = True
                time.sleep(5)
                continue
            if tracker.update(screen.match_choices(lines, names)):
                ctx = context(gd, *latest["units"], args.role, args.damage, use_opgg)
                text, result = rank_choices(ctx, tracker.query())
                state["seq"] += 1
                print(f"\n[화면에서 읽은 증강 선택지] {' / '.join(tracker.query())}\n{text}")
                if board:
                    board.screen = {"seq": state["seq"], "names": tracker.names,
                                    "rerolled": tracker.rerolled, **result}
            time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()
    return state


def cmd_live(args):
    gd = ddragon.load()
    use_opgg = not args.no_opgg
    board = None
    if args.serve:
        try:
            board = notify.WebBoard(args.serve)
            print("추천 웹페이지: " + " , ".join(board.urls()))
        except OSError as e:
            # 이미 다른 창(로그인 시 자동 실행 등)이 같은 포트로 실행 중일 때
            print(f"[경고] 웹페이지를 {args.serve}번 포트로 열지 못했습니다 ({e}). "
                  f"이미 실행 중인 창이 있는지 확인하세요. 웹페이지 없이 계속합니다.", file=sys.stderr)
    levels = sorted(int(x) for x in args.augment_levels.split(",") if x.strip())
    game = None      # 진행 중인 게임 상태
    waiting = False
    latest = {}      # 웹페이지 증강 비교용 최신 게임 데이터

    def ranker(names):
        ctx = context(gd, *latest["units"], args.role, args.damage, use_opgg)
        return rank_choices(ctx, names)[1]

    if board:
        board.ranker = ranker
    reader = start_screen_reader(args, gd, latest, board, use_opgg) if args.screen else None

    while True:
        if reader:
            reader["active"] = False
        try:
            data = read_game(args)
            me, allies, enemies, info = live.parse_game(gd, data)
        except (OSError, ValueError) as e:
            if game:
                print("\n게임이 끝났습니다. 다음 게임을 기다립니다.")
                game = None
            if not args.watch:
                print(f"게임에 연결하지 못했습니다 ({e}). 게임이 진행 중인지 확인하세요.", file=sys.stderr)
                return 1
            if not waiting:
                print("게임을 기다리는 중... 롤 게임이 시작되면 자동으로 분석합니다. (끝내려면 Ctrl+C)")
                waiting = True
            if board:
                board.update("게임을 기다리는 중")
            # 게임 밖에서는 자주 확인할 필요가 없다
            time.sleep(max(args.watch, 5))
            continue
        waiting = False
        latest["units"] = (me, allies, enemies)
        if reader:
            # 증강은 죽어서 우물에 있을 때(첫 증강은 게임 시작 직후)만 고를 수 있으므로 그때만 화면을 읽는다
            reader["active"] = info["dead"] or info["time"] < START_SHOPPING_SECONDS

        # 새 게임: 처음 연결했거나, 챔피언이 바뀌었거나, 게임 시간이 되돌아갔을 때
        if (game is None or game["champion"] != me.champion["id"]
                or info["time"] + 5 < game["time"]):
            record = args.watch and not args.file and not args.no_history
            game = {"champion": me.champion["id"], "time": info["time"], "first": True,
                    "prev_dead": False, "last_key": None, "level": me.level, "augment_ready": False,
                    "log": history.GameLog(me.champion, time.time() - info["time"]) if record else None}
            if args.watch and not args.always:
                # 죽었을 때 바로 결과가 나오도록 OP.GG 데이터를 미리 받아 캐시에 둔다
                fetch_opgg(me.champion, use_opgg)
                print(f"{me.champion['name']} 감시 중: 죽거나 게임 시작 직후에 추천을 보여줍니다.")
            if board:
                board.view = None  # 이전 게임 추천은 지운다
                board.augment_names = augment_names({"augments": fetch_opgg(me.champion, use_opgg)[2]})
        game["time"] = info["time"]

        key = [(u.champion["id"], u.level, tuple(u.item_ids)) for u in [me] + allies + enemies]
        changed = key != game["last_key"]
        game["last_key"] = key

        # 증강 레벨에 도달하면 다음에 죽었을 때 고를 수 있다
        reached = [lv for lv in levels if game["level"] < lv <= me.level]
        if game["first"] and me.level in levels:
            reached = [me.level]
        if reached:
            game["augment_ready"] = True
            if not game["first"] and not info["dead"]:
                print(f"\n[알림] 레벨 {me.level}: 증강을 고를 수 있습니다 (죽어서 우물에 돌아가면 선택).")
                if args.speak:
                    notify.speak("증강 선택 가능. 다음 부활 때 고르세요.")
        game["level"] = me.level

        minutes, seconds = divmod(int(info["time"]), 60)
        status = f"사망 (부활까지 {info['respawn']:.0f}초)" if info["dead"] else "생존"
        if board:
            board.update(f"{me.champion['name']} / {minutes}:{seconds:02d} / {status}", dead=info["dead"])
        if game["log"]:
            game["log"].update([gd.items[i]["name"] for i in me.item_ids], info["result"])

        if not args.watch or should_report(info, game["prev_dead"], game["first"], args.always, changed):
            augment_first = game["augment_ready"] and (info["dead"] or game["first"])
            ctx = context(gd, me, allies, enemies, args.role, args.damage, use_opgg)
            text, summary = report(ctx, args.top, info["gold"], augment_first)
            header = f"\n######## 게임 모드 {info['mode']} / {minutes}:{seconds:02d} / {status} ########"
            print("\n".join([header] + team_lines(gd, allies, enemies)))
            print(text)
            if board:
                board.update(board.status, f"{minutes}:{seconds:02d} 기준 추천\n\n{text}", dead=info["dead"],
                             view={**summary["view"], "time": f"{minutes}:{seconds:02d}"})
            if args.speak:
                notify.speak(notify.speech_text(summary))
            if game["log"]:
                game["log"].report(info["time"], [gd.items[i]["name"] for i in me.item_ids],
                                   summary["items"][:3], ctx["role"])
            if augment_first:
                game["augment_ready"] = False
        game["prev_dead"] = info["dead"]
        game["first"] = False
        if not args.watch:
            return 0
        time.sleep(args.watch)


def cmd_pick(args):
    """증강 선택 화면의 선택지를 비교한다."""
    gd = ddragon.load()
    try:
        me, allies, enemies, _ = live.parse_game(gd, read_game(args))
    except OSError as e:
        print(f"게임에 연결하지 못했습니다 ({e}). 게임이 진행 중인지 확인하세요.", file=sys.stderr)
        return 1
    names = [n.strip() for arg in args.names for n in arg.split(",") if n.strip()]
    ctx = context(gd, me, allies, enemies, args.role, args.damage, not args.no_opgg)
    print(rank_choices(ctx, names)[0])
    return 0


def cmd_history(args):
    games = history.load_games()
    if not games:
        print(f"기록이 없습니다. 게임 중 자동 분석(--watch)을 실행하면 {history.HISTORY_DIR}에 저장됩니다.")
        return 0
    print(f"최근 게임 기록 (전체 {len(games)}판)")
    print("\n".join(history.summary(games, args.last)))
    return 0


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

    def common(p):
        p.add_argument("--role", choices=["auto", "tank", "dealer", "bruiser", "enchanter"], default="auto",
                       help="내 역할 (기본: 챔피언 분류로 자동 판단, enchanter는 회복·보호막 서포터)")
        p.add_argument("--damage", choices=["auto", "ad", "ap"], default="auto", help="내 피해 유형")
        p.add_argument("--no-opgg", action="store_true", help="OP.GG 데이터 없이 계산")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=2999)
        p.add_argument("--file", help="API 대신 저장된 allgamedata JSON 파일 사용 (테스트용)")

    lv = sub.add_parser("live", help="게임 중 Live Client Data API로 자동 분석")
    common(lv)
    lv.add_argument("--top", type=int, default=6, help="추천 개수")
    lv.add_argument("--watch", type=int, default=0,
                    help="N초마다 확인하고 죽었을 때 추천 (0이면 한 번만 바로 계산). 권장: 2")
    lv.add_argument("--always", action="store_true",
                    help="죽었을 때만이 아니라 아이템·레벨이 바뀔 때마다 계산")
    lv.add_argument("--speak", action="store_true",
                    help="추천을 음성으로 읽어줌 (전체 화면 게임 중에도 사용 가능, Windows 음성 합성)")
    lv.add_argument("--serve", type=int, metavar="PORT",
                    help="휴대폰·보조 모니터에서 볼 수 있는 웹페이지를 이 포트로 띄움 (예: 8765)")
    lv.add_argument("--augment-levels", default=",".join(map(str, AUGMENT_LEVELS)),
                    help="증강을 고르는 레벨 (기본: 3,7,11,15)")
    lv.add_argument("--no-history", action="store_true", help="게임 기록을 저장하지 않음")
    lv.add_argument("--screen", action="store_true",
                    help="증강 선택 화면을 읽어 선택지를 자동으로 비교 (Windows OCR, 테두리 없는 창 모드 권장)")
    lv.add_argument("--screen-monitor", type=int, default=-1, help="읽을 모니터 번호 (기본: 주 모니터)")
    lv.add_argument("--screen-image", help="화면 대신 이 이미지를 읽음 (테스트용)")
    lv.set_defaults(func=cmd_live)

    pk = sub.add_parser("pick", help="증강 선택지 비교 (예: pick 둠 핀볼 \"남작의 도움\")")
    common(pk)
    pk.add_argument("names", nargs="+", help="증강 이름 (띄어쓰기가 있으면 따옴표, 쉼표로 구분해도 됨)")
    pk.set_defaults(func=cmd_pick)

    hs = sub.add_parser("history", help="게임 기록과 추천을 따랐을 때의 승률")
    hs.add_argument("--last", type=int, default=20, help="보여줄 최근 게임 수")
    hs.set_defaults(func=cmd_history)

    ef = sub.add_parser("efficiency", help="아이템 골드 효율표")
    ef.add_argument("--top", type=int, default=30)
    ef.set_defaults(func=cmd_efficiency)

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n종료합니다.")
        return 0
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
