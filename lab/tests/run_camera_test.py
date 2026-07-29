import asyncio
import importlib.util
import json
import os
import socket
from pathlib import Path
from typing import Any

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "icsee_ptz"
    / "asyncio_dvrip.py"
)
spec = importlib.util.spec_from_file_location("icsee_asyncio_dvrip", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load DVRIP module from {MODULE_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DVRIPCam = module.DVRIPCam


def emit(name: str, status: str, detail: Any = None) -> None:
    record = {"test": name, "status": status}
    if detail is not None:
        record["detail"] = detail
    print(json.dumps(record, default=str, sort_keys=True))


async def diagnostic_login(camera: Any) -> dict[str, Any] | None:
    await camera.connect()
    response = await camera.send(
        1000,
        {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": camera.hash_pass,
            "UserName": camera.user,
        },
    )

    if response is not None and response.get("Ret") in camera.OK_CODES:
        camera.session = int(response["SessionID"], 16)
        camera.alive_time = response["AliveInterval"]
        camera.keep_alive(asyncio.get_running_loop())

    return response


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
        login_response = await diagnostic_login(camera)
        if login_response is None:
            emit("login", "failed", "no response")
            return 1

        login_ret = login_response.get("Ret")
        login_detail = {
            "ret": login_ret,
            "meaning": camera.CODES.get(login_ret, "unknown return code"),
            "response_keys": sorted(login_response.keys()),
        }
        if login_ret not in camera.OK_CODES:
            emit("login", "failed", login_detail)
            return 1
        emit("login", "passed", login_detail)

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
        print(
            json.dumps(
                {
                    "status": "failed",
                    "detail": f"missing environment variable: {exc.args[0]}",
                }
            )
        )
        raise SystemExit(2)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        raise SystemExit(1)
