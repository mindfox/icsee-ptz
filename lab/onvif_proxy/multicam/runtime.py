from __future__ import annotations

import json, os, subprocess, threading, xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import requests
from requests.auth import HTTPDigestAuth
from .config import ProxyConfig
from .onvif_synth import advertise_ptz, synthetic_ptz_response
from .registry import DriverRegistry

SOAP12="http://www.w3.org/2003/05/soap-envelope"; TPTZ="http://www.onvif.org/ver20/ptz/wsdl"
def lname(tag): return tag.rsplit("}",1)[-1]
def action(body):
    try: root=ET.fromstring(body)
    except ET.ParseError: return "unknown"
    bodies=[n for n in root.iter() if lname(n.tag)=="Body"]
    return lname(list(bodies[0])[0].tag) if bodies and list(bodies[0]) else "unknown"
def vector(body):
    try: root=ET.fromstring(body); pts=[n for n in root.iter() if lname(n.tag)=="PanTilt"]
    except ET.ParseError: return 0.0,0.0
    if not pts: return 0.0,0.0
    try: return float(pts[0].attrib.get("x",0)),float(pts[0].attrib.get("y",0))
    except ValueError: return 0.0,0.0
def empty(operation):
    env=ET.Element(f"{{{SOAP12}}}Envelope"); body=ET.SubElement(env,f"{{{SOAP12}}}Body"); ET.SubElement(body,f"{{{TPTZ}}}{operation}Response")
    return ET.tostring(env,encoding="utf-8",xml_declaration=True)

class TapoHandler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def send(self,status,payload,ctype="application/soap+xml; charset=utf-8"):
        self.send_response(status); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(payload))); self.send_header("Connection","close"); self.end_headers(); self.wfile.write(payload)
    def do_GET(self): self.send(200,b'{"status":"ok"}',"application/json") if self.path=="/health" else self.send(404,b"not found","text/plain")
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get("Content-Length","0"))); op=action(body)
        if op in {"RelativeMove","ContinuousMove"}:
            pan,tilt=vector(body); self.server.driver.move(pan,tilt,self.server.pulse); return self.send(200,empty(op))
        if op=="Stop": self.server.driver.stop(); return self.send(200,empty(op))
        synthesized=synthetic_ptz_response(op) if urlsplit(self.path).path.endswith("ptz_service") else None
        if synthesized is not None: return self.send(200,synthesized)
        upstream=f"http://{self.server.camera.host}:{self.server.onvif_port}{urlsplit(self.path).path}"
        headers={k:v for k,v in self.headers.items() if k.lower() not in {"host","content-length","connection"}}
        try:
            response=requests.post(upstream,data=body,headers=headers,auth=HTTPDigestAuth(self.server.camera.username or "",self.server.camera.password or ""),timeout=self.server.timeout)
            public=f'http://{self.headers.get("Host",f"127.0.0.1:{self.server.server_port}")}'
            camera=f"http://{self.server.camera.host}:{self.server.onvif_port}"
            payload=response.content.replace(camera.encode(),public.encode())
            if op in {"GetCapabilities","GetServices"}: payload=advertise_ptz(payload,public)
            self.send(response.status_code,payload,response.headers.get("Content-Type","application/soap+xml; charset=utf-8"))
        except requests.RequestException as exc: self.send(502,str(exc).encode(),"text/plain")
    def log_message(self,*_): return

class TapoServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,camera,driver):
        super().__init__((camera.listen_host,camera.listen_port),TapoHandler); self.camera=camera; self.driver=driver
        self.onvif_port=int(camera.options.get("onvif_port",2020)); self.timeout=float(camera.options.get("timeout",10)); self.pulse=float(camera.options.get("pulse_seconds",0.1))

