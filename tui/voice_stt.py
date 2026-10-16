"""Grok Build streaming STT for the asdaaas TUI.

Wire matches grok-build `xai-grok-voice`: PCM16 mono at 16 kHz over
`wss://api.x.ai/v1/stt`, Linux capture via pw-record / parec / arecord.
Dictation only — transcript is inserted into the composer, not sent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

SAMPLE_RATE = 16_000
STT_WS_PATH = "/v1/stt"
DEFAULT_API_BASE = "https://api.x.ai"
AUTH_PATH = Path.home() / ".grok" / "auth.json"

OnPartial = Callable[[str], None]
OnDone = Callable[[str], None]
OnError = Callable[[str], None]
OnStatus = Callable[[str], None]


class VoiceError(Exception):
    pass


def build_stt_ws_url(
    api_base: str = DEFAULT_API_BASE,
    *,
    sample_rate: int = SAMPLE_RATE,
    language: str = "en",
    endpointing_ms: int = 400,
    interim_results: bool = True,
) -> str:
    """HTTPS api_base → wss://…/v1/stt with grok-build query params."""
    base = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
    lower = base.lower()
    if lower.startswith("http://") or lower.startswith("ws://"):
        raise VoiceError(f"insecure voice api_base {api_base!r}: need https/wss")
    if lower.startswith("https://"):
        rest = base[8:]
    elif lower.startswith("wss://"):
        rest = base[6:]
    else:
        rest = base
    path = STT_WS_PATH.lstrip("/")
    if rest.endswith("/v1") and path.startswith("v1/"):
        path = path[3:]
    if language.strip().lower() in ("", "auto"):
        language = "en"
    query = urlencode(
        {
            "sample_rate": str(sample_rate),
            "encoding": "pcm",
            "interim_results": "true" if interim_results else "false",
            "language": language,
            "endpointing": str(endpointing_ms),
        }
    )
    return f"wss://{rest}/{path}?{query}"


def resolve_bearer() -> str:
    """XAI_API_KEY, else ~/.grok/auth.json `key` (OAuth access token)."""
    env = (os.environ.get("XAI_API_KEY") or "").strip()
    if env:
        return env
    try:
        data = json.loads(AUTH_PATH.read_text())
    except FileNotFoundError as e:
        raise VoiceError(
            "not signed in — run `grok login` or set XAI_API_KEY"
        ) from e
    except Exception as e:
        raise VoiceError(f"could not read grok auth: {e}") from e
    if isinstance(data, dict):
        for _k, rec in data.items():
            if isinstance(rec, dict):
                key = (rec.get("key") or rec.get("access_token") or "").strip()
                if key:
                    return key
            elif isinstance(rec, str) and rec.strip():
                return rec.strip()
    raise VoiceError("not signed in — run `grok login` or set XAI_API_KEY")


def recorder_candidates(rate: int = SAMPLE_RATE) -> list[tuple[str, list[str]]]:
    """Same order as grok-build Linux capture: pw-record, parec, arecord."""
    rate_s = str(rate)
    out: list[tuple[str, list[str]]] = []
    if shutil.which("pw-record"):
        out.append(
            (
                "pw-record",
                [
                    "--raw",
                    "--rate",
                    rate_s,
                    "--channels",
                    "1",
                    "--format",
                    "s16",
                    "-",
                ],
            )
        )
    if shutil.which("parec"):
        out.append(
            (
                "parec",
                [
                    "--raw",
                    "--format=s16le",
                    f"--rate={rate_s}",
                    "--channels=1",
                ],
            )
        )
    if shutil.which("arecord"):
        out.append(
            (
                "arecord",
                [
                    "-q",
                    "-t",
                    "raw",
                    "-f",
                    "S16_LE",
                    "-c",
                    "1",
                    "-r",
                    rate_s,
                    "-",
                ],
            )
        )
    return out


