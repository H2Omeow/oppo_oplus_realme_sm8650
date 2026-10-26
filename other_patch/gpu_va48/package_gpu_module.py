#!/usr/bin/env python3
"""Package the built GPU VA48 KGSL module as a KernelSU/Magisk module.

The AnyKernel ZIP installs only the kernel Image. The GPU address limit lives in
msm_kgsl.ko, which the device loads from its vendor_dlkm partition, so flashing
an Image alone can never change it. This packer produces a KernelSU/Magisk
module that replaces that module file through an overlay instead of writing any
partition, which is what makes the change possible on a locked bootloader.

What this does NOT do:
  * It does not write vendor_dlkm, modify partitions, or disable verified boot.
  * It does not sign modules. If the device enforces module signatures the new
    module will be rejected at load time; that must be checked on device.
  * It cannot replace an already-loaded module at runtime. The replacement takes
    effect on the next boot, when the init stage loads modules from the overlaid
    path.

Deployment safety is a real constraint, not a formality: a wrong module for the
running firmware means no GPU. The installer therefore refuses to install when
the device's running kernel release does not match the module vermagic, and
keeps a restorable copy of the original module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile


MODULE_ID = "gpu_va48_kgsl"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


MODULE_PROP = """id={module_id}
name=GPU VA48 KGSL ({version})
version={version}
versionCode={version_code}
author=oppo_oplus_realme_sm8650 builder
description=Experimental 48-bit GPU TTBR0 for Adreno. Replaces msm_kgsl.ko via overlay; takes effect after reboot. Refuses to install on a mismatched kernel. Not hardware-verified.
"""

# Mounting over the real module path is what avoids touching the partition.
# KernelSU/Magisk both expose the same "replace a file under /vendor" contract.
CUSTOMIZE_SH = r"""#!/system/bin/sh
# Install the rebuilt KGSL module as an overlay over the vendor module path.
SKIPUNZIP=0

VERMAGIC_EXPECTED="$(cat "$MODPATH/vermagic.txt" 2>/dev/null)"
RUNNING="$(uname -r)"

ui_print "- GPU VA48: built for kernel $VERMAGIC_EXPECTED"
ui_print "- GPU VA48: running kernel $RUNNING"

case "$VERMAGIC_EXPECTED" in
  "$RUNNING"|"$RUNNING "*)
    ;;
  *)
    ui_print "! GPU VA48: module was built for a different kernel."
    ui_print "! Flash the matching AnyKernel3 Image first, reboot, then install."
    abort "! Refusing to install a mismatched KGSL module."
    ;;
esac

# Locate where this firmware actually keeps msm_kgsl.ko. Never guess a single
# hardcoded path: vendor_dlkm layout differs between firmware versions.
TARGET=""
for candidate in \
  /vendor/lib/modules/msm_kgsl.ko \
  /vendor_dlkm/lib/modules/msm_kgsl.ko \
  /vendor/lib/modules/*/msm_kgsl.ko \
  /vendor_dlkm/lib/modules/*/msm_kgsl.ko; do
  if [ -f "$candidate" ]; then
    TARGET="$candidate"
    break
  fi
done

if [ -z "$TARGET" ]; then
  ui_print "! GPU VA48: msm_kgsl.ko not found in vendor module paths."
  ui_print "! This firmware may build KGSL into the kernel or use another path."
  abort "! Refusing to install without a known target module path."
fi

ui_print "- GPU VA48: overlaying $TARGET"

# Keep a copy so the original module can be restored by removing the module.
mkdir -p "$MODPATH/original"
cp -f "$TARGET" "$MODPATH/original/msm_kgsl.ko" 2>/dev/null

# Build the overlay tree mirroring the real path so the module system binds it.
OVERLAY_DIR="$MODPATH${TARGET%/*}"
mkdir -p "$OVERLAY_DIR"
cp -f "$MODPATH/msm_kgsl.ko" "$OVERLAY_DIR/msm_kgsl.ko"
set_perm "$OVERLAY_DIR/msm_kgsl.ko" 0 0 0644 u:object_r:vendor_file:s0

# Dependencies are only installed when the firmware lacks them; never clobber a
# working vendor driver that the running system already loads.
if [ -d "$MODPATH/dependencies" ]; then
  ui_print "- GPU VA48: dependency modules included but not auto-overlaid."
  ui_print "- Install them manually only if KGSL fails to load."
fi

rm -f "$MODPATH/msm_kgsl.ko"

ui_print "- GPU VA48: installed. Reboot to load the new module."
ui_print "- Verify with: dmesg | grep 'GPU VA48'"
ui_print "- If the GPU fails, remove this module and reboot to restore."
"""

# A module that bricks graphics must be removable without a working screen, so
# keep the uninstall path to "delete the overlay", which the manager does itself.
POST_FS_DATA_SH = r"""#!/system/bin/sh
# Nothing to do: the overlay is applied by the module system's own mount.
# Kept so the module layout stays explicit about having no runtime patching.
exit 0
"""


def build_module(args: argparse.Namespace) -> int:
    module = args.module.resolve()
    output = args.output.resolve()
    if not module.is_file() or not module.stat().st_size:
        print(f"GPU module is absent or empty: {module}", file=sys.stderr)
        return 1

    vermagic = args.vermagic
    if not vermagic:
        evidence = module.parent / "evidence.json"
        if evidence.is_file():
            data = json.loads(evidence.read_text(encoding="utf-8"))
            vermagic = (data.get("vermagic") or "").split()
            vermagic = vermagic[0] if vermagic else ""
    if not vermagic:
        print("a kernel release (vermagic) is required to gate installation",
              file=sys.stderr)
        return 1

    staging = output.parent / (output.name + ".stage")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    (staging / "module.prop").write_text(MODULE_PROP.format(
        module_id=MODULE_ID, version=vermagic,
        version_code=args.version_code), encoding="utf-8")
    (staging / "customize.sh").write_text(CUSTOMIZE_SH, encoding="utf-8")
    (staging / "post-fs-data.sh").write_text(POST_FS_DATA_SH, encoding="utf-8")
    (staging / "vermagic.txt").write_text(vermagic + "\n", encoding="utf-8")
    shutil.copyfile(module, staging / "msm_kgsl.ko")

    dependencies = module.parent / "dependencies"
    if args.include_dependencies and dependencies.is_dir():
        shutil.copytree(dependencies, staging / "dependencies")

    manifest = {
        "module_id": MODULE_ID,
        "kernel_release": vermagic,
        "msm_kgsl_sha256": sha256(module),
        "experimental": True,
        "hardware_tested": False,
        "writes_partitions": False,
        "requires_unlocked_bootloader": False,
        "effective_after_reboot": True,
        "note": "Overlay replacement of msm_kgsl.ko. Enforced module signing or "
                "a firmware mismatch can still prevent loading; check dmesg.",
    }
    (staging / "install-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging).as_posix())
    shutil.rmtree(staging)

    print(f"GPU VA48 KernelSU/Magisk module: {output}")
    print(f"Gated to kernel release: {vermagic}")
    print("Takes effect after reboot; does not write any partition.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path,
                        help="built msm_kgsl.ko")
    parser.add_argument("--output", required=True, type=Path,
                        help="output KernelSU/Magisk module ZIP")
    parser.add_argument("--vermagic", help="kernel release the module targets")
    parser.add_argument("--version-code", default="1")
    parser.add_argument("--include-dependencies", action="store_true")
    args = parser.parse_args(argv)
    try:
        return build_module(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Failed to package GPU VA48 module: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
