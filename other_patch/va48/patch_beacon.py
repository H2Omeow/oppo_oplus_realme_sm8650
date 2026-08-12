#!/usr/bin/env python3
"""
VA48 BEACON PATCHER  (round 7)
==============================

ROUND 6 RESULT (confirmed on real hardware)
-------------------------------------------
All 21 bands lit -- "five groups plus one thin band" -- and the thin band was
NOT the thick panic block, and the device hung before the watchdog reset it.
That is a much stronger result than any previous round:

  * CP20 sits in kernel_init() AFTER free_initmem(), mark_readonly(),
    pti_finalize() and system_state = SYSTEM_RUNNING, immediately before
    run_init_process("/init").  So the whole kernel-side VA48 decoupling works:
    4-level pagetables, secondary CPUs, all eight initcall levels, every vendor
    module probe, prepare_namespace() (root mount), and the initmem
    reclaim/read-only transition.
  * The panic block was ARMED at that point (va48_beacon_linear is set to true
    on the line before va48_beacon(3), and CP3 was seen), so its absence really
    means panic() was never reached.  Combined with PANIC_TIMEOUT=0 -- which
    makes a real panic hang forever rather than reboot -- the hang-then-watchdog
    behaviour confirms a TRUE HANG, not a panic.
  * That rules out every panic-based failure: "No working init found",
    "VFS: Unable to mount root fs", "Attempted to kill init!", and any
    oops promoted by PANIC_ON_OOPS.

WHAT ROUND 7 ADDS
-----------------
The remaining unknown is a pure hang somewhere at or after kernel_execve() of
/init, taking no panic path.  Round 7 splits that into mutually exclusive
outcomes with four new bands:

    CP21  kernel_execve() RETURNED  -> exec of /init failed
    CP22  first ever el0 syscall    -> userspace ran >=1 instruction (KEY BIT)
    CP23  4096th syscall            -> userspace is genuinely running
    CP24  arm64_force_sig_fault()   -> a fatal signal reached userspace

A custom minimal /init was considered and rejected: CONFIG_DEVMEM is not set,
so a userspace helper cannot mmap /dev/mem to paint bands, and it would
therefore be less observable than the kernel-side probes above.

CP24 is deliberately hooked at arm64_force_sig_fault(), NOT at el0_da/el0_ia.
Ordinary demand paging traverses those entry points thousands of times per
second, so they would light unconditionally and carry no information.

READOUT CHANGE: TWO CLUSTERS, NOT MORE STRIPES
----------------------------------------------
25 bands in groups of four would read as "6 groups plus 1" against round 6's
"5 groups plus 1" -- far too easy to confuse, and round 5 already proved that
asking for a fine-grained count is unreliable.  Instead CP0..CP20 keep
byte-identical slots to round 6 and the userspace bands form a SECOND cluster
143 rows lower (vs 46 rows between groups and 13 within a group).  The reader
counts bands in the bottom cluster only, 0..4.  The top cluster doubles as a
regression control: it must reproduce the round-6 photo exactly, otherwise the
hot-path hook in do_el0_svc broke something.

HOT PATH CAVEAT
---------------
CP22/CP23 hook do_el0_svc(), the hottest path in the kernel.  The counter is
atomic (a lost update on a non-atomic counter could step past an "== threshold"
test, suppressing CP23 and INVERTING the diagnosis), and the hook disarms
itself permanently once CP23 fires, after which it is one predicted-taken
branch.  If round 7 misbehaves in a way round 6 did not, this hook is the first
suspect.

ROUND 6 NOTES BELOW (still accurate)
====================================

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
    PANIC     slot 27, 0x50000 (320 KiB, ~65 rows)

    Highest band ends at ~4.0 MiB, the panic block at ~4.6 MiB.  Round 5
    proved at least the first 2.8 MiB is really scanned out; a full
    1264x2780x4 framebuffer is 13.4 MiB and even a 1080x1920x4 one is 7.9 MiB,
    so 4.6 MiB is inside either.

MAPPING METHOD PER PHASE
------------------------
    CP0        head.S, MMU + D-cache still OFF -> raw physical store
    CP1..CP2   early_ioremap (valid from early_ioremap_init() until
               early_ioremap_reset(); 96 KiB = 24 pages <= NR_FIX_BTMAPS=64)
    CP3..CP20  phys_to_virt + dcache_clean_inval_poc (valid once paging_init()
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
    CP12..CP19  after initcall level 0..7    vendor driver probes live here;
                                             this is where round 5 was blind
    CP20  about to exec userspace init       kernel side fully OK

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

EXPECT = 25
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
print("VA48 BEACON PATCHER  (round 8: variable-width + reboot probes)")
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
 *   va48_beacon()        non-init, linear map   -- CP3..CP20, must NOT be
 *                        __init because CP20 runs after free_initmem()
 */
#define VA48_SPLASH_BASE	%s
#define VA48_STRIDE		0x28000UL	/* 160 KiB slot pitch  */
#define VA48_BAND		0x18000UL	/*  96 KiB painted     */

/*
 * ROUND 8 GEOMETRY
 *
 * Round 7 reported "two bands in the lower cluster" and that turned out to be
 * ambiguous: CP21+CP22 (exec of /init FAILED, a fallback init took over) and
 * CP22+CP23 (/init ran fine and userspace was busy) differ by 32 rows of black,
 * which is not something anyone can judge from a photo. That was a design
 * defect -- identical-looking bands carrying opposite meanings.
 *
 * Round 8 fixes it structurally: every band below the kernel cluster has its
 * own THICKNESS. Identity no longer depends on counting or on judging gap
 * sizes, only on "how thick is it", which is robust in a photograph.
 *
 *   kernel cluster     CP0..CP20  slots 0..25, VA48_STRIDE pitch, 96 KiB bands
 *                      >>> byte-identical to r6/r7: regression control <<<
 *   -- ~50 rows black --
 *   progress cluster   CP21..CP24  thickness 1x/2x/3x/4x of VA48_UBASE
 *   -- ~50 rows black --
 *   reboot cluster     CP25..CP27  thickness 1x/2x/3x  (who triggered reboot)
 *   -- ~50 rows black --
 *   panic block        5x -- still the thickest thing on screen
 *
 * Bottom lands at 6.94 MiB, leaving ~0.97 MiB of margin under the pessimistic
 * 7.91 MiB framebuffer estimate. Margin matters: if the panic block fell off
 * screen, "no panic" and "not visible" would be indistinguishable, which is
 * exactly the round-5 failure mode that produces a WRONG diagnosis rather than
 * merely a missing data point.
 */
#define VA48_US_FIRST_CP	21
#define VA48_UBASE		0x14000UL	/*  80 KiB, ~16 rows   */

/*
 * Explicit (offset, length) table rather than a stride formula: thicknesses
 * differ, so a uniform pitch cannot express this layout without either wasting
 * vertical space or letting a thick band overlap the next slot.
 */
static const struct {
	unsigned long off;
	unsigned long len;
} va48_us_band[] = {
	{ 0x0440000UL, 0x014000UL },	/* CP21 1x  exec /init failed        */
	{ 0x0468000UL, 0x028000UL },	/* CP22 2x  userspace first syscall  */
	{ 0x04a4000UL, 0x03c000UL },	/* CP23 3x  userspace ran 4096 calls */
	{ 0x04f4000UL, 0x050000UL },	/* CP24 4x  fatal signal delivered   */
	{ 0x0598000UL, 0x014000UL },	/* CP25 1x  reboot() syscall         */
	{ 0x05c0000UL, 0x028000UL },	/* CP26 2x  kernel_restart()         */
	{ 0x05fc000UL, 0x03c000UL },	/* CP27 3x  emergency_restart()      */
};

#define VA48_PANIC_OFF		0x068c000UL
#define VA48_PANIC_BAND		0x064000UL	/* 5x, ~81 rows        */

/* Set once paging_init() has run; guards the panic marker's linear access. */
bool va48_beacon_linear __read_mostly;

static void va48_beacon_paint(void *va, unsigned long len)
{
	memset(va, 0xff, len);
	dcache_clean_inval_poc((unsigned long)va, (unsigned long)va + len);
}

static phys_addr_t va48_beacon_pa(unsigned long off)
{
	return (phys_addr_t)VA48_SPLASH_BASE + (phys_addr_t)off;
}

/* Kernel cluster: uniform pitch, one slot skipped after every fourth band. */
static unsigned long va48_beacon_koff(int cp)
{
	return (unsigned long)(cp + cp / 4) * VA48_STRIDE;
}

/*
 * Resolve a checkpoint to (offset, length). Kernel checkpoints keep the r6/r7
 * stride formula so the top cluster is unchanged; CP21+ come from the explicit
 * thickness table above.
 */
static void va48_beacon_where(int cp, unsigned long *off, unsigned long *len)
{
	if (cp >= VA48_US_FIRST_CP) {
		int i = cp - VA48_US_FIRST_CP;

		if (i >= (int)ARRAY_SIZE(va48_us_band)) {	/* cannot happen */
			*off = va48_us_band[0].off;
			*len = va48_us_band[0].len;
			return;
		}
		*off = va48_us_band[i].off;
		*len = va48_us_band[i].len;
		return;
	}
	*off = va48_beacon_koff(cp);
	*len = VA48_BAND;
}

/* CP1..CP2: before paging_init(), the linear map is not usable yet. */
void __init va48_beacon_early(int cp)
{
	unsigned long off, len;
	void __iomem *io;

	va48_beacon_where(cp, &off, &len);
	io = early_ioremap(va48_beacon_pa(off), len);
	if (!io)
		return;
	memset_io(io, 0xff, len);
	early_iounmap(io, len);
}

/*
 * CP3..CP20: linear map is live. Not __init -- CP20 executes after
 * free_initmem(), so marking this __init would be a use-after-free.
 */
void va48_beacon(int cp)
{
	unsigned long off, len;

	va48_beacon_where(cp, &off, &len);
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(off)), len);
}

/*
 * CP22/CP23: userspace liveness, driven from the syscall entry path.
 *
 * do_el0_svc() is the hottest path in the kernel, so this must cost almost
 * nothing once armed. After the CP23 threshold is crossed, va48_svc_count stops
 * being incremented and the whole hook is a single load-compare-branch that is
 * always predicted not-taken.
 *
 * CP22 firing at all is the single most valuable bit of round 7: it proves
 * userspace executed at least one instruction and successfully trapped back
 * into the kernel. Nothing before round 7 could establish that.
 */
#define VA48_SVC_ALIVE		4096	/* CP23 threshold */

/*
 * Deliberately atomic. do_el0_svc() runs concurrently on every CPU, and a
 * non-atomic read-modify-write here loses updates -- which would let the count
 * step straight past an "== VA48_SVC_ALIVE" test without ever equalling it. CP23
 * would then never paint, and an absent CP23 reads as "userspace died early":
 * the diagnosis would be inverted by a data race. Thresholds are therefore
 * ">=", and each band has its own one-shot flag so a lost update can only
 * delay a band, never suppress it.
 */
static atomic_t va48_svc_count = ATOMIC_INIT(0);
static bool va48_svc_first, va48_svc_alive;

void va48_beacon_svc(void)
{
	int n;

	if (likely(va48_svc_alive))
		return;

	n = atomic_inc_return(&va48_svc_count);

	if (unlikely(!va48_svc_first)) {
		va48_svc_first = true;
		va48_beacon(22);		/* userspace ran and trapped back */
	}
	if (unlikely(n >= VA48_SVC_ALIVE)) {
		va48_svc_alive = true;		/* disarms the hook for good */
		va48_beacon(23);		/* userspace is really running */
	}
}

/*
 * CP24: a fatal signal was delivered to userspace (SIGSEGV/SIGBUS/SIGILL from
 * arm64_force_sig_fault). Deliberately NOT hooked at el0_da/el0_ia: ordinary
 * demand paging traverses those thousands of times per second, so they carry no
 * signal at all. Reaching force_sig means the fault could not be resolved.
 *
 * Painted once; repeated faults must not repaint (cheap, and keeps the photo
 * meaning "at least one" rather than "many").
 */
void va48_beacon_sigfault(void)
{
	static bool done;

	if (done)
		return;
	done = true;
	va48_beacon(24);
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
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(VA48_PANIC_OFF)),
			  VA48_PANIC_BAND);
}

/*
 * CP25..CP27: who actually triggered the reboot.
 *
 * Round 7 established that userspace runs (CP22/CP23 lit), does real work, and
 * then the device reboots after roughly ten seconds -- far too quick for the
 * ~32 s PMIC watchdog seen in round 6, and impossible for a panic, since
 * PANIC_TIMEOUT=0 makes a panic hang forever instead of resetting. Something is
 * therefore asking for a reboot on purpose, and these three bands say which
 * layer did it:
 *
 *   CP25  reboot() syscall     userspace decided to reboot (Android init:
 *                              dm-verity/AVB failure, a critical mount failing,
 *                              or a critical service crash-looping)
 *   CP26  kernel_restart()     an orderly kernel-side restart
 *   CP27  emergency_restart()  the violent path, no syscore shutdown
 *
 * CP25 is the informative one: it moves the fault out of the kernel entirely
 * and into Android's own boot policy, which is a different (and far more
 * tractable) class of problem than a VA48 mapping bug.
 *
 * These paths run with the linear map live, but the guard is kept because
 * emergency_restart() can in principle be reached from very early code.
 */
void va48_beacon_reboot(int cp)
{
	if (!va48_beacon_linear)
		return;
	va48_beacon(cp);
}

void __init __no_sanitize_address setup_arch(char **cmdline_p)
""" % SPLASH_BASE_C

