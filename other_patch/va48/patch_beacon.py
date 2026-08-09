#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_beacon.py -- splash-framebuffer boot beacon for the VA48 kernel.

WHY THIS EXISTS
---------------
On this device (OnePlus ACE 5 / PKG110, sm8650) there is no usable kernel log
channel for a boot that dies before userspace:

  * console=ttynull on the cmdline, no UART hardware access
  * ramoops/pstore DDR region does NOT survive reset on this platform
    (proven: pmsg zone never reappears as pmsg-ramoops-0, /data/debugging/
     last_kmsg.txt has never been created, /sys/fs/pstore always empty)
  * OPPO's own minidump channel needs late-loading vendor modules
  * CONFIG_FB and CONFIG_VT are both off -> no framebuffer console
  * ramdump needs a PC

What IS available: the bootloader leaves the DPU continuously scanning out
the continuous-splash framebuffer, and that region is described in the DT as

    reserved-memory/splash_region  reg = <0x0 0xd5100000  0x0 0x02b00000>
    label = "cont_splash_region"       (43 MiB, and NO no-map property)

So the kernel can paint into it and the change is immediately visible on the
panel, with no driver, no userspace, and no console. We use that as a
12-checkpoint progress bar: band i is painted when checkpoint i is reached.
Photograph the screen, count the stripes, and you know exactly how far the
kernel got.

Because splash_region is reserved but NOT no-map, it stays in
memblock.memory and is therefore covered by the linear map, so phys_to_virt()
is valid for it once paging_init() has run.

BAND LAYOUT (offsets from 0xd5100000)
-------------------------------------
    CP0  +0x000000    160 KiB  (thicker, so "mechanism works" is unmistakable)
    CPi  +i*0x40000    64 KiB   for i = 1..11

    Stride is 256 KiB, not 1 MiB, so all twelve bands live inside the first
    2.8 MiB of the region. The bootloader's scanout geometry is not discoverable
    from DT or /proc (the XBL log's 1080x1920 is the logo asset, not the
    stride), so a 1 MiB stride would put CP8..CP11 past the end of a
    1080x1920x4 (7.9 MiB) framebuffer and make them silently invisible -
    which would read as "kernel died at CP7" when it actually reached CP11.
    2.8 MiB is inside every plausible framebuffer, panel-sized or FHD.

MAPPING METHOD PER PHASE
------------------------
    CP0        head.S, MMU+D-cache still OFF -> raw physical store
    CP1..CP2   early_ioremap (valid from early_ioremap_init() until
               early_ioremap_reset(); 64 KiB = 16 pages <= NR_FIX_BTMAPS=64)
    CP3..CP11  phys_to_virt + dcache_clean_inval_poc (valid from paging_init())

CHECKPOINT -> PATCH CORRESPONDENCE (what each band proves)
----------------------------------------------------------
    CP0   CPU is executing kernel code; framebuffer address is correct
    CP1   early_fixmap_init/early_ioremap_init survived 4-level pud reuse  (E1)
    CP2   arm64_memblock_init survived the linear_region_size change       (B1)
    CP3   paging_init survived                       (C1 / D1 / F1 / I1)
    CP4   bootmem_init survived (vmemmap populated)
    CP5   rest of setup_arch survived
    CP6   mm_init
    CP7   sched_init
    CP8   vfs_caches_init
    CP9   end of start_kernel
    CP10  kernel_init_freeable done (all initcalls, driver probes)
    CP11  about to exec userspace init -> kernel side is fully OK,
          any remaining failure is userspace / vendor module loading

Run AFTER patch_va48.py, from the kernel source root.
Exits non-zero unless exactly EXPECT edits applied.
"""

import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

# C form carries the UL suffix; the assembler does not understand it.
SPLASH_BASE_C = "0xd5100000UL"
SPLASH_BASE_ASM = "0xd5100000"

applied = []
failed = []


def patch(relpath, edits):
    p = os.path.join(ROOT, relpath)
    if not os.path.exists(p):
        failed.append("%s: FILE NOT FOUND" % relpath)
        return
    with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
        src = f.read()
    orig = src
    for name, old, new in edits:
        n = src.count(old)
        if n == 0:
            failed.append("%s [%s]: anchor not found" % (relpath, name))
            continue
        if n > 1:
            failed.append("%s [%s]: anchor matched %dx (ambiguous)" % (relpath, name, n))
            continue
        src = src.replace(old, new, 1)
        applied.append("%s [%s]" % (relpath, name))
    if src != orig:
        with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(src)


# ---------------------------------------------------------------- head.S : CP0
# MMU and D-cache are still off here, so a plain store goes straight to DRAM
# (Device-nGnRnE) and the DPU sees it immediately. x0-x3 carry the boot
# protocol arguments and must not be touched; x4-x6 are free.
HEAD_OLD = "SYM_CODE_START(primary_entry)\n\tbl\tpreserve_boot_args\n"
HEAD_NEW = """SYM_CODE_START(primary_entry)
	/*
	 * VA48 BEACON CP0: paint a 160 KiB white band at the start of the
	 * continuous-splash framebuffer. MMU and caches are still off, so this
	 * store lands in DRAM directly and the DPU (still scanning out the
	 * bootloader splash) shows it right away. This is the only diagnostic
	 * channel that works this early on this device.
	 *
	 * 160 KiB (not the full 256 KiB stride) so that a gap remains before the
	 * CP1 band at +256 KiB and the two do not merge into one block. CP0 is
	 * deliberately thicker than the rest so it is unmistakable.
	 *
	 * x0-x3 hold the boot arguments - only x4-x6 are used here.
	 */
	mov_q	x4, %s
	add	x6, x4, #0x28000
	mov	x5, #-1
