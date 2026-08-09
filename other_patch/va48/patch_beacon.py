#!/usr/bin/env python3
"""
VA48 BEACON PATCHER  (round 6)
==============================

This device has no usable kernel log channel at all:

  * console=ttynull on the cmdline, no UART hardware exposed
  * CONFIG_FB / CONFIG_VT are off  -> no framebuffer console
  * ramoops DDR does NOT survive reset on this platform (proven: pmsg-ramoops-0
    absent though logd writes pmsg continuously; /data/debugging/last_kmsg.txt
    never created despite an unconditional copy in init.oplus.debug.rc;
    /sys/fs/pstore permanently empty)
  * OPPO minidump needs late vendor modules -> useless for early boot death
  * ramdump needs a PC, which the user does not have

So the panel itself is the output channel.  The bootloader's continuous-splash
framebuffer lives at 0xd5100000 (DT reserved-memory/splash_region, 43 MiB,
label cont_splash_region, NO no-map).  The DPU keeps scanning it out, which is
why the first splash image stays on screen.  Writing there changes the display
with no driver and no userspace.

ROUND 5 RESULT (confirmed on real hardware)
-------------------------------------------
White bands DID appear, then the device hung and the PMIC watchdog reset it
(~32 s).  That settles three things:

  1. 0xd5100000 is correct AND the scanout buffer starts at offset 0.  This was
     the one assumption that could not be checked from the build host.
  2. More than one band => CP0 passed => the MMU came up => patch H1 works.
  3. PANIC_TIMEOUT=0 + PANIC_ON_OOPS=y means any panic/oops HANGS FOREVER
     instead of rebooting.  The device rebooted after a delay, so it was a
     watchdog bite on a hung kernel, not a panic.  No minidump was produced
     and nothing under /data was touched, which is consistent: a hang never
     reaches panic(), so OPPO's DFR hooks never run.

WHAT CHANGED FOR ROUND 6
------------------------
The round-5 readout asked the user to count 13-row stripes and they could not
recall the count.  That is a design defect, not a user error.  Fixes:

  a) FAT BANDS IN GROUPS OF FOUR.  Bands are ~19 rows with ~13-row gaps, and
     one slot is SKIPPED after every fourth band, producing a ~45-row gap
     between groups.  The reader reports "N groups plus M" instead of counting
     twenty individual stripes.  CP0 is deliberately thicker (~26 rows) as an
     unmistakable "the mechanism works" anchor.

  b) A PANIC DISCRIMINATOR.  panic() itself paints a much thicker block
     (~65 rows) well below the band cluster.  Present => the kernel reached
     panic().  Absent => a true hang.  No evidence gathered so far could tell
     these apart; now the screen answers it directly.

  c) LATE-REGION SUBDIVISION.  Round 5's CP10 covered all of
     kernel_init_freeable() -- every initcall and every driver probe -- which
     is far too coarse.  Round 6 puts one band after each initcall level by
     patching the loop in do_initcalls(), plus bands around SMP bringup,
     driver_init() and the jump to userspace.

BAND GEOMETRY (offsets from 0xd5100000; panel row stride is 1264*4 = 5056 B)
---------------------------------------------------------------------------
    STRIDE    0x28000  (160 KiB, ~32 rows)   slot pitch
    BAND      0x18000  ( 96 KiB, ~19 rows)   painted part of a slot
    CP0 BAND  0x20000  (128 KiB, ~26 rows)   thicker anchor
    slot(cp) = cp + cp/4                     the /4 term inserts group gaps
    PANIC     slot 25, 0x50000 (320 KiB, ~65 rows)

    Highest band ends at ~3.7 MiB, the panic block at ~4.2 MiB.  Round 5
    proved at least the first 2.8 MiB is really scanned out; a full
    1264x2780x4 framebuffer is 13.4 MiB and even a 1080x1920x4 one is 7.9 MiB,
    so 4.2 MiB is inside either.

MAPPING METHOD PER PHASE
------------------------
    CP0        head.S, MMU + D-cache still OFF -> raw physical store
    CP1..CP2   early_ioremap (valid from early_ioremap_init() until
               early_ioremap_reset(); 96 KiB = 24 pages <= NR_FIX_BTMAPS=64)
    CP3..CP19  phys_to_virt + dcache_clean_inval_poc (valid once paging_init()
               has run; splash_region is reserved but not no-map, so it stays
               in memblock.memory and the linear map covers it)

CHECKPOINT -> WHAT ITS BAND PROVES
----------------------------------
    CP0   primary_entry, first instruction   CPU runs kernel code, fb addr ok
    CP1   after early_ioremap_init()         early fixmap survives 4-level
                                             pud reuse          (patch E1)
    CP2   after arm64_memblock_init()        linear_region_size change is
                                             sane               (patch B1)
    CP3   after paging_init()                THE critical one
                                             (patches C1/D1/F1/I1)
    CP4   after bootmem_init()               vmemmap / sparsemem ok
    CP5   end of setup_arch()
    CP6   after mm_init()                    buddy + slab up
    CP7   after vfs_caches_init()
    CP8   end of start_kernel()
    CP9   after do_pre_smp_initcalls()
    CP10  after sched_init_smp()             secondary CPUs are up
    CP11  after driver_init()
    CP12..CP18  after initcall level 0..6    vendor driver probes live here;
                                             this is where round 5 was blind
    CP19  about to exec userspace init       kernel side fully OK

    PANIC block  panic() entered            distinguishes panic from hang
"""

