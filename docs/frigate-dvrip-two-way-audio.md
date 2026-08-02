# Frigate + go2rtc DVRIP Two-Way Audio

_Last verified: 2026-08-02_

## Proven working pattern

For DVRIP cameras with two-way audio, Frigate/go2rtc requires:

1. A `go2rtc.webrtc.candidates` entry reachable by the browser.
2. Normal main/sub DVRIP streams for recording and detection.
3. A dedicated two-way go2rtc stream containing **two DVRIP source lines grouped under the same stream name**:
   - the normal camera media source;
   - a separate DVRIP backchannel source using `backchannel=1`.
4. Frigate `live.streams` must point to the grouped two-way stream.

The successful design is not merely a second independent camera stream. The two DVRIP source URLs are combined into one logical go2rtc stream so WebRTC has both incoming media and an outgoing camera audio backchannel.

## Working example

```yaml
go2rtc:
  webrtc:
    candidates:
      - 192.168.250.150:8555

  streams:
    cam_2:
      - dvrip://{FRIGATE_CAM1_ONVIF_USER}:{FRIGATE_CAM1_ONVIF_PASSWORD}@192.168.31.176:34567?channel=0&subtype=0

    cam_2_sub:
      - dvrip://{FRIGATE_CAM1_ONVIF_USER}:{FRIGATE_CAM1_ONVIF_PASSWORD}@192.168.31.176:34567?channel=0&subtype=1

    cam_2_twoway:
      - dvrip://{FRIGATE_CAM1_ONVIF_USER}:{FRIGATE_CAM1_ONVIF_PASSWORD}@192.168.31.176:34567?channel=0&subtype=0
      - dvrip://{FRIGATE_CAM1_ONVIF_USER}:{FRIGATE_CAM1_ONVIF_PASSWORD}@192.168.31.176:34567?backchannel=1
```

Frigate camera mapping:

```yaml
cameras:
  cam_2:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam_2
          input_args: preset-rtsp-restream
          roles:
            - record
            - audio

        - path: rtsp://127.0.0.1:8554/cam_2_sub
          input_args: preset-rtsp-restream
          roles:
            - detect

    live:
      streams:
        Stream 1: cam_2_twoway
        Stream 2: cam_2_sub
```

## Stream roles

| go2rtc stream | Purpose |
|---|---|
| `cam_2` | Main video/audio source used by Frigate FFmpeg for recording and audio |
| `cam_2_sub` | Lower-resolution source used for detection and optional Live viewing |
| `cam_2_twoway` | Live WebRTC source combining main incoming media and DVRIP outgoing backchannel |

## Important details

- `cam_2_twoway` contains two entries under one stream name. This is the key requirement.
- The first entry supplies camera video/audio to the browser.
- The second entry supplies the camera-bound audio backchannel.
- Frigate Live must select `cam_2_twoway`; pointing Live at `cam_2` alone does not provide the grouped backchannel.
- Port `8555` must be reachable for WebRTC.
- An explicit IP candidate was proven working. The hostname candidate was commented out during the successful test.
- DVRIP uses TCP port `34567`.
- `channel=0&subtype=0` is the main stream.
- `channel=0&subtype=1` is the substream.
- `backchannel=1` opens the DVRIP talkback path.

## Earlier misunderstanding corrected

A go2rtc probe showing browser-side microphone media does not, by itself, prove that the configured stream includes a camera backchannel. The missing configuration was the second DVRIP source line with `?backchannel=1`, grouped with the normal source under the same logical stream.

## Status

Physically verified working for the ESCAM/iCSee DVRIP cameras represented by `cam_1` and `cam_2` in the Frigate configuration.
