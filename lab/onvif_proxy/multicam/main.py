from __future__ import annotations
import argparse, signal, threading
from .config import load_config
from .runtime import CameraRuntime, start_web

def main():
    p=argparse.ArgumentParser(); p.add_argument('--config',default='/config/cameras.yaml'); p.add_argument('--proxy-script',default='/app/proxy.py'); a=p.parse_args()
    runtime=CameraRuntime(load_config(a.config),a.proxy_script); runtime.start(); web=start_web(runtime); done=threading.Event()
    def stop(*_): done.set()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop); done.wait(); web.shutdown(); web.server_close(); runtime.stop()
if __name__=='__main__': main()
