from __future__ import annotations

import subprocess
import threading
import time

from .config import CameraConfig


class PersistentRtspFeed:
    """Maintain one configured RTSP connection and cache the newest JPEG frame."""

    MAX_CONSECUTIVE_FAILURES = 5
    MAX_RETRY_SECONDS = 60.0
    STABLE_RUN_SECONDS = 30.0

    def __init__(self, camera: CameraConfig, log):
        self.camera = camera
        self.log = log
        feed = camera.feed
        if feed.source not in {"restream", "direct_rtsp"}:
            raise ValueError(
                f"{camera.camera_id}: persistent RTSP feed requires source=restream or direct_rtsp"
            )
        if not feed.url:
            raise ValueError(f"{camera.camera_id}: feed URL is required")

        self.source = feed.source
        self.url = feed.url
        self.transport = feed.transport
        self.ffmpeg = feed.ffmpeg_binary
        self.start_timeout = feed.start_timeout
        self.stop_timeout = feed.stop_timeout
        self.retry_seconds = feed.retry_seconds
        self.fps = feed.fps
        self.width = feed.width

        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._generation = 0
        self._enabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._restart_count = 0
        self._consecutive_failures = 0
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._last_frame_at: float | None = None

    def _command(self) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            self.transport,
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
            self._generation = 0
            self._restart_count = 0
            self._consecutive_failures = 0
            self._last_exit_code = None
            self._last_error = None
            self._last_frame_at = None
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"rtsp-feed-{self.camera.camera_id}",
                daemon=True,
            )
            self._thread.start()
        self.log("INFO", f"Persistent RTSP feed starting source={self.source}")

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
            frame_age = (
                max(0.0, time.monotonic() - self._last_frame_at)
                if self._last_frame_at is not None
                else None
            )
            return {
                "source": self.source,
                "running": bool(
                    self._enabled and process is not None and process.poll() is None
                ),
                "frame_ready": self._latest is not None,
                "generation": self._generation,
                "restart_count": self._restart_count,
                "consecutive_failures": self._consecutive_failures,
                "last_exit_code": self._last_exit_code,
                "last_error": self._last_error,
                "frame_age_seconds": round(frame_age, 3) if frame_age is not None else None,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            frames_before = self._generation
            error: Exception | None = None

            try:
                self._capture_once()
            except FileNotFoundError:
                self._last_error = f"ffmpeg binary not found: {self.ffmpeg}"
                self.log("ERROR", self._last_error)
                break
            except Exception as exc:
                error = exc
                self._last_error = f"{type(exc).__name__}: {exc}"
                if not self._stop_event.is_set():
                    self.log("ERROR", f"Persistent RTSP feed failed: {self._last_error}")

            if self._stop_event.is_set():
                break

            runtime = time.monotonic() - started
            produced_frames = self._generation > frames_before
            stable = produced_frames and runtime >= self.STABLE_RUN_SECONDS
            if stable:
                self._consecutive_failures = 0
                self._last_error = None
            else:
                self._consecutive_failures += 1
                if error is None and self._last_error is None:
                    self._last_error = (
                        f"ffmpeg exited after {runtime:.1f}s without a stable run"
                    )

            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                with self._condition:
                    self._enabled = False
                    self._latest = None
                    self._condition.notify_all()
                self.log(
                    "ERROR",
                    "Persistent RTSP feed disabled after "
                    f"{self._consecutive_failures} consecutive failures",
                )
                break

            delay = min(
                self.MAX_RETRY_SECONDS,
                self.retry_seconds * (2 ** max(0, self._consecutive_failures - 1)),
            )
            self._restart_count += 1
            if not self._stop_event.wait(delay):
                self.log(
                    "WARN",
                    "Restarting persistent RTSP feed "
                    f"after {delay:g}s restart={self._restart_count} "
                    f"failures={self._consecutive_failures}",
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
        self.log(
            "INFO",
            f"Persistent RTSP ffmpeg started pid={process.pid} source={self.source}",
        )

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
                    self._last_frame_at = time.monotonic()
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
            self._last_exit_code = code
            self._latest = None
            if self._process is process:
                self._process = None
            self._condition.notify_all()
        if not self._stop_event.is_set() and code not in (0, 255):
            raise RuntimeError(f"ffmpeg exited with status {code}")

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text and not self._stop_event.is_set():
                self.log("WARN", f"ffmpeg: {text}")
