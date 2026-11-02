"""게임 기록: 추천 내용과 실제 구매, 승패를 저장하고 추천을 따랐을 때의 승률을 본다.

기록은 data/history/에 게임마다 JSON 파일 하나로 저장한다.
"""

import json
import time

from .ddragon import DATA_DIR

HISTORY_DIR = DATA_DIR / "history"
RESULT_LABEL = {"Win": "승리", "Lose": "패배"}


class GameLog:
    def __init__(self, champion, start):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
        self.path = HISTORY_DIR / f"{stamp}_{champion['id']}.json"
        self.data = {"champion": champion["name"], "start": start, "result": None,
                     "reports": [], "final_items": []}

    def report(self, game_time, items, recommended, role):
        self.data["role"] = role
        self.data["reports"].append({"time": round(game_time), "items": items,
                                     "recommended": recommended})
        self.save()

    def update(self, items, result):
        """매 확인마다 호출. 바뀐 것이 있을 때만 저장한다."""
        if items == self.data["final_items"] and (result or None) == self.data["result"]:
            return
        self.data["final_items"] = items
        self.data["result"] = result or self.data["result"]
        self.save()

    def save(self):
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")


def follow_rate(game):
    """추천 1순위 아이템을 실제로 산 비율. 추천은 보유하지 않은 아이템만 고르므로
    이후에 가지고 있었으면 추천을 따른 것으로 본다."""
    recs = [r["recommended"][0] for r in game["reports"] if r["recommended"]]
    if not recs:
        return None
    bought = set(game["final_items"])
    for r in game["reports"]:
        bought |= set(r["items"])
    return sum(r in bought for r in recs) / len(recs)


def load_games():
    if not HISTORY_DIR.exists():
        return []
    games = []
    for path in sorted(HISTORY_DIR.glob("*.json")):
        try:
            games.append(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            continue
    return games


def summary(games, last=20):
    lines = []
    for g in games[-last:]:
        rate = follow_rate(g)
        follow = f"추천 따름 {rate:.0%}" if rate is not None else "추천 없음"
        when = time.strftime("%m/%d %H:%M", time.localtime(g["start"]))
        lines.append(f"  {when}  {g['champion']:<8} {RESULT_LABEL.get(g['result'], '결과 모름'):<6} "
                     f"{follow}  최종: {', '.join(g['final_items']) or '-'}")

    def win_rate(group):
        done = [g for g in group if g["result"] in RESULT_LABEL]
        if not done:
            return "기록 없음"
        wins = sum(g["result"] == "Win" for g in done)
        return f"{wins / len(done):.0%} ({wins}승 {len(done) - wins}패)"

    rated = [g for g in games if follow_rate(g) is not None]
    lines.append("")
    lines.append(f"추천을 절반 이상 따른 게임 승률: {win_rate([g for g in rated if follow_rate(g) >= 0.5])}")
    lines.append(f"추천을 절반 미만 따른 게임 승률: {win_rate([g for g in rated if follow_rate(g) < 0.5])}")
    lines.append(f"전체 승률: {win_rate(games)}")
    return lines
