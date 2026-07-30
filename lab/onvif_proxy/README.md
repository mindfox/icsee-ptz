# Frigate ONVIF compatibility proxy

This service presents the camera through a Frigate-facing ONVIF endpoint while forwarding requests to the camera's native ONVIF service.

## Implemented compatibility behavior

- Proxies device, media, PTZ, imaging, events, and other ONVIF paths without hard-coding individual operations.
- Rewrites upstream camera XAddr values to the proxy address derived from the incoming HTTP `Host` header.
- Adds `TranslationSpaceFov` to `GetNode` and `GetConfigurationOptions` responses.
- Converts FOV-relative pan/tilt translations to the camera's working `TranslationGenericSpace`.
- Synthesizes `PanTilt=MOVING` for a bounded period after translated relative moves.
- Clears synthetic movement immediately on `Stop`.
- Preserves native camera handling for continuous movement, presets, zoom, and all unmodified operations.

## Deploy

```bash
cd lab/onvif_proxy
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs -f
```

The health endpoint is:

```text
http://<docker-host>:8999/health
```

The ONVIF device service is:

```text
http://<docker-host>:8999/onvif/device_service
```

Use the Docker host address and the published proxy port in Frigate's `onvif.host` and `onvif.port` settings. Continue using the camera's normal ONVIF username and password; the proxy authenticates upstream with the values in `.env`.

## Initial Frigate configuration shape

```yaml
cameras:
  test_camera:
    onvif:
      host: 192.168.31.10
      port: 8999
      user: admin
      password: "camera-password"
      autotracking:
        enabled: false
```

First confirm that Frigate discovers the camera and exposes manual PTZ controls through the proxy. Enable and calibrate autotracking only after proxy discovery, relative movement, `Stop`, and movement-state behavior have been validated.

## Calibration controls

`FOV_TO_GENERIC_SCALE_X` and `FOV_TO_GENERIC_SCALE_Y` scale Frigate's FOV-relative translations before forwarding them to the camera. They default to `1.0`.

Synthetic movement duration is calculated as:

```text
base_seconds + max(abs(x), abs(y)) * seconds_per_fov
```

and clamped between the configured minimum and maximum. The defaults are conservative starting values derived from the lab behavior; they are not yet camera-calibrated.

## Security boundary

The proxy accepts ONVIF requests from clients on its listening interface and uses configured credentials upstream. Bind or firewall the published port so only Frigate and trusted management hosts can reach it.