import re
import sys
import os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

# The C form carries the UL suffix; the assembler does not understand it.
SPLASH_BASE_C = "0xd5100000UL"
SPLASH_BASE_ASM = "0xd5100000"

CP0_BAND_ASM = "0x20000"          # must be encodable as an ADD immediate

EXPECT = 18
applied = 0
failed = 0


def patch(relpath, edits):
    """Apply (label, old, new) edits to relpath. Refuse on 0 or >1 matches."""
    global applied, failed
    path = os.path.join(ROOT, relpath)
    try:
        with open(path, "r", errors="surrogateescape") as f:
            src = f.read()
    except OSError as e:
        for label, _, _ in edits:
            print("  FAIL %s [%s]: %s" % (relpath, label, e))
            failed += 1
        return

    orig = src
    for label, old, new in edits:
        n = src.count(old)
        if n != 1:
            print("  FAIL %s [%s]: anchor found %d times (need exactly 1)"
                  % (relpath, label, n))
            failed += 1
            continue
        src = src.replace(old, new, 1)
        print("  OK   %s [%s]" % (relpath, label))
        applied += 1

    if src != orig:
        with open(path, "w", errors="surrogateescape") as f:
            f.write(src)


print("=" * 62)
print("VA48 BEACON PATCHER  (round 6: fat grouped bands + panic marker)")
print("=" * 62)

# ------------------------------------------------------------------ head.S CP0
# MMU and D-cache are still off here, so a plain store goes straight to DRAM
# and the DPU sees it immediately. x0-x3 carry the boot protocol arguments and
# must not be touched; x4-x6 are free.
HEAD_OLD = "SYM_CODE_START(primary_entry)\n\tbl\tpreserve_boot_args\n"
HEAD_NEW = """SYM_CODE_START(primary_entry)
	/*
	 * VA48 BEACON CP0: paint the first band of the continuous-splash
	 * framebuffer. MMU and caches are still off, so this store lands in
	 * DRAM directly and the DPU (still scanning out the bootloader splash)
	 * shows it right away. This is the only diagnostic channel that works
	 * this early on this device.
	 *
	 * 128 KiB (~26 rows) -- deliberately thicker than the ~19-row bands
	 * that follow, so "the mechanism works" is unmistakable, while still
	 * leaving a gap before the CP1 band at +160 KiB.
	 *
	 * x0-x3 hold the boot arguments - only x4-x6 are used here.
	 */
	mov_q	x4, %s
	add	x6, x4, #%s
	mov	x5, #-1
0:	stp	x5, x5, [x4], #16
	cmp	x4, x6
	b.lo	0b
	dsb	sy

	bl	preserve_boot_args
""" % (SPLASH_BASE_ASM, CP0_BAND_ASM)

patch("arch/arm64/kernel/head.S", [("CP0 raw-phys splash band", HEAD_OLD, HEAD_NEW)])