class CameraRuntime:
    def __init__(self,config:ProxyConfig,proxy_script="proxy.py"):
        self.config=config; self.proxy_script=proxy_script; registry=DriverRegistry.defaults()
        self.drivers={c.camera_id:registry.create(c) for c in config.cameras}; self.servers=[]; self.processes=[]
    def start(self):
        for c in self.config.cameras:
            if c.driver=="icsee_onvif":
                env=os.environ.copy(); env.update({"CAMERA_HOST":c.host,"CAMERA_ONVIF_PORT":str(c.options.get("onvif_port",8899)),"CAMERA_USERNAME":c.username or "","CAMERA_PASSWORD":c.password or "","PROXY_LISTEN_HOST":c.listen_host,"PROXY_LISTEN_PORT":str(c.listen_port)})
                self.processes.append(subprocess.Popen(["python",self.proxy_script],env=env))
            elif c.driver=="tapo_c200":
                server=TapoServer(c,self.drivers[c.camera_id]); threading.Thread(target=server.serve_forever,daemon=True).start(); self.servers.append(server)
            else: raise ValueError(f"unsupported driver: {c.driver}")
    def stop(self):
        for server in self.servers: server.shutdown(); server.server_close()
        for process in self.processes:
            process.terminate()
            try: process.wait(5)
            except subprocess.TimeoutExpired: process.kill()
    def status(self):
        result=[]
        for c in self.config.cameras:
            try: result.append(self.drivers[c.camera_id].status())
            except Exception as exc: result.append({"id":c.camera_id,"name":c.name,"driver":c.driver,"host":c.host,"error":f"{type(exc).__name__}: {exc}"})
        return result
    def move(self,camera_id,pan,tilt,duration=.1): self.drivers[camera_id].move(pan,tilt,duration)
    def stop_camera(self,camera_id): self.drivers[camera_id].stop()

class WebHandler(BaseHTTPRequestHandler):
    def send(self,status,data,ctype="application/json"):
        payload=data if isinstance(data,bytes) else json.dumps(data).encode(); self.send_response(status); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def do_GET(self):
        if self.path=="/api/cameras": self.send(200,self.server.runtime.status())
        elif self.path=="/health": self.send(200,{"status":"ok"})
        elif self.path=="/": self.send(200,HTML.encode(),"text/html; charset=utf-8")
        else: self.send(404,{"error":"not found"})
    def do_POST(self):
        parts=self.path.strip("/").split("/")
        if len(parts)!=4 or parts[:2]!=["api","cameras"]: return self.send(404,{"error":"not found"})
        camera_id,command=parts[2],parts[3]
        try:
            if command=="move":
                payload=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}"); self.server.runtime.move(camera_id,float(payload.get("pan",0)),float(payload.get("tilt",0)),float(payload.get("duration",.1)))
            elif command=="stop": self.server.runtime.stop_camera(camera_id)
            else: return self.send(404,{"error":"unknown command"})
            self.send(200,{"status":"ok"})
        except KeyError: self.send(404,{"error":"unknown camera"})
        except (ValueError,json.JSONDecodeError) as exc: self.send(400,{"error":str(exc)})
        except Exception as exc: self.send(502,{"error":f"{type(exc).__name__}: {exc}"})
    def log_message(self,*_): return

HTML='''<!doctype html><meta charset="utf-8"><title>Camera proxy</title><style>body{font:16px sans-serif;max-width:900px;margin:2rem auto}section{border:1px solid #aaa;padding:1rem;margin:1rem 0}button{font-size:1.2rem;margin:.2rem;padding:.5rem 1rem}</style><h1>Multi-camera ONVIF proxy</h1><main></main><script>async function post(id,cmd,p={}){let r=await fetch(`/api/cameras/${id}/${cmd}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)});if(!r.ok)alert(await r.text())}async function load(){let cs=await(await fetch('/api/cameras')).json();document.querySelector('main').innerHTML=cs.map(c=>`<section><h2>${c.name}</h2><div>${c.driver} — ${c.host}</div><button onclick="post('${c.id}','move',{tilt:1})">↑</button><br><button onclick="post('${c.id}','move',{pan:-1})">←</button><button onclick="post('${c.id}','stop')">■</button><button onclick="post('${c.id}','move',{pan:1})">→</button><br><button onclick="post('${c.id}','move',{tilt:-1})">↓</button><pre>${JSON.stringify(c.capabilities||c.error,null,2)}</pre></section>`).join('')}load()</script>'''
def start_web(runtime):
    server=ThreadingHTTPServer((runtime.config.web_host,runtime.config.web_port),WebHandler); server.runtime=runtime; threading.Thread(target=server.serve_forever,daemon=True).start(); return server
