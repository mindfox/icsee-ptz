import asyncio
import importlib.util
from pathlib import Path


class CameraClient:
    def __init__(self, module_path: Path, host: str, port: int, username: str, password: str):
        spec = importlib.util.spec_from_file_location("icsee_asyncio_dvrip", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load DVRIP module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.camera_type = module.DVRIPCam
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    async def _snapshot(self):
        camera = self.camera_type(self.host, port=self.port, user=self.username, password=self.password)
        try:
            if not await camera.login(asyncio.get_running_loop()):
                raise RuntimeError("camera login failed")
            return await camera.snapshot(channel=0)
        finally:
            camera.close()

    def snapshot(self) -> bytes:
        result = asyncio.run(self._snapshot())
        if not result:
            raise RuntimeError("camera returned no snapshot")
        return bytes(result)