# -------------------------------------------------------- setup.c : the helper
SETUP_FN_OLD = "void __init __no_sanitize_address setup_arch(char **cmdline_p)\n"
SETUP_FN_NEW = """/*
 * VA48 BEACON
 *
 * Paint band `cp' of the continuous-splash framebuffer to signal that boot
 * checkpoint `cp' was reached. See other_patch/va48/patch_beacon.py for the
 * full rationale: this device has no console, no surviving ramoops and no
 * framebuffer console, so the panel is the only output channel for a boot
 * that dies before userspace.
 *
 * splash_region is reserved but has no `no-map', so it stays in
 * memblock.memory and is covered by the linear map; phys_to_virt() is
 * therefore valid for it once paging_init() has run. Before that we go
 * through early_ioremap.
 *
 * Bands are grouped four-at-a-time: slot(cp) = cp + cp/4 skips one slot after
 * every fourth band, so the reader counts groups instead of twenty stripes.
 *
 * There are deliberately SEPARATE early/late functions rather than one with a
 * runtime flag. early_ioremap()/early_iounmap() live in .init.text, so a
 * single non-init function referencing them is a section mismatch -- and this
 * kernel builds section mismatches as hard ERRORS, not warnings. Splitting
 * removes the bad reference instead of suppressing it with __ref:
 *
 *   va48_beacon_early()  __init, early_ioremap  -- CP1..CP2, called from
 *                        setup_arch() which is itself __init (legal)
 *   va48_beacon()        non-init, linear map   -- CP3..CP19, must NOT be
 *                        __init because CP19 runs after free_initmem()
 */
#define VA48_SPLASH_BASE	%s
#define VA48_STRIDE		0x28000UL	/* 160 KiB slot pitch  */
#define VA48_BAND		0x18000UL	/*  96 KiB painted     */
#define VA48_PANIC_SLOT		25
#define VA48_PANIC_BAND		0x50000UL	/* 320 KiB, ~65 rows   */

/* Set once paging_init() has run; guards the panic marker's linear access. */
bool va48_beacon_linear __read_mostly;

static void va48_beacon_paint(void *va, unsigned long len)
{
	memset(va, 0xff, len);
	dcache_clean_inval_poc((unsigned long)va, (unsigned long)va + len);
}

static phys_addr_t va48_beacon_pa(int slot)
{
	return (phys_addr_t)VA48_SPLASH_BASE + (phys_addr_t)slot * VA48_STRIDE;
}

/* Group gaps: one slot skipped after every fourth band. */
static int va48_beacon_slot(int cp)
{
	return cp + cp / 4;
}

/* CP1..CP2: before paging_init(), the linear map is not usable yet. */
void __init va48_beacon_early(int cp)
{
	phys_addr_t pa = va48_beacon_pa(va48_beacon_slot(cp));
	void __iomem *io = early_ioremap(pa, VA48_BAND);

	if (!io)
		return;
	memset_io(io, 0xff, VA48_BAND);
	early_iounmap(io, VA48_BAND);
}

/*
 * CP3..CP19: linear map is live. Not __init -- CP19 executes after
 * free_initmem(), so marking this __init would be a use-after-free.
 */
void va48_beacon(int cp)
{
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(va48_beacon_slot(cp))),
			  VA48_BAND);
}

/*
 * Called from panic(). A much thicker block, well below the band cluster:
 * present => the kernel reached panic(); absent => a true hang. Guarded
 * because panic() can fire before paging_init().
 */
void va48_beacon_panic(void)
{
	if (!va48_beacon_linear)
		return;
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(VA48_PANIC_SLOT)),
			  VA48_PANIC_BAND);
}

void __init __no_sanitize_address setup_arch(char **cmdline_p)
""" % SPLASH_BASE_C