0:	stp	x5, x5, [x4], #16
	cmp	x4, x6
	b.lo	0b
	dsb	sy

	bl	preserve_boot_args
""" % SPLASH_BASE_ASM

patch("arch/arm64/kernel/head.S", [("CP0 raw-phys splash band", HEAD_OLD, HEAD_NEW)])

# -------------------------------------------------------- setup.c : the helper
SETUP_FN_OLD = "void __init __no_sanitize_address setup_arch(char **cmdline_p)\n"
SETUP_FN_NEW = """/*
 * VA48 BEACON
 *
 * Paint band `cp' of the continuous-splash framebuffer to signal that boot
 * checkpoint `cp' was reached. See other_patch/va48/patch_beacon.py for the
 * full rationale: this device has no console, no surviving ramoops and no
 * framebuffer console, so the panel itself is the only output channel for a
 * boot that dies before userspace.
 *
 * splash_region is reserved but has no `no-map', so it stays in
 * memblock.memory and is covered by the linear map; phys_to_virt() is
 * therefore valid for it once paging_init() has run. Before that we go
 * through early_ioremap.
 */
bool va48_beacon_linear __read_mostly;

void va48_beacon(int cp)
{
	phys_addr_t pa = (phys_addr_t)VA48_SPLASH_BASE + (phys_addr_t)cp * 0x40000ULL;
	size_t len = 0x10000;

	if (va48_beacon_linear) {
		void *va = (void *)phys_to_virt(pa);

		memset(va, 0xff, len);
		dcache_clean_inval_poc((unsigned long)va, (unsigned long)va + len);
	} else {
		void __iomem *io = early_ioremap(pa, len);

		if (!io)
			return;
		memset_io(io, 0xff, len);
		early_iounmap(io, len);
	}
}

void __init __no_sanitize_address setup_arch(char **cmdline_p)
"""

# The define has to precede the function; put it with the other file-scope bits
# by prepending it to the helper block.
SETUP_FN_NEW = ("#define VA48_SPLASH_BASE\t%s\n\n" % SPLASH_BASE_C) + SETUP_FN_NEW

patch("arch/arm64/kernel/setup.c", [
    # early_ioremap()/early_iounmap()/memset_io() live behind linux/io.h, which
    # setup.c does not include on its own.
    ("linux/io.h include",
     "#include <linux/mm.h>\n",
     "#include <linux/mm.h>\n#include <linux/io.h>\n"),

    ("beacon helper + define", SETUP_FN_OLD, SETUP_FN_NEW),

    # CP1 -- early fixmap / early ioremap are up (proves patch E1)
    ("CP1 after early_ioremap_init",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n\tva48_beacon(1);\n"),

    # CP2 before paging_init (proves B1), CP3 after it (proves C1/D1/F1/I1)
    ("CP2+CP3 around paging_init",
     "\tarm64_memblock_init();\n\n\tpaging_init();\n",
     "\tarm64_memblock_init();\n\tva48_beacon(2);\n\n\tpaging_init();\n"
     "\tva48_beacon_linear = true;\n\tva48_beacon(3);\n"),

    # CP4 after bootmem_init (vmemmap is populated by now)
    ("CP4 after bootmem_init",
     "\tbootmem_init();\n",
     "\tbootmem_init();\n\tva48_beacon(4);\n"),

    # CP5 -- rest of setup_arch reached
    ("CP5 after request_standard_resources",
     "\trequest_standard_resources();\n\n\tearly_ioremap_reset();\n",
     "\trequest_standard_resources();\n\tva48_beacon(5);\n\n\tearly_ioremap_reset();\n"),
])

# ------------------------------------------------------------ init/main.c : CP6+
MAIN_DECL_OLD = "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n"
MAIN_DECL_NEW = """/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */
void va48_beacon(int cp);

asmlinkage __visible void __init __no_sanitize_address start_kernel(void)
"""

patch("init/main.c", [
    ("beacon extern decl", MAIN_DECL_OLD, MAIN_DECL_NEW),

    ("CP6 after mm_init",
     "\tmm_init();\n",
     "\tmm_init();\n\tva48_beacon(6);\n"),

    ("CP7 after sched_init",
     "\tsched_init();\n",
     "\tsched_init();\n\tva48_beacon(7);\n"),

    # note: vfs_caches_init_early() does not match this because of the suffix
    ("CP8 after vfs_caches_init",
     "\tvfs_caches_init();\n",
     "\tvfs_caches_init();\n\tva48_beacon(8);\n"),

    ("CP9 before arch_call_rest_init",
     "\tarch_call_rest_init();\n",
     "\tva48_beacon(9);\n\tarch_call_rest_init();\n"),

    ("CP10 after kernel_init_freeable",
     "\tkernel_init_freeable();\n",
     "\tkernel_init_freeable();\n\tva48_beacon(10);\n"),

    ("CP11 before userspace init",
     "\tif (ramdisk_execute_command) {\n",
     "\tva48_beacon(11);\n\n\tif (ramdisk_execute_command) {\n"),
])

EXPECT = 14

print("=" * 62)
print("VA48 BEACON PATCHER")
print("=" * 62)
for a in applied:
    print("  OK   %s" % a)
for f in failed:
    print("  FAIL %s" % f)
print("-" * 62)
print("applied=%d expected=%d failed=%d" % (len(applied), EXPECT, len(failed)))

if failed or len(applied) != EXPECT:
    print("BEACON PATCH FAILED - aborting so a silent no-op image is not produced")
    sys.exit(1)

print("beacon OK: CP0 raw-phys, CP1-2 early_ioremap, CP3-11 linear map")
print("splash framebuffer base %s; CP0 = +0 (160 KiB), CPi = +i*256KiB (64 KiB)"
      % SPLASH_BASE_C)
sys.exit(0)
