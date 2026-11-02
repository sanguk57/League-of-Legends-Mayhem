"""증강 선택 화면을 읽어 선택지를 찾는다.

화면 캡처와 글자 인식은 Windows 기본 OCR(ocr.ps1)로 한다. 게임 메모리를 읽거나 게임 화면에 그리지 않는다.
카드 위치를 고정하지 않고, 화면 가운데에서 읽은 줄 중 증강 이름과 비슷한 것을 찾는다.
그래서 해상도나 창 크기가 달라도 따로 맞출 필요가 없다.
"""

import difflib
import json
import shutil
import subprocess
from pathlib import Path

from .ddragon import normalize

OCR_SCRIPT = Path(__file__).with_name("ocr.ps1")
# 읽을 영역 (화면 대비 왼쪽, 위, 오른쪽, 아래). 1920x1080 전체 화면 기준 증강 카드 3장은
# 가로 23~83%, 세로 18~67%에 있고 이름 줄은 41% 높이다. UI 크기 설정 차이를 감안해 여유를 둔다.
CARD_REGION = (0.18, 0.12, 0.86, 0.72)
FULL_REGION = (0.0, 0.0, 1.0, 1.0)
# OCR이 한두 글자 틀려도 같은 증강으로 본다 (예: "지옥의 전도세" -> "지옥의 전도체")
MIN_RATIO = 0.75
# 카드 이름은 같은 높이에 나란히 있다 (화면 높이 대비)
SAME_ROW = 0.03


class OcrError(RuntimeError):
    pass


def _powershell():
    return shutil.which("powershell.exe") or shutil.which("powershell")


def ocr(path=None, monitor=-1, region=CARD_REGION, timeout=20):
    """화면(path가 없으면 캡처) 가운데 영역의 글자 줄 목록. 좌표는 영역 기준 0~1."""
    exe = _powershell()
    if exe is None:
        raise OcrError("PowerShell을 찾지 못했습니다 (Windows에서만 화면 읽기 가능)")
    left, top, right, bottom = region
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(OCR_SCRIPT),
           "-Monitor", str(monitor), "-Left", str(left), "-Top", str(top), "-Right", str(right),
           "-Bottom", str(bottom)]
    if path:
        cmd += ["-Path", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrError(f"화면을 읽지 못했습니다: {e}") from e
    out = proc.stdout.decode("utf-8", "replace")
    start = out.find("{")
    if proc.returncode != 0 or start < 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or out.strip()
        raise OcrError(f"화면을 읽지 못했습니다: {err[:300]}")
    return json.loads(out[start:]).get("lines", [])


def _best_name(text, names):
    """text와 가장 비슷한 증강 이름과 유사도."""
    key = normalize(text)
    if len(key) < 2:
        return None, 0.0
    best, best_ratio = None, 0.0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(key)
    for name, norm in names:
        matcher.set_seq1(norm)
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = name, ratio
    return best, best_ratio


def match_choices(lines, names, min_ratio=MIN_RATIO):
    """OCR 줄 목록에서 증강 선택지(최대 3개, 왼쪽부터)를 찾는다."""
    indexed = [(n, normalize(n)) for n in names]
    found = []
    for line in lines:
        name, ratio = _best_name(line.get("text", ""), indexed)
        if name and ratio >= min_ratio:
            found.append({"name": name, "ratio": ratio, "x": line["x"] + line["w"] / 2, "y": line["y"]})
    if not found:
        return []
    # 카드 이름이 나란히 있는 줄: 같은 높이에 가장 많이 모인 무리
    rows = []
    for f in sorted(found, key=lambda f: f["y"]):
        if rows and f["y"] - rows[-1][0]["y"] <= SAME_ROW:
            rows[-1].append(f)
        else:
            rows.append([f])
    row = max(rows, key=lambda r: (len(r), sum(f["ratio"] for f in r)))
    # 같은 카드에서 두 번 잡힌 경우 유사도가 높은 쪽만 남긴다
    row.sort(key=lambda f: f["x"])
    picked = []
    for f in row:
        if picked and f["x"] - picked[-1]["x"] < 0.08:
            if f["ratio"] > picked[-1]["ratio"]:
                picked[-1] = f
            continue
        picked.append(f)
    return [f["name"] for f in picked[:3]]


class ChoiceTracker:
    """연속으로 읽은 선택지에서 리롤한 칸을 알아낸다.
    한 칸만 이름이 바뀌면 그 칸을 리롤한 것으로 본다. 세 칸이 모두 바뀌면 새 선택 화면이다."""

    def __init__(self, forget_after=3):
        self.names = []
        self.rerolled = []
        self.misses = 0
        self.forget_after = forget_after

    def update(self, names):
        """새로 읽은 선택지. 바뀐 것이 있으면 True."""
        if len(names) < 2:
            # 선택 화면이 닫혔거나 못 읽음: 몇 번 연속이면 잊는다
            self.misses += 1
            if self.misses >= self.forget_after and self.names:
                self.names, self.rerolled = [], []
            return False
        self.misses = 0
        if names == self.names:
            return False
        if len(names) == len(self.names):
            changed = [a != b for a, b in zip(names, self.names)]
            if all(changed):
                self.rerolled = [False] * len(names)
            else:
                self.rerolled = [r or c for r, c in zip(self.rerolled, changed)]
        else:
            self.rerolled = [False] * len(names)
        self.names = list(names)
        return True

    def query(self):
        """rank_choices에 넘길 이름 (리롤한 칸은 끝에 *)."""
        return [n + ("*" if r else "") for n, r in zip(self.names, self.rerolled)]
