import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.icsee_ptz.asyncio_dvrip import DVRIPCam


def emit(name: str, status: str, detail: Any = None) -> None:
    record = {"test": name, "status": status}
    if detail is not None:
        record["detail"] = detail
    print(json.dumps(record, default=str, sort_keys=True))


async def main() -> int:
    host = os.environ["CAMERA_HOST"]
    port = int(os.environ.get("CAMERA_PORT", "34567"))
    username = os.environ["CAMERA_USERNAME"]
    password = os.environ["CAMERA_PASSWORD"]

    try:
        with socket.create_connection((host, port), timeout=5):
            pass
        emit("tcp_connect", "passed")
    except Exception as exc:
        emit("tcp_connect", "failed", f"{type(exc).__name__}: {exc}")
        return 1

    camera = DVRIPCam(host, port=port, user=username, password=password)

    try:
        logged_in = await camera.login(asyncio.get_running_loop())
        if not logged_in:
            emit("login", "failed", "camera rejected login")
            return 1
        emit("login", "passed")

        tests = (
            ("system_info", camera.get_system_info),
            ("system_capabilities", camera.get_system_capabilities),
            ("detect", lambda: camera.get_info("Detect")),
        )

        failures = 0
        for name, operation in tests:
            try:
                result = await operation()
                if result is None:
                    emit(name, "no_response")
                    failures += 1
                else:
                    emit(name, "passed", result)
            except Exception as exc:
                emit(name, "failed", f"{type(exc).__name__}: {exc}")
                failures += 1

        return 1 if failures else 0
    finally:
        camera.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyError as exc:
        print(json.dumps({"status": "failed", "detail": f"missing environment variable: {exc.args[0]}"}))
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)
