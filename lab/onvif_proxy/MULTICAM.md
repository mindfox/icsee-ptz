# Multi-camera ONVIF proxy

This branch introduces the configuration and driver boundary needed to run different camera families behind one management UI.

## Design

- Each camera has a stable `id`, driver name, upstream host and unique ONVIF listener.
- `icsee_onvif` preserves the existing tested `proxy.py` translation implementation.
- `tapo_c200` uses `pytapo` for local, proprietary pan/tilt commands.
- Tapo requests are serialized per camera because supported firmware expects command ordering and may reject parallel requests.
- Capabilities are explicit. The Tapo C200 has pan/tilt but no optical zoom; audio is a stream capability, not a PTZ command.

## Migration safety

The legacy `config.yaml` and `proxy.py` path remain unchanged. Do not replace the live deployment yet. First copy `config.multicam.yaml.example` to a private `config.multicam.yaml`, enter both camera records, and validate the Tapo camera credentials and firmware behavior.

## Tapo media

Use the camera account created in the Tapo app:

- Main stream: `rtsp://USER:PASSWORD@CAMERA_IP:554/stream1`
- Sub stream: `rtsp://USER:PASSWORD@CAMERA_IP:554/stream2`
- Native ONVIF service port: `2020`

Audio must be inspected from the RTSP stream and, when necessary, transcoded by go2rtc/FFmpeg into a codec accepted by the chosen Frigate live-view path. The ONVIF PTZ adapter does not manufacture or transport audio.

## Next integration boundary

The manager process will:

1. Load and validate the shared YAML.
2. Start one ONVIF listener per camera.
3. Route translated ONVIF PTZ operations to the selected driver.
4. Serve a shared UI/API using driver capability data.

The current commits intentionally establish this boundary without modifying the known-good iCSee translation logic.
