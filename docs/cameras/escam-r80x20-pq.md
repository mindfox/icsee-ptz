# Chinese Camera — Consolidated Project Record

_Last consolidated: 2026-08-02_

## Purpose

This file isolates every recoverable project detail concerning the Chinese/iCSee PTZ camera and the Frigate ONVIF compatibility work.

It distinguishes:

- **physically verified behavior**
- **camera-reported ONVIF declarations**
- **proxy behavior**
- **historical configuration**
- **inferences and unresolved questions**

Passwords and other reusable secrets are intentionally omitted.

---

# 1. Camera identity

## Confirmed identification

| Field | Value |
|---|---|
| Brand | ESCAM |
| Model / device version | `R80X20-PQ` |
| Hardware platform | `XM530_R80X20-PQ_8M` |
| Firmware | `V5.00.R02.00030695.10010.244306.0000000` |
| Ecosystem / app | iCSee |
| Likely OEM platform | Xiongmai / XM |
| Camera type | PTZ IP camera with optical zoom |

The camera was initially described only as a generic Chinese camera using the Android **iCSee** application. A later project continuation record identified it as **ESCAM R80X20-PQ** with an XM530 hardware platform.

---

# 2. Network services and historical addresses

## Ports

| Service | Port | Notes |
|---|---:|---|
| RTSP | TCP `554` | Main and substream access |
| ONVIF | TCP `8899` | Correct native ONVIF port |
| DVRIP | TCP `34567` | Login/basic commands work |
| HTTP | TCP `80` | Returned HTML, not ONVIF SOAP |

An early Frigate configuration incorrectly targeted ONVIF port `80`. This returned HTML and caused an invalid SOAP envelope. Native ONVIF connectivity was then confirmed on port `8899`.

## IP addresses observed during the project

| Address | Context |
|---|---|
| `192.168.31.95` | Initial `cam_1` address |
| `192.168.31.62` | Address used by a later ONVIF compatibility test |
| `192.168.31.176` | Current/later `cam_2` address and multicamera configuration |

These changing addresses appear to be DHCP-related. A previous proxy outage was traced to the camera receiving a different DHCP lease, not to the proxy itself.

The camera is on the remote `192.168.31.0/24` network reached through the site-to-site IPsec VPN. The remote WAN uses Starlink.

---

# 3. Historical Frigate camera names

| Frigate name | Meaning |
|---|---|
| `cam_1` | Initial Chinese PTZ test camera |
| `cam_2` | Later/new Chinese PTZ test camera used by the compatibility proxy |

A separate camera named `fireplace` is a Tapo C200 and must not be conflated with this ESCAM/iCSee camera.

---

# 4. Historical RTSP configuration

The initial Frigate configuration used these URL forms:

```text
Main stream:
rtsp://<camera-ip>:554/user=admin_password=<redacted>_channel=1_stream=0.sdp?real_stream

Substream:
rtsp://<camera-ip>:554/user=admin_password=<redacted>_channel=2_stream=0.sdp?real_stream
```

Historical detect resolution:

```yaml
detect:
  width: 1280
  height: 720
```

The exact current RTSP credentials and address must be taken from the deployed configuration, not this record.

---

# 5. Native ONVIF profile and advertised capabilities

## Tokens reported by the camera

```text
Profile token:           000
PTZ configuration token: 000
PTZ node token:          000
```

## Advertised ranges

Continuous velocity:

```text
Pan/tilt x: -1 to 1
Pan/tilt y: -1 to 1
Zoom:       -1 to 1
```

Relative translation:

```text
Pan/tilt x: -1 to 1
Pan/tilt y: -1 to 1
Zoom:        0 to 1
```

Preset speed:

```text
PanTiltSpeedSpace: 1 to 8
ZoomSpeedSpace:    1 to 8
```

Defaults:

```text
PanTilt speed x=1, y=1
Zoom speed x=1
Default PTZ timeout: PT1S
```

Advertised capabilities:

```text
HomeSupported = true
MaximumNumberOfPresets = 255
```

## Important reliability warning

These are **camera-reported declarations**, not proof of working behavior.

`GetStatus` was observed returning:

```text
Pan/tilt position x=0, y=0
Zoom position x=0
MoveStatus = IDLE
UtcTime = 1970-01-01T00:00:00Z
```

