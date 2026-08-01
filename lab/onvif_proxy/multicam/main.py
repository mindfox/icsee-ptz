from __future__ import annotations

import argparse
import signal
import threading

from . import runtime as runtime_module
from .config import load_config
from .dashboard import HTML
from .runtime import CameraRuntime, start_web


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/cameras.yaml")
    parser.add_argument("--proxy-script", default="/app/proxy.py")
    args = parser.parse_args()

    runtime_module.HTML = HTML

    runtime = CameraRuntime(load_config(args.config), args.proxy_script)
    runtime.start()
    web = start_web(runtime)
    done = threading.Event()

    def stop(*_args) -> None:
        done.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    done.wait()
    web.shutdown()
    web.server_close()
    runtime.stop()


if __name__ == "__main__":
    main()
