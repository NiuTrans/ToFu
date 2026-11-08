# Runtime image builder guidance

`android/runtime/` builds the local kernel's rootfs and payload archives. Its
README is the producer half of the runtime contract (lane L5's installer and
the proot lane consume it): change the README in the same commit as any
change to archive names, `index.json`, `.manifest.json`, the on-device
layout or the guest entry, and tell the consumer lanes.

- `pins.json` is written only by `build_runtime.py resolve`; review its diff.
- Builds must stay byte-identical for the same inputs. Anything that records
  the build host (paths, times, interpreters) is a defect, not noise.
- Verification: `python -m pytest android/runtime/tests` (no build needed),
  then the artifact and host-boot tiers with `--runtime-out`,
  `--runtime-out-repeat` and `--runtime-host-payload` (README §8). The
  Gradle tiers in `android/AGENTS.md` do not cover this directory.