The camera itself had valid NTP time. Therefore at least some native ONVIF state values are placeholders or fabricated.

---

# 6. Physically verified native behavior

## Works

- ONVIF manual pan.
- ONVIF manual tilt.
- ONVIF optical zoom using velocity commands.
- ONVIF `ContinuousMove`.
- ONVIF `Stop`.
- At least some `GotoPreset` operations eventually worked during later proxy/Frigate testing.
- DVRIP authentication and basic non-PTZ requests.
- RTSP streaming.

## Does not work reliably

- Native FOV-relative `RelativeMove`.
- Native zoom `AbsoluteMove`.
- DVRIP PTZ movement.
- Native PTZ position/status reporting.
- Preset naming and persistence.
- Some preset operations that return HTTP 200.

## DVRIP PTZ

DVRIP PTZ requests returned:

```text
Ret: 100
```

but the camera did not physically move.

## HTTP success is not physical success

Multiple ONVIF requests returned HTTP 200 while producing no visible movement. All future testing must distinguish:

1. transport/protocol success;
2. parsed response success;
3. actual camera movement.

---

# 7. Manual movement characteristics

Directional movement used native ONVIF:

```text
ContinuousMove → timed delay → Stop
```

Observed test values:

```text
PTZ_SPEED = 0.5
```

Examples:

```text
left:  x=-0.5, y=0
right: x=0.5,  y=0
```

Movement pulse selector:

```text
steps 1–10
```

Timing:

```text
step 1  = 0.04 seconds
step 10 = 0.40 seconds
```

These commands physically moved the camera.

---

# 8. Zoom behavior

The camera showed an optical zoom OSD of approximately:

```text
x1.0 to x8.0
```

Observed behavior:

- a short press changed zoom by roughly `0.1`;
- longer presses caused larger changes;
- native velocity zoom works;
- native absolute zoom does not work reliably;
- a saved preset appeared to preserve pan/tilt but not necessarily zoom.

The proxy therefore implemented:

- relative zoom through bounded velocity pulses;
- absolute zoom through a synthetic zoom-position model;
- a zoom-only `Stop`;
- full zoom-out after returning to the home preset.

Historical tuning values included:

```yaml
zoom:
  relative_velocity: 0.5
  absolute_full_travel_seconds: 8.0

return_preset_zoom_reset:
  enabled: true
  delay_seconds: 0.25
  velocity: -0.5
  duration_seconds: 8.0
```

---

# 9. Preset history

## Native preset token

The early camera investigation found an existing preset:

```text
token: 0
```

## Preset name

The useful home/return preset was named:

```text
vertical
```

The proxy later used this deterministic alias:

```text
preset token 0 → vertical
```

## Early failed `GotoPreset` test

A request containing only:

```xml
<tptz:ProfileToken>000</tptz:ProfileToken>
<tptz:PresetToken>0</tptz:PresetToken>
```

returned HTTP 200 but did not move the camera.

A later request explicitly included preset speed:

```text
preset token: 0
speed_x: 1
speed_y: 1
speed-space:
http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace
```

It also returned HTTP 200 without physical movement.

Conclusion at that stage:

```text
Explicit preset speed did not fix native GotoPreset.
```

## Later working behavior

During later Frigate/proxy testing, `GotoPreset` for `vertical` moved the camera back to its home position. This established that preset recall could work in at least some camera states and request paths.

## Name-loss regression

After zoom compatibility changes, Frigate saw preset tokens/positions but blank names and reported:

```text
vertical is not a valid preset for cam_2
```

The proxy repaired blank `GetPresets` names using configured aliases while preserving any nonblank native names.

## Suspected reboot/power-cycle loss

The user observed that preset names disappeared repeatedly and later suspected this correlated with camera reboot or power loss.

Current working hypothesis:

```text
The camera firmware does not persist all preset metadata, and may not persist
the native preset definition itself, across reboot/power cycle.
```

This is not yet fully proven. The following cases must be distinguished:

1. only the preset name disappears;
2. `GetPresets` stops listing it but the token still works;
3. token remains but position is lost;
4. complete preset definition is erased.

---

# 10. Proposed preset-persistence solution

The preferred design is a **proxy-owned persistent preset registry**.

