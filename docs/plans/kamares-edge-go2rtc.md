# Kamares edge go2rtc option

Status: future investigation

Site: Kamares
Network: `192.168.31.0/24`

## Problem

The Kamares cameras are remote from the Frigate host and are reached through the site-to-site VPN. Multiple direct consumers of camera RTSP streams can increase camera load and may contribute to unstable video or ONVIF behavior.

## Candidate approach

Deploy an edge restreamer on the Kamares LAN, likely on a Raspberry Pi, to maintain one local connection per camera and expose restreamed feeds across the VPN.

Expected topology:

```text
Kamares cameras
    -> edge go2rtc/restreamer on 192.168.31.0/24
    -> site-to-site VPN
    -> home-side Frigate, dashboards, and other consumers
```

A Raspberry Pi 4-class device should be sufficient when streams are copied/remuxed without video transcoding. Wired Ethernet is preferred.

## Future research

Before deployment, compare available implementations and management options, including:

- plain go2rtc with configuration files;
- Docker Compose deployments;
- projects or wrappers that provide a web UI for adding and managing streams;
- authentication and secret handling;
- health monitoring and automatic recovery;
- compatibility with Tapo and iCSee/DVRIP cameras;
- exposure of main and substreams;
- behavior over the existing IPsec VPN;
- whether the edge service should expose RTSP only or also WebRTC, HLS, or snapshots;
- configuration backup and migration procedures.

## Constraints

- Avoid unnecessary video transcoding on the Raspberry Pi.
- Keep camera-facing RTSP sessions local to Kamares where possible.
- Prefer one upstream camera session shared by multiple downstream consumers.
- Preserve ONVIF/PTZ responsiveness while streams are active.
- Validate packet loss, corruption, reconnect behavior, and VPN bandwidth before production use.

## Decision

No deployment decision has been made. This is a retained option for later investigation when the Kamares streaming architecture is revisited.
