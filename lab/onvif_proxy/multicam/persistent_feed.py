from __future__ import annotations

import subprocess
import threading
import time
from urllib.parse import quote

from .config import CameraConfig


class PersistentRtspFeed:
    """Maintain one RTSP connection and cache the newest complete JPEG frame."""

    def __init__(self, camera: CameraConfig, log):
        self.camera = camera
        self.log = log
        self.ffmpeg = str(camera.options.get("ffmpeg_binary", "ffmpeg"))
        self.start_timeout = float(camera.options.get("feed_start_timeout", 15))
        self.stop_timeout = float(camera.options.get("feed_stop_timeout", 5))
        self.retry_seconds = float(camera.options.get("feed_retry_seconds", 5))
        self.fps = float(camera.options.get("feed_fps", 1))
        self.width = int(camera.options.get("feed_width", 960))
        if not 0.1 <= self.fps <= 10:
            raise ValueError(f"{camera.camera_id}: feed_fps must be between 0.1 and 10")
        if not 160 <= self.width <= 3840:
            raise ValueError(f"{camera.camera_id}: feed_width must be between 160 and 3840")

        port = int(camera.options.get("rtsp_port", 554))
        path = str(camera.options.get("main_stream", "/stream1"))
        if not path.startswith("/"):
            path = "/" + path
        username = quote(camera.username or "", safe="")
        password = quote(camera.password or "", safe="")
        auth = f"{username}:{password}@" if username or password else ""
        self.url = f"rtsp://{auth}{camera.host}:{port}{path}"

        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._generation = 0
        self._enabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None

    def _command(self) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.url,
            "-an",
            "-vf",
            f"fps={self.fps:g},scale={self.width}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
        ]

    def start(self) -> None:
        with self._condition:
            if self._enabled:
                return
            self._enabled = True
            self._latest = None
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"rtsp-feed-{self.camera.camera_id}",
                daemon=True,
            )
            self._thread.start()
        self.log("INFO", "Persistent RTSP feed starting")

    def stop(self) -> None:
        with self._condition:
            if not self._enabled and self._thread is None:
                return
            self._enabled = False
            self._stop_event.set()
            process = self._process
            thread = self._thread
            self._condition.notify_all()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.stop_timeout + 1)
        with self._condition:
            self._process = None
            self._thread = None
            self._latest = None
            self._condition.notify_all()
        self.log("INFO", "Persistent RTSP feed stopped")

    def snapshot(self) -> bytes:
        deadline = time.monotonic() + self.start_timeout
        with self._condition:
            while self._enabled and self._latest is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"persistent RTSP feed produced no frame within {self.start_timeout:g}s"
                    )
                self._condition.wait(remaining)
            if not self._enabled:
                raise RuntimeError("live feed is disabled")
            if self._latest is None:
                raise RuntimeError("persistent RTSP feed has no cached frame")
            return self._latest

    def status(self) -> dict:
        with self._condition:
            process = self._process
            return {
                "running": bool(
                    self._enabled and process is not None and process.poll() is None
                ),
                "frame_ready": self._latest is not None,
                "generation": self._generation,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._capture_once()
            except FileNotFoundError:
                self.log("ERROR", f"ffmpeg binary not found: {self.ffmpeg}")
                break
            except Exception as exc:
                if not self._stop_event.is_set():
                    self.log(
                        "ERROR",
                        f"Persistent RTSP feed failed: {type(exc).__name__}: {exc}",
                    )
            if not self._stop_event.wait(self.retry_seconds):
                self.log(
                    "WARN",
                    f"Restarting persistent RTSP feed after {self.retry_seconds:g}s",
                )
        with self._condition:
            self._process = None
            self._condition.notify_all()

    def _capture_once(self) -> None:
        process = subprocess.Popen(
            self._command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        with self._condition:
            self._process = process
        self.log("INFO", f"Persistent RTSP ffmpeg started pid={process.pid}")

        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            daemon=True,
        )
        stderr_thread.start()

        if process.stdout is None:
            raise RuntimeError("ffmpeg stdout pipe is unavailable")

        buffer = bytearray()
        while not self._stop_event.is_set():
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                with self._condition:
                    self._latest = frame
                    self._generation += 1
                    self._condition.notify_all()

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        code = process.returncode
        stderr_thread.join(1)
        with self._condition:
            if self._process is process:
                self._process = None
        if not self._stop_event.is_set() and code not in (0, 255):
            raise RuntimeError(f"ffmpeg exited with status {code}")

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text and not self._stop_event.is_set():
                self.log("WARN", f"ffmpeg: {text}")