The proxy should:

- store known logical preset names persistently;
- associate each logical preset with native token and camera;
- return stable names through `GetPresets`;
- translate `GotoPreset` from logical preset to native camera operation;
- detect camera restart/reconnection;
- verify whether native preset token/position still exists;
- recreate or reconcile lost native presets where technically possible;
- avoid treating native preset metadata as authoritative.

For the current single return preset, the minimum useful persistent mapping is:

```yaml
preset_aliases:
  "0": vertical
```

A complete solution requires experimentally determining what the camera loses on reboot.

Potential fallback strategies discussed:

- vendor home/guard position;
- deterministic mechanical calibration/home position;
- recreating a native preset after restart;
- virtual preset represented by a repeatable movement path;
- proxy-controlled boot-time positioning.

---

# 11. Frigate compatibility problem

Frigate autotracking requires behavior the native camera does not reliably provide.

The central incompatibilities were:

- Frigate requires FOV-relative movement.
- The camera advertises `RelativeMove` but does not physically execute it reliably.
- Frigate requires meaningful movement status.
- Native status/position values are unreliable.
- Frigate zoom compatibility expected relative/absolute behavior that the camera lacks.
- Frigate requires a stable named return preset.

---

# 12. Proxy compatibility transformations

The proxy evolved to provide:

- FOV `RelativeMove` → bounded native `ContinuousMove` pulse + `Stop`;
- synthetic `MOVING` / `IDLE`;
- injected PTZ capability metadata;
- relative zoom → velocity zoom pulse;
- absolute zoom → synthetic-position-based velocity pulse;
- synthetic absolute zoom status;
- blank preset-name repair;
- return-preset zoom reset;
- retry-forever behavior for temporarily unreachable camera;
- rewritten ONVIF service addresses;
- later multicamera runtime isolation.

Frigate calibration eventually progressed through pan/tilt and zoom.

Historical relative-move tuning:

```yaml
relative_move:
  velocity: 0.5
  pulse_min_seconds: 0.04
  pulse_seconds_per_fov: 0.8
  pulse_max_seconds: 1.0
```

Example observed translation:

```text
RelativeMove → ContinuousMovePulse
requested=(1,1)
velocity=(0.5,0.5)
pulse_seconds≈0.84
```

Frigate-required capability flags included:

```text
MoveStatus="true"
StatusPosition="true"
```

---

# 13. Proxy repository and deployment

## Repository

```text
GitHub:       mindfox/icsee-ptz
Forked from:  dbuezas/icsee-ptz
```

Historical working branch:

```text
feature/handle-missing-detect-response
```

Local checkout:

```text
~/icsee-ptz-lab/icsee-ptz
```

Proxy directory:

```text
lab/onvif_proxy
```

Web investigation UI:

```text
lab/webui/app.py
```

Historical container:

```text
frigate-onvif-proxy
```

Historical proxy listener for `cam_2`:

```text
8999
```

Historical Frigate endpoint:

```text
http://ai-lab.mindfox:8999/onvif/device_service
```

## Configuration policy

- Local editable `config.yaml` is ignored by Git.
- Tracked defaults/examples use `config.yaml.example`.
- Environment substitution is supported for secrets.
- Python-only changes use `git pull` and container restart.
- Rebuild only for Dockerfile or dependency changes.
- Avoid unnecessary Frigate restarts.

---

# 14. Multicamera architecture

The agreed target architecture was:

- one container;
- one Python process;
- one YAML configuration;
- one listener port per camera;
- one independent runtime/state object per camera;
- no shared mutable PTZ/zoom state;
- one failed camera must not block another.

The first behavior-preserving migration kept only `cam_2` on port `8999`.

Later test configuration showed:

```yaml
cameras:
- id: cam_2
  name: Chinese PTZ camera
  driver: icsee_onvif
  host: 192.168.31.176
  listen:
    host: 0.0.0.0
    port: 12002
  options:
    onvif_port: 8899
    ptz_path: /onvif/ptz_service
    pulse_seconds: 0.1
    timeout: 10
```

That `12002` listener was part of a multicamera test stack, not the established production `8999` endpoint.

---

# 15. Web investigation UI

The UI was explicitly an **investigation harness**, not a polished product.

Functions included:

