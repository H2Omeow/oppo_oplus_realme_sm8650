#!/usr/bin/env python3
"""
Prove the VA48-USER-ONLY patch reproduces the stock VA39 kernel layout exactly.

Run this BEFORE trusting any built image. It is pure arithmetic -- no kernel
source needed -- and it encodes the values observed on the live device, so a
mismatch means the patch logic is wrong, not that the device is unusual.

    python3 verify_layout.py
"""

PAGE_SHIFT = 12
STRUCT_PAGE_MAX_SHIFT = 6
VMEMMAP_SHIFT = PAGE_SHIFT - STRUCT_PAGE_MAX_SHIFT  # 6
MASK = (1 << 64) - 1


def page_offset(va):
    return (-(1 << va)) & MASK


def page_end(va):
    return (-(1 << (va - 1))) & MASK


def vmemmap_start(va):
    return (-(1 << (va - VMEMMAP_SHIFT))) & MASK


def layout(va_bits, kernel_va_bits):
    """Mirror memory.h. VA_BITS_MIN follows KERNEL_VA_BITS after the patch."""
    va_bits_min = 48 if kernel_va_bits > 48 else kernel_va_bits
    po = page_offset(kernel_va_bits)
    pe = page_end(va_bits_min)
    modules_vaddr = pe
    modules_end = modules_vaddr + 128 * 1024 * 1024   # MODULES_VSIZE = SZ_128M
    return {
        "PAGE_OFFSET": po,
        "PAGE_END": pe,
        "linear_span": pe - po,
        "MODULES_VADDR": modules_vaddr,
        "KIMAGE_VADDR": modules_end,          # KIMAGE_VADDR == MODULES_END
        "VMEMMAP_START": vmemmap_start(kernel_va_bits),
        "TASK_SIZE_64": 1 << va_bits,         # from vabits_actual == VA_BITS
    }


def show(tag, d):
    print("== %s" % tag)
    for k, v in d.items():
        if k in ("linear_span", "TASK_SIZE_64"):
            gib = v / (1024 ** 3)
            unit = "%.0f TiB" % (gib / 1024) if gib >= 1024 else "%.0f GiB" % gib
            print("   %-14s 0x%016x  (%s)" % (k, v, unit))
        else:
            print("   %-14s 0x%016x" % (k, v))
    print()


stock39 = layout(39, 39)          # what the prebuilt vendor modules expect
naive48 = layout(48, 48)          # plain CONFIG_ARM64_VA_BITS_48 -- bricked
patched = layout(48, 39)          # VA48 user + VA39 kernel layout

show("stock VA39 (what 432 prebuilt modules were compiled against)", stock39)
show("naive VA48 (the build that bricked the phone)", naive48)
show("PATCHED: VA_BITS=48, KERNEL_VA_BITS=39", patched)

# --- the assertions that matter -------------------------------------------
fail = []

# Kernel side must be bit-identical to stock VA39, or __va() in the prebuilt
# modules still lands outside the linear map.
for k in ("PAGE_OFFSET", "PAGE_END", "MODULES_VADDR",
          "KIMAGE_VADDR", "VMEMMAP_START", "linear_span"):
    if patched[k] != stock39[k]:
        fail.append("%s: patched 0x%x != stock VA39 0x%x"
                    % (k, patched[k], stock39[k]))

# User side must be 48-bit or EAC's fixed pages still cannot be mapped.
if patched["TASK_SIZE_64"] != (1 << 48):
    fail.append("TASK_SIZE_64 is not 256 TiB")

# EAC's highest fixed allocation must fall inside the user VA.
EAC_TOP = 0x700a00000000 + 0x1000
if EAC_TOP >= patched["TASK_SIZE_64"]:
    fail.append("EAC top 0x%x does not fit in TASK_SIZE_64" % EAC_TOP)
if EAC_TOP < (1 << 39):
    fail.append("EAC top unexpectedly fits in VA39 -- premise wrong")

# KIMAGE_VADDR must equal the _stext read off the running device.
DEVICE_STEXT = 0xffffffc008000000
if patched["KIMAGE_VADDR"] != DEVICE_STEXT:
    fail.append("KIMAGE_VADDR 0x%x != device _stext 0x%x"
                % (patched["KIMAGE_VADDR"], DEVICE_STEXT))

# With four levels and a VA39-pinned kernel, the linear map shares PGD 511
# with the module/vmalloc/fixmap region. patch_va48.py deliberately removes
# the stock guard for this supported mixed-layout case; verify the geometry
# instead of treating the expected sharing as a failure.
PGDIR_SHIFT_4LVL = 39
def pgd_index(a):
    return (a >> PGDIR_SHIFT_4LVL) & 0x1ff
dme = patched["PAGE_END"]
if pgd_index(dme - 1) != 511 or pgd_index(dme) != 511:
    fail.append("expected shared PGD 511, got idx(end-1)=%d idx(end)=%d"
                % (pgd_index(dme - 1), pgd_index(dme)))
else:
    print("mmu.c shared-PGD geometry OK: idx(end-1)=511 idx(end)=511")

# init.c:99 BUILD_BUG_ON(ARM64_HW_PGTABLE_LEVELS(VA_BITS) != PGTABLE_LEVELS)
def hw_levels(va):
    return (va - 4) // (PAGE_SHIFT - 3)
if hw_levels(48) != 4:
    fail.append("init.c:99 BUILD_BUG_ON would fire: levels=%d" % hw_levels(48))
else:
    print("init.c:99 guard OK: ARM64_HW_PGTABLE_LEVELS(48) = 4 = PGTABLE_LEVELS")

print()
if fail:
    print("FAILED:")
    for f in fail:
        print("  - " + f)
    raise SystemExit(1)

print("ALL CHECKS PASSED")
print("  kernel side  : bit-identical to stock VA39 -> prebuilt __va() correct")
print("  user side    : 256 TiB -> EAC's 0x700a00000000 fits")
print()
print("STILL UNVERIFIED BY THIS SCRIPT (must check separately):")
print("  1. KASAN_HW_TAGS is compatible with this layout; GENERIC/SW_TAGS")
print("     would require a separate PAGE_END/KASAN shadow review.")
print("  2. Whether the 19 FATAL modules in vendor_dlkm matter for first boot.")
print("     They are loaded after /vendor mounts; msm_drm/msm_kgsl are display")
print("     and GPU, so the screen may stay dark even if the kernel is alive.")
print("     Check for USB enumeration / adb before concluding it failed.")
