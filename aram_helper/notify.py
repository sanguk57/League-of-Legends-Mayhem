"""전체 화면 게임 중에도 추천을 전달하는 방법: 음성 안내, 휴대폰·보조 모니터용 웹페이지.

게임 화면 위에 직접 그리는 방식(그래픽 후킹)은 안티치트(Vanguard) 제재 위험이 있어 쓰지 않는다.
"""

import base64
import html
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    if summary.get("boots"):
        parts.append(f"신발 {summary['boots']}")
    if summary["items"]:
        parts.append("아이템 " + ", ".join(summary["items"][:items]))
    if summary["augments"]:
        parts.append("증강 " + ", ".join(summary["augments"][:augments]))
    return ". ".join(parts) + "." if parts else "추천할 아이템이 없습니다."


_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>칼바람 추천</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 16px; background: #111418; color: #e6e6e6;
         font-family: system-ui, "Malgun Gothic", sans-serif; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .status {{ color: #9aa4ad; font-size: 14px; margin-bottom: 12px; }}
  .dead {{ color: #ff7b72; }}
  pre {{ white-space: pre-wrap; word-break: break-all; font-size: 14px; line-height: 1.5;
        background: #1a1f25; padding: 12px; border-radius: 8px; }}
</style></head>
<body><h1>칼바람 아이템·증강 추천</h1>
<div class="status {status_class}">{status}</div>
<pre>{body}</pre></body></html>"""


class WebBoard:
    """최신 추천을 보여주는 웹페이지 (3초마다 자동 새로고침)."""

    def __init__(self, port):
        self.status = "게임을 기다리는 중"
        self.dead = False
        self.body = "아직 추천이 없습니다. 죽거나 게임이 시작되면 표시됩니다."
        board = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                page = _PAGE.format(status=html.escape(board.status), body=html.escape(board.body),
                                    status_class="dead" if board.dead else "")
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def update(self, status, body=None, dead=False):
        self.status = status
        self.dead = dead
        if body is not None:
            self.body = body

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
