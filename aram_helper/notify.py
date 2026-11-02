"""전체 화면 게임 중에도 추천을 전달하는 방법: 음성 안내, 휴대폰·보조 모니터용 웹페이지.

게임 화면 위에 직접 그리는 방식(그래픽 후킹)은 안티치트(Vanguard) 제재 위험이 있어 쓰지 않는다.
"""

import base64
import json
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Windows 음성 합성(System.Speech). 한국어 음성이 있으면 그것을 쓴다.
_SPEAK_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$v = $s.GetInstalledVoices() | ? {{ $_.VoiceInfo.Culture.Name -eq 'ko-KR' }} | select -First 1;"
    "if ($v) {{ $s.SelectVoice($v.VoiceInfo.Name) }};"
    "$s.Rate = {rate};"
    "$s.Speak([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{text}')))"
)


def _powershell():
    return shutil.which("powershell.exe") or shutil.which("powershell")


def speak(text, rate=1):
    """비동기로 읽어준다 (게임 감시를 멈추지 않도록)."""
    exe = _powershell()
    if exe is None:
        print("[경고] PowerShell을 찾지 못해 음성 안내를 할 수 없습니다 (Windows 또는 WSL에서만 가능).",
              file=sys.stderr)
        return
    encoded = base64.b64encode(text.encode("utf-8")).decode()
    script = _SPEAK_SCRIPT.format(rate=int(rate), text=encoded)
    subprocess.Popen([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speech_text(summary, items=2, augments=2):
    parts = []
    if summary.get("buy_now"):
        parts.append("지금 구매 " + ", ".join(summary["buy_now"]))
    if summary.get("swap"):
        parts.append(summary["swap"])
    if summary.get("boots"):
        parts.append(f"신발 {summary['boots']}")
    if summary["items"]:
        parts.append("아이템 " + ", ".join(summary["items"][:items]))
    if summary["augments"]:
        parts.append("증강 " + ", ".join(summary["augments"][:augments]))
    return ". ".join(parts) + "." if parts else "추천할 아이템이 없습니다."


# 추천 웹페이지: web/index.html이 /data(JSON)를 3초마다 받아 화면을 다시 그린다
_INDEX = Path(__file__).with_name("web") / "index.html"


class WebBoard:
    """최신 추천을 보여주는 웹페이지.

    /       페이지 (web/index.html)
    /data   상태와 추천 결과 JSON
    /names  증강 이름 목록 (입력 자동 완성)
    /rank   ?c=증강&c=증강&c=증강 선택지 비교 결과 JSON
    """

    def __init__(self, port):
        self.status = "게임을 기다리는 중"
        self.dead = False
        self.body = "아직 추천이 없습니다. 죽거나 게임이 시작되면 표시됩니다."
        self.view = None
        # 증강 비교: ranker(이름 목록) -> 결과 행 목록, augment_names: 입력 자동 완성 목록
        self.ranker = None
        self.augment_names = []
        board = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/data":
                    self._send(json.dumps({"status": board.status, "dead": board.dead, "view": board.view},
                                          ensure_ascii=False), "application/json")
                elif url.path == "/names":
                    self._send(json.dumps(board.augment_names, ensure_ascii=False), "application/json")
                elif url.path == "/rank":
                    result = board.rank(parse_qs(url.query).get("c", []))
                    self._send(json.dumps(result, ensure_ascii=False), "application/json")
                elif url.path in ("/", "/index.html"):
                    self._send(_INDEX.read_text("utf-8"), "text/html")
                else:
                    self.send_error(404)

            def _send(self, text, content_type):
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def rank(self, choices):
        choices = [c.strip() for c in choices if c.strip()][:3]
        if not choices:
            return {"error": "증강 선택 화면에 나온 증강 이름을 입력하세요."}
        if self.ranker is None:
            return {"error": "아직 게임 데이터가 없습니다. 게임이 시작된 뒤 다시 시도하세요."}
        try:
            return {"rows": self.ranker(choices)}
        except Exception as e:  # 웹페이지가 죽지 않도록 오류를 그대로 보여준다
            return {"error": f"비교하지 못했습니다: {e}"}

    def update(self, status, body=None, dead=False, view=None):
        self.status = status
        self.dead = dead
        if body is not None:
            self.body = body
        if view is not None:
            self.view = view

    def urls(self):
        port = self.server.server_address[1]
        urls = [f"http://localhost:{port}"]
        try:
            # 외부로 나가는 경로의 내 IP (실제로 패킷을 보내지는 않는다)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                urls.append(f"http://{s.getsockname()[0]}:{port}")
        except OSError:
            pass
        return urls
