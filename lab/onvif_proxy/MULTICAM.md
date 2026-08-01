# Multi-camera ONVIF proxy

This branch runs different camera families behind one management UI while keeping one ONVIF listener per camera.

## Design

- Each camera has a stable `id`, driver name, upstream host and unique ONVIF listener.
- `icsee_onvif` preserves the existing `proxy.py` translation implementation.
- `tapo_c200` attempts local proprietary pan/tilt through `pytapo`.
- A failed camera backend is isolated: it must not terminate the shared UI or other camera listeners.
- Tapo initialization is lazy, so service startup does not contact or authenticate with the camera.
- Tapo requests are serialized per camera because supported firmware expects command ordering and may reject parallel requests.
- Capabilities are explicit. The Tapo C200 has pan/tilt hardware but no optical zoom; audio is a stream capability, not a PTZ command.

## Deployment model

Application code is bind-mounted from the Git checkout by `docker-compose.multicam.yml`.

Python-only changes require:

1. Pull the branch.
2. Restart the multi-camera container.

Rebuild the image only when `Dockerfile` or `requirements.txt` changes.

The local credential file is `local-config/cameras.yaml`. The directory is ignored by Git and must remain readable only by the container runtime group.

## Verified Tapo C200 result — 2026-08-01

Physical testing against the configured C200 established:

- RTSP authentication succeeds with the configured Camera Account.
- `stream1` provides H.264 video and PCM A-law audio.
- Native ONVIF/RTSP credentials are therefore valid.
- `pytapo` private-API authentication fails with `Invalid authentication data` when a motor command is attempted.
- The failure is contained to the Tapo backend; the manager, web UI and iCSee listener remain running.

This means RTSP/ONVIF media support and proprietary PTZ support must be treated as separate compatibility paths. `available` must not be reported for the private PTZ backend before it has been tested; the API now reports the initial state as `untested`.

The `pytapo` project documents the same generic authentication error for firmware/private-protocol incompatibilities even when RTSP credentials are valid. TP-Link documents C200 ONVIF as Profile S media interoperability and has historically stated that C200 PTZ is not exposed through ONVIF. Therefore, the current C200 firmware requires either a compatible private-control implementation or a different supported firmware path before Frigate autotracking can use its motor.

## Tapo media

Use the camera account created in the Tapo app:

- Main stream: `rtsp://USER:PASSWORD@CAMERA_IP:554/stream1`
- Sub stream: `rtsp://USER:PASSWORD@CAMERA_IP:554/stream2`
- Native ONVIF service port: `2020`

Audio must be consumed from the RTSP stream and, when necessary, transcoded by go2rtc/FFmpeg into a codec accepted by the chosen Frigate live-view path. The ONVIF PTZ adapter does not manufacture or transport audio.

## Migration safety

The legacy `config.yaml`, live `proxy.py` deployment and port `8999` remain unchanged during validation. The isolated multi-camera deployment uses a separate Compose project and ports in the `12xxx` range.