patch("arch/arm64/kernel/setup.c", [
    # early_ioremap()/early_iounmap()/memset_io() live behind linux/io.h, which
    # setup.c does not include on its own.
    # early_ioremap()/early_iounmap()/memset_io() need linux/io.h, and the CP22
    # counter needs atomic_t. Both are pulled in indirectly via linux/kernel.h in
    # practice, but relying on transitive includes is exactly the kind of
    # assumption that breaks on the runner rather than here.
    ("linux/io.h + atomic.h includes",
     "#include <linux/mm.h>\n",
     "#include <linux/mm.h>\n#include <linux/io.h>\n#include <linux/atomic.h>\n"),

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

    # CP12..CP19 -- one band per initcall level. This is the subdivision round 5
    # lacked: vendor driver probes run here, and a hang inside one of them was
    # indistinguishable from a hang anywhere else in kernel_init_freeable().
    ("CP12-18 per initcall level",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n\t}\n",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n"
     "\t\t/* VA48 BEACON CP12..CP19: band per completed initcall level */\n"
     "\t\tva48_beacon(12 + level);\n\t}\n"),

    # CP19 -- kernel side is fully done; anything after this is userspace
    ("CP20 before userspace init",
     "\tif (ramdisk_execute_command) {\n",
     "\tva48_beacon(20);\n\tif (ramdisk_execute_command) {\n"),

    # CP21 -- kernel_execve() RETURNED, i.e. exec of /init failed. This is the
    # opposite signal to CP22: on a successful exec, run_init_process() never
    # returns, so CP21 stays dark and CP22 lights instead. Seeing CP21 means the
    # ELF loader / mm switch / user pagetable setup rejected the binary, which
    # under VA48 is a live suspect (TASK_SIZE, mmap base, stack placement).
    ("CP21 after ramdisk exec failed",
     "\t\tpr_err(\"Failed to execute %s (error %d)\\n\",\n"
     "\t\t       ramdisk_execute_command, ret);\n",
     "\t\tva48_beacon(21);\n"
     "\t\tpr_err(\"Failed to execute %s (error %d)\\n\",\n"
     "\t\t       ramdisk_execute_command, ret);\n"),
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

# ------------------------------------------------ arch/arm64/kernel/syscall.c
# CP22/CP23: the syscall entry hook. This is the one probe that touches a hot
# path, and it is the reason round 7 exists: nothing else can prove that
# userspace executed an instruction.
patch("arch/arm64/kernel/syscall.c", [
    ("svc beacon decl",
     "void do_el0_svc(struct pt_regs *regs)\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "void va48_beacon_svc(void);\n\n"
     "void do_el0_svc(struct pt_regs *regs)\n"),

    ("CP22+CP23 at do_el0_svc",
     "void do_el0_svc(struct pt_regs *regs)\n{\n\tfp_user_discard();\n",
     "void do_el0_svc(struct pt_regs *regs)\n{\n"
     "\tva48_beacon_svc();\n"
     "\tfp_user_discard();\n"),
])

# -------------------------------------------------- arch/arm64/kernel/traps.c
# CP24: a fatal signal actually reached userspace. NOT hooked at el0_da/el0_ia,
# which ordinary demand paging traverses constantly.
patch("arch/arm64/kernel/traps.c", [
    ("sigfault beacon decl + CP24 call",
     "void arm64_force_sig_fault(int signo, int code, unsigned long far,\n"
     "\t\t\t   const char *str)\n"
     "{\n"
     "\tarm64_show_signal(signo, str);\n",

     "/* VA48 BEACON: defined in this directory's setup.c */\n"
     "void va48_beacon_sigfault(void);\n\n"
     "void arm64_force_sig_fault(int signo, int code, unsigned long far,\n"
     "\t\t\t   const char *str)\n"
     "{\n"
     "\tva48_beacon_sigfault();\n"
     "\tarm64_show_signal(signo, str);\n"),
])

# ---------------------------------------------------------------- kernel/reboot.c
# CP25..CP27: who triggered the reboot.
#
# Round 7 showed userspace ran (CP22+CP23 lit) then rebooted ~10 s later.
# PANIC_TIMEOUT=0 makes a panic hang forever -- so the reboot must have been
# requested deliberately. These bands say which layer asked for it:
#
#   CP25  reboot() syscall entry   userspace (Android init) requested reboot
#   CP26  kernel_restart()         orderly kernel-side restart
#   CP27  emergency_restart()      violent path, no syscore shutdown
#
# Typical Android init reboot: CP25 fires first (syscall entry), then CP26
# fires when kernel_restart() is called by the syscall handler.  A kernel-
# initiated orderly restart skips CP25 and only fires CP26.  Emergency restart
# fires only CP27.
#
# All three bands have distinct thicknesses in the r8 layout (1x / 2x / 3x of
# VA48_UBASE = 80 KiB), so they are unambiguous in a photograph.
patch("kernel/reboot.c", [
    # emergency_restart() is defined before kernel_restart_prepare() in this OEM
    # kernel (confirmed by build error: line 79 vs 88). The declaration must be
    # placed before emergency_restart(), not before kernel_restart_prepare().
    # Combine the decl with the CP27 hook so one anchor covers both.
    ("CP27 + reboot beacon decl at emergency_restart",
     "void emergency_restart(void)\n{\n\tkmsg_dump(KMSG_DUMP_EMERG);\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "#ifdef CONFIG_ARM64\n"
     "void va48_beacon_reboot(int cp);\n"
     "#else\n"
     "static inline void va48_beacon_reboot(int cp) { }\n"
     "#endif\n\n"
     "void emergency_restart(void)\n{\n"
     "\tva48_beacon_reboot(27);\n"
     "\tkmsg_dump(KMSG_DUMP_EMERG);\n"),

    # CP26: orderly kernel-side restart. Covers both the reboot-syscall path
    # and any direct kernel-initiated restart.
    ("CP26 at kernel_restart",
     "void kernel_restart(char *cmd)\n{\n\tkernel_restart_prepare(cmd);\n",
     "void kernel_restart(char *cmd)\n{\n"
     "\tva48_beacon_reboot(26);\n"
     "\tkernel_restart_prepare(cmd);\n"),

    # CP25: userspace called the reboot() syscall.  Fires before the lock.
    ("CP25 at reboot syscall",
     "\tmutex_lock(&system_transition_mutex);\n\tswitch (cmd) {\n",
     "\tva48_beacon_reboot(25);\n"
     "\tmutex_lock(&system_transition_mutex);\n\tswitch (cmd) {\n"),
])

print("-" * 62)
print("applied=%d expected=%d failed=%d" % (applied, EXPECT, failed))
if failed or applied != EXPECT:
    print("BEACON PATCH FAILED - refusing to continue")
    sys.exit(1)
print("beacon OK: CP0 raw-phys, CP1-2 early_ioremap, CP3-27 linear map")
print("splash base %s, stride 160 KiB (kernel cluster)" % SPLASH_BASE_C)
print("kernel cluster CP0-CP20 slots 0-25 (groups of 4, identical to r6/r7)")
print("progress cluster CP21-CP24 variable thickness (1x/2x/3x/4x of 80 KiB)")
print("reboot cluster  CP25-CP27 variable thickness (1x/2x/3x of 80 KiB)")
print("panic block     offset 0x68c000, 5x thickness (~81 rows)")
