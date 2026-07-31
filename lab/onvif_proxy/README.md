# Frigate ONVIF compatibility proxy

This service presents the camera through a Frigate-facing ONVIF endpoint while forwarding requests to the camera's native ONVIF service.

## Implemented compatibility behavior

- Proxies device, media, PTZ, imaging, events, and other ONVIF paths without hard-coding individual operations.
- Rewrites upstream camera XAddr values to the proxy address derived from the incoming HTTP `Host` header.
- Adds `TranslationSpaceFov` and relative zoom support to discovery responses.
- Converts Frigate FOV-relative pan/tilt and relative zoom requests to bounded continuous-move pulses supported by the camera.
- Synthesizes `PanTilt=MOVING` for a bounded period after translated relative moves.
- Resets zoom to the wide limit after a successful preset recall.
- Preserves native camera handling for presets and all unmodified operations.

## Deploy

```bash
cd lab/onvif_proxy
cp .env.example .env
cp config.yaml.example config.yaml
chmod 600 .env config.yaml
${EDITOR:-nano} .env
${EDITOR:-nano} config.yaml
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs -f
```

`config.yaml` is intentionally ignored by Git. Keep local tuning and secrets there. The tracked `config.yaml.example` is the template. Values support `${VARIABLE}`, `${VARIABLE:-default}`, and `${VARIABLE-default}` expansion from the container environment.

The health endpoint is:

```text
http://<docker-host>:8999/health
```

The ONVIF device service is:

```text
http://<docker-host>:8999/onvif/device_service
```

Use the Docker host address and the published proxy port in Frigate's `onvif.host` and `onvif.port` settings. Continue using the camera's normal ONVIF username and password; the proxy authenticates upstream with the values in `.env`.

## Security boundary

The proxy accepts ONVIF requests from clients on its listening interface and uses configured credentials upstream. Bind or firewall the published port so only Frigate and trusted management hosts can reach it.