patch("arch/arm64/kernel/setup.c", [
    # early_ioremap()/early_iounmap()/memset_io() live behind linux/io.h, which
    # setup.c does not include on its own.
    ("linux/io.h include",
     "#include <linux/mm.h>\n",
     "#include <linux/mm.h>\n#include <linux/io.h>\n"),

    ("beacon helpers + defines", SETUP_FN_OLD, SETUP_FN_NEW),

    # CP1 -- early fixmap / early ioremap are up (proves patch E1)
    ("CP1 after early_ioremap_init",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n\tva48_beacon_early(1);\n"),

    # CP2 before paging_init (proves B1), CP3 after it (proves C1/D1/F1/I1).
    # va48_beacon_linear must be armed here, before any CP3+ / panic use.
    ("CP2+CP3 around paging_init",
     "\tarm64_memblock_init();\n\n\tpaging_init();\n",
     "\tarm64_memblock_init();\n\tva48_beacon_early(2);\n\n\tpaging_init();\n"
     "\tva48_beacon_linear = true;\n\tva48_beacon(3);\n"),

    # CP4 after bootmem_init (vmemmap is populated by now)
    ("CP4 after bootmem_init",
     "\tbootmem_init();\n",
     "\tbootmem_init();\n\tva48_beacon(4);\n"),

    # CP5 -- rest of setup_arch reached
    ("CP5 after request_standard_resources",
     "\trequest_standard_resources();\n",
     "\trequest_standard_resources();\n\tva48_beacon(5);\n"),
])

# ------------------------------------------------------------------ init/main.c
patch("init/main.c", [
    ("beacon extern decl",
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "void va48_beacon(int cp);\n\n"
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n"),

    # CP6 -- buddy + slab are up
    ("CP6 after mm_init",
     "\tmm_init();\n",
     "\tmm_init();\n\tva48_beacon(6);\n"),

    # CP7 -- VFS is up
    ("CP7 after vfs_caches_init",
     "\tvfs_caches_init();\n",
     "\tvfs_caches_init();\n\tva48_beacon(7);\n"),

    # CP8 -- start_kernel ran to completion
    ("CP8 before arch_call_rest_init",
     "\tarch_call_rest_init();\n",
     "\tva48_beacon(8);\n\tarch_call_rest_init();\n"),

    # CP9 -- early initcalls done
    ("CP9 after do_pre_smp_initcalls",
     "\tdo_pre_smp_initcalls();\n",
     "\tdo_pre_smp_initcalls();\n\tva48_beacon(9);\n"),

    # CP10 -- secondary CPUs are up and the scheduler knows about them
    ("CP10 after sched_init_smp",
     "\tsmp_init();\n\tsched_init_smp();\n",
     "\tsmp_init();\n\tsched_init_smp();\n\tva48_beacon(10);\n"),

    # CP11 -- driver core initialised (before any probe runs)
    ("CP11 after driver_init",
     "\tcpuset_init_smp();\n\tdriver_init();\n",
     "\tcpuset_init_smp();\n\tdriver_init();\n\tva48_beacon(11);\n"),

    # CP12..CP18 -- one band per initcall level. This is the subdivision round 5
    # lacked: vendor driver probes run here, and a hang inside one of them was
    # indistinguishable from a hang anywhere else in kernel_init_freeable().
    ("CP12-18 per initcall level",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n\t}\n",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n"
     "\t\t/* VA48 BEACON CP12..CP18: band per completed initcall level */\n"
     "\t\tva48_beacon(12 + level);\n\t}\n"),

    # CP19 -- kernel side is fully done; anything after this is userspace
    ("CP19 before userspace init",
     "\tif (ramdisk_execute_command) {\n",
     "\tva48_beacon(19);\n\tif (ramdisk_execute_command) {\n"),
])

# ----------------------------------------------------------------- kernel/panic.c
# The panic discriminator. Declared behind CONFIG_ARM64 so the generic file
# stays buildable on other architectures.
patch("kernel/panic.c", [
    ("panic beacon decl",
     "void __weak panic_smp_self_stop(void)\n",
     "/*\n"
     " * VA48 BEACON: defined in arch/arm64/kernel/setup.c. Paints a thick block\n"
     " * on the splash framebuffer so a panic can be told apart from a pure hang\n"
     " * on a device with no usable console. See other_patch/va48/patch_beacon.py.\n"
     " */\n"
     "#ifdef CONFIG_ARM64\n"
     "void va48_beacon_panic(void);\n"
     "#else\n"
     "static inline void va48_beacon_panic(void) { }\n"
     "#endif\n\n"
     "void __weak panic_smp_self_stop(void)\n"),

    ("panic beacon call",
     "\tbool _crash_kexec_post_notifiers = crash_kexec_post_notifiers;\n",
     "\tbool _crash_kexec_post_notifiers = crash_kexec_post_notifiers;\n\n"
     "\tva48_beacon_panic();\n"),
])

print("-" * 62)
print("applied=%d expected=%d failed=%d" % (applied, EXPECT, failed))
if failed or applied != EXPECT:
    print("BEACON PATCH FAILED - refusing to continue")
    sys.exit(1)
print("beacon OK: CP0 raw-phys, CP1-2 early_ioremap, CP3-19 linear map")
print("splash base %s, stride 160 KiB, band 96 KiB, groups of 4"
      % SPLASH_BASE_C)
print("CP0 = 128 KiB anchor; panic marker = slot 25, 320 KiB")