- manual directional ONVIF PTZ;
- optical zoom;
- movement pulse selector;
- manual preset refresh;
- save current position to an existing preset token;
- `GotoPreset`;
- read-only ONVIF diagnostics;
- console logging;
- web-service restart;
- web feed disabled by default.

Safety rules:

- no automatic movement;
- no automatic preset refresh;
- no automatic diagnostics;
- no guessed/new preset tokens;
- operate only on an existing selected token;
- keep web feed disabled during risky PTZ/preset tests;
- avoid simultaneous DVRIP snapshots and ONVIF PTZ;
- log exact request parameters and elapsed time.

---

# 16. Firmware fragility and safety findings

- A previous PTZ experiment caused the camera to go offline until it was power-cycled.
- Concurrent ONVIF, DVRIP and snapshot operations were considered unsafe.
- The web feed was intentionally disabled during risky PTZ tests.
- Native response codes cannot be trusted as proof of execution.
- Automated probing should remain bounded and serialized.
- Brute-forcing all preset-speed combinations was explicitly rejected.

---

# 17. Important historical commits

These were recorded in project continuations and may no longer be branch HEAD:

```text
0bbdc8fccdda95353789b6b619cc5cc8b6e2ec7b
Label camera-reported diagnostics and flag placeholders

5eddebe7ea9805f806b9136fcfb81164a1b1202e
Add configurable preset speed test

4f21ee5ad50207f067c9099fa36fa64ace2889da
Absolute zoom translation

89e9a34c5fd4898dff6bda3649fa08d87eb666c7
Blank preset-name repair

bf703e0a2d63c5f1f4f1b47d0fda134807fcfaae
Later branch state previously recorded

e9608fe
Reported commit containing preset-persistence analysis
```

The current repository must be inspected before relying on any of these as the latest commit.

---

# 18. Verified conclusions

1. The camera is an **ESCAM R80X20-PQ**, XM530/iCSee platform.
2. Native ONVIF is on port `8899`, not port `80`.
3. Native `ContinuousMove`, `Stop`, manual pan/tilt and velocity zoom work.
4. Native `RelativeMove`, `AbsoluteMove`, status/position and preset behavior are unreliable.
5. DVRIP PTZ acknowledges commands but does not physically move the camera.
6. The camera advertises capabilities it does not faithfully implement.
7. The proxy successfully enabled Frigate calibration/tracking by translating unsupported operations.
8. Preset alias `0 → vertical` was required because native preset names became blank.
9. The camera likely loses preset metadata or definitions after reboot/power cycle, but the exact loss mode remains unverified.
10. Persistent preset ownership belongs in the proxy, not in trust of the camera firmware.

---

# 19. Unresolved questions

The next controlled investigation should answer:

1. Does a reboot erase only the preset name, or the position too?
2. Does token `0` still execute after names disappear?
3. Does `GetPresets` return token `0` after reboot?
4. Does the full preset object change before versus after reboot?
5. Does `SetPreset` genuinely write a position, or only return success?
6. Can a native preset be recreated automatically after startup?
7. Does the camera expose a vendor guard/home position through DVRIP or another XM command?
8. Can reboot be detected reliably through uptime, connection state, device time, or service response?
9. Is there a deterministic mechanical zero/home position after boot?
10. Are zoom and pan/tilt preset persistence independent?

---

# 20. Recommended next experiment

Capture the following before and after one deliberate camera reboot:

```text
GetPresets complete raw response
GetStatus complete raw response
token 0 name
token 0 PanTilt position
token 0 Zoom position
physical GotoPreset result
physical zoom result
camera boot orientation
```

Do not modify or recreate the preset between the two captures. This isolates what the reboot itself destroys.

Afterward, test whether sending `SetPreset` for existing token `0` restores:

- the list entry;
- the name;
- the position;
- physical `GotoPreset`.

---

# 21. Source provenance

This consolidation was derived from:

- Frigate project conversation history from 2026-07-29 through 2026-08-01;
- the saved ONVIF PTZ proxy continuation record;
- the multicamera refactor continuation record;
- pasted Frigate/proxy configuration and diagnostics;
- the later preset-persistence discussion.

Where records conflicted, this file preserves the timeline rather than silently treating an earlier observation as the final truth.
