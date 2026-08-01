# Chinese camera preset persistence after reboot

## Context

This note belongs to the Chinese/iCSee camera work, not the Tapo C200 thread.

Observed hypothesis: presets configured through the ONVIF proxy may disappear after the Chinese camera is rebooted or power-cycled because the camera may not persist the preset metadata or positions in non-volatile storage.

The next session should confirm the exact failure mode before implementing a fix.

## Recommended architecture

Do not treat the camera as the sole authoritative store for presets. Maintain a persistent preset registry in the proxy and reconcile it with the camera after startup.

Example conceptual structure:

```yaml
camera_id: cam_2
presets:
  vertical:
    native_token: "1"
    restore_strategy: boot_position
```

The proxy should:

1. expose persistent preset names through `GetPresets`;
2. translate `GotoPreset` to the camera's native operation;
3. detect camera reboot or loss of preset state;
4. reconcile or recreate missing presets automatically.

This keeps Frigate-facing preset names stable even when the camera forgets them.

## Determine the exact failure mode

After the next controlled reboot, distinguish these cases.

### Case A: only names disappear

Native preset tokens and positions remain, but names are blank, changed, or absent.

Possible solution:

- persist the token-to-name mapping in the proxy;
- virtualize `GetPresets` names;
- optionally restore names to the camera when supported.

### Case B: `GetPresets` loses entries, but old tokens still work

The camera no longer reports presets, but `GotoPreset` using previously known tokens still moves to the correct positions.

Possible solution:

- fully virtualize `GetPresets` from the proxy registry;
- retain and use the old native tokens;
- reconcile only when a token stops working.

### Case C: tokens and positions are lost

Old `GotoPreset` calls no longer work after reboot.

This is harder because `SetPreset` normally saves the camera's current position. The proxy cannot recreate an arbitrary position unless it has a stable reference or reproducible movement method.

## Practical solution for the Frigate return preset

Frigate currently needs a reliable return preset such as `vertical`.

If the camera always boots into the same physical pan/tilt position, the proxy can perform startup reconciliation:

1. wait until the camera's ONVIF service is reachable;
2. query presets;
3. if `vertical` is missing, call `SetPreset` at the current boot position;
4. persist the resulting token/name mapping;
5. expose `vertical` consistently to Frigate.

This is likely the simplest robust solution if the boot position is deterministic.

## If the boot position is not deterministic

Investigate these alternatives in order.

### 1. Vendor home or guard position

The camera may persist a vendor-specific home/guard position even if ONVIF presets are volatile.

The proxy could:

1. command the camera to the persistent vendor home position;
2. wait for motion to finish;
3. recreate the ONVIF preset there.

### 2. Mechanical calibration

At startup, establish a physical origin by moving to mechanical limits, then replay timed movements to a logical preset.

Possible sequence:

1. drive fully left until the mechanical stop;
2. drive fully up or down to another stop;
3. treat that position as the origin;
4. replay calibrated movement pulses to the target position.

Risks:

- timed continuous movement can drift;
- motor speed may vary with voltage, temperature, or load;
- repeated contact with mechanical stops may be undesirable.

Use this only if no persistent vendor home position exists.

### 3. Proxy-side virtual movement paths

Persist movement instructions instead of native camera presets.

Example:

```yaml
presets:
  vertical:
    anchor: calibrated_home
    movements:
      - pan: 0.5
        tilt: 0
        seconds: 1.35
      - pan: 0
        tilt: -0.5
        seconds: 0.62
```

`GotoPreset` would move to a known anchor and replay the path.

This is a fallback because accumulated movement timing errors reduce precision.

## Proposed implementation order

1. Perform a controlled camera reboot.
2. Capture `GetPresets` before and after reboot.
3. Test whether pre-reboot native preset tokens still work afterward.
4. Verify whether the camera returns to a deterministic boot position.
5. Check whether a vendor home/guard position survives reboot.
6. Implement a persistent proxy preset registry.
7. Add startup reconciliation for the single Frigate return preset first.
8. Add broader multi-preset restoration only after the single-preset path is reliable.

## Test automation to add

The self-publishing diagnostic runner should eventually include a Chinese-camera preset persistence test that records:

- pre-reboot preset list and tokens;
- post-reboot preset list and tokens;
- result of `GotoPreset` with old tokens;
- boot-position observations;
- whether the proxy successfully restored the configured return preset;
- sanitized logs and timestamps.

The actual power-cycle or reboot step may remain manual, but pre-reboot and post-reboot collection should be automated and publish results to the repository.

## Current status

No implementation decision has been made yet. The key unknown is whether the camera loses only preset names/listing, or loses the underlying native positions and tokens as well.