def spawn_recorder(rate: int = SAMPLE_RATE) -> subprocess.Popen:
    last_err = "no recorder (install pipewire-pulse / pulseaudio-utils / alsa-utils)"
    for prog, args in recorder_candidates(rate):
        try:
            proc = subprocess.Popen(
                [prog, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            last_err = f"{prog}: {e}"
            continue
        deadline = time.time() + 0.3
        while time.time() < deadline:
            if proc.poll() is not None:
                last_err = f"{prog} exited {proc.returncode}"
                break
            time.sleep(0.015)
        else:
            return proc
        try:
            proc.kill()
        except OSError:
            pass
    raise VoiceError(f"could not start a microphone recorder: {last_err}")


def merge_voice_fragment(existing: str, fragment: str) -> str:
    fragment = (fragment or "").strip()
    if not fragment:
        return existing
    if not existing:
        return fragment
    if existing.endswith((" ", "\n", "\t")):
        return existing + fragment
    return existing + " " + fragment


class VoiceSession:
    """One dictation clip. Threaded; callbacks may arrive off the UI thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finish = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.listening = False

    def start(
        self,
        *,
        on_partial: OnPartial,
        on_done: OnDone,
        on_error: OnError,
        on_status: Optional[OnStatus] = None,
        api_base: str = DEFAULT_API_BASE,
        language: str = "en",
    ) -> None:
        with self._lock:
            if self.listening:
                return
            self._stop.clear()
            self._finish.clear()
            self.listening = True
            self._thread = threading.Thread(
                target=self._run,
                kwargs={
                    "on_partial": on_partial,
                    "on_done": on_done,
                    "on_error": on_error,
                    "on_status": on_status or (lambda _s: None),
                    "api_base": api_base,
                    "language": language,
                },
                name="tui-voice-stt",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """End capture and wait for transcript.done."""
        self._finish.set()
        self._stop.set()
        self._kill_recorder()

    def cancel(self) -> None:
        """Abort; discard the clip."""
        self._stop.set()
        self._finish.clear()
        self._kill_recorder()

    def _kill_recorder(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _run(
        self,
        on_partial: OnPartial,
        on_done: OnDone,
        on_error: OnError,
        on_status: OnStatus,
        api_base: str,
        language: str,
    ) -> None:
        proc = None
        ws = None
        try:
            import websocket

            bearer = resolve_bearer()
            url = build_stt_ws_url(api_base, language=language)
            ws = websocket.create_connection(
                url,
                header=[f"Authorization: Bearer {bearer}"],
                timeout=15,
            )
            ready = False
            deadline = time.time() + 10
            while time.time() < deadline:
                raw = ws.recv()
                if not raw:
                    continue
                ev = json.loads(raw) if isinstance(raw, str) else {}
                if ev.get("type") == "transcript.created":
                    ready = True
                    break
                if ev.get("type") == "error":
                    raise VoiceError(ev.get("message") or "stt error")
            if not ready:
                raise VoiceError("timed out waiting for transcript.created")

            proc = spawn_recorder()
            self._proc = proc
            on_status("listening")
            ws.settimeout(0.05)
            stdout = proc.stdout
            assert stdout is not None
            last_text = ""
            while not self._stop.is_set():
                chunk = stdout.read(4096)
                if chunk:
                    ws.send_binary(chunk)
                elif proc.poll() is not None:
                    break
                try:
                    msg = ws.recv()
                except Exception:
                    msg = None
                if isinstance(msg, str) and msg:
                    last_text = self._handle_event(msg, on_partial, on_error) or last_text
            try:
                ws.send(json.dumps({"type": "audio.done"}))
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
            if not self._finish.is_set():
                # cancel: do not wait for a final
                return
            on_status("transcribing")
            ws.settimeout(8)
            end = time.time() + 8
            while time.time() < end:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if isinstance(msg, str) and msg:
                    got = self._handle_event(msg, on_partial, on_error)
                    if got is not None:
                        last_text = got
                    ev = json.loads(msg)
                    if ev.get("type") in ("transcript.done", "error"):
                        if ev.get("type") == "transcript.done":
                            on_done((ev.get("text") or last_text or "").strip())
                        return
            if last_text.strip():
                on_done(last_text.strip())
        except VoiceError as e:
            on_error(str(e))
        except Exception as e:
            on_error(str(e))
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            with self._lock:
                self.listening = False
            on_status("")

    @staticmethod
    def _handle_event(
        raw: str, on_partial: OnPartial, on_error: OnError
    ) -> Optional[str]:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return None
        kind = ev.get("type")
        if kind == "transcript.partial":
            text = ev.get("text") or ""
            if text:
                on_partial(text)
            return text
        if kind == "transcript.done":
            return ev.get("text") or ""
        if kind == "error":
            on_error(ev.get("message") or "stt error")
        return None
