#!/usr/bin/env python3
"""
VA48 BEACON PATCHER  (round 14)
================================

CHANGES VS r12
--------------
1. panic_timeout fix  : va48_fix_cmdline() now sets `panic_timeout = 0`
   directly in C instead of appending "panic=0" to boot_command_line.
   Rationale: if "panic=" is already present in the bootloader cmdline,
   strstr() finds it and the old code skips the append — so the device
   keeps rebooting on panic.  Worse, even a successful append is only
   parsed by parse_args() AFTER setup_arch() returns, which is too late
   for crashes inside paging_init().  Setting panic_timeout directly
   overrides CONFIG_PANIC_TIMEOUT=-1 immediately.

2. Early cmdline fix  : va48_fix_cmdline() is now called at CP1 (right
   after early_ioremap_init, before arm64_memblock_init / paging_init)
   instead of CP5.  panic_timeout=0 is therefore in effect for any crash
   during paging_init.

3. paging_init sub-beacons : three new bands inside paging_init() via
   va48_beacon_paginit() (arch/arm64/mm/mmu.c patch):
     sub 0 @ 0x092000 : before map_kernel()
     sub 1 @ 0x0a4000 : after  map_kernel(), before map_mem()
     sub 2 @ 0x0b6000 : after  map_mem()
   With panic_timeout=0 active a crash in paging_init now hangs the
   device showing exactly which sub-CP was last painted.
"""

import sys
import os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

SPLASH_BASE_C   = "0xd5100000UL"
SPLASH_BASE_ASM = "0xd5100000"
CP0_BAND_ASM    = "0x20000"

EXPECT  = 29
applied = 0
failed  = 0


def patch(relpath, edits):
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
print("VA48 BEACON PATCHER  (round 14: paginit sub-beacons)")
print("=" * 62)

# ------------------------------------------------------------------ head.S CP0
HEAD_OLD = "SYM_CODE_START(primary_entry)\n\tbl\tpreserve_boot_args\n"
HEAD_NEW = """SYM_CODE_START(primary_entry)
\t/*
\t * VA48 BEACON CP0: paint the first band of the continuous-splash
\t * framebuffer. MMU and caches are still off, so this store lands in
\t * DRAM directly and the DPU shows it right away.
\t * x0-x3 hold the boot arguments - only x4-x6 are used here.
\t */
\tmov_q\tx4, %s
\tadd\tx6, x4, #%s
\tmov\tx5, #-1
0:\tstp\tx5, x5, [x4], #16
\tcmp\tx4, x6
\tb.lo\t0b
\tdsb\tsy

\tbl\tpreserve_boot_args
""" % (SPLASH_BASE_ASM, CP0_BAND_ASM)

patch("arch/arm64/kernel/head.S",
      [("CP0 raw-phys splash band", HEAD_OLD, HEAD_NEW)])


# -------------------------------------------------------- setup.c : the helper
SETUP_FN_OLD = "void __init __no_sanitize_address setup_arch(char **cmdline_p)\n"
SETUP_FN_NEW = r"""/*
 * VA48 BEACON  (round 14: paginit sub-beacons + direct panic_timeout fix)
 *
 * KEY CHANGES vs r12:
 *  1. va48_fix_cmdline() sets panic_timeout=0 directly in C, overriding
 *     CONFIG_PANIC_TIMEOUT=-1 before parse_args() runs.
 *  2. va48_fix_cmdline() called at CP1 (before paging_init) not CP5.
 *  3. va48_beacon_paginit(sub) paints sub-bands inside paging_init().
 */
#include <linux/font.h>

#define VA48_SPLASH_BASE	""" + SPLASH_BASE_C + r"""
#define VA48_STRIDE		0x28000UL	/* 160 KiB slot pitch  */
#define VA48_BAND		0x18000UL	/*  96 KiB painted     */
#define VA48_US_FIRST_CP	21
#define VA48_UBASE		0x14000UL	/*  80 KiB, ~16 rows   */

/* Explicit (offset, length) table for the variable-thickness lower bands. */
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
	{ 0x0640000UL, 0x014000UL },	/* CP28 1x  RESTART (bare restart)   */
	{ 0x065c000UL, 0x028000UL },	/* CP29 2x  RESTART2 (with reason)   */
};

#define VA48_PANIC_OFF		0x06c0000UL	/* PANIC band start offset */
#define VA48_PANIC_BAND		0x064000UL	/* 5x, ~81 fb rows         */

/*
 * Text region: starts 0x10000 (16 fb rows) after the PANIC band, on a
 * black-cleared background.  Written BEFORE the PANIC band so partial
 * output is detectable (absent PANIC band = render was interrupted).
 *
 *   font_vga_8x16: 8 px wide, 16 px tall per glyph
 *   SCALE 3       -> 24 px wide, 48 px tall on screen
 *   columns       = 1264 / 24 = 52
 *   max rows      = 8   -> 416 characters, 384 fb rows of text
 */
#define VA48_TEXT_OFF		(VA48_PANIC_OFF + VA48_PANIC_BAND + 0x10000UL)
#define VA48_TEXT_SCALE		3
#define VA48_FB_W		1264
#define VA48_FB_H		2780
#define VA48_FB_STRIDE		(VA48_FB_W * 4)
#define VA48_TEXT_ROWS		8

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

static unsigned long va48_beacon_koff(int cp)
{
	return (unsigned long)(cp + cp / 4) * VA48_STRIDE;
}

static void va48_beacon_where(int cp, unsigned long *off, unsigned long *len)
{
	if (cp >= VA48_US_FIRST_CP) {
		int i = cp - VA48_US_FIRST_CP;

		if (i >= (int)ARRAY_SIZE(va48_us_band)) {
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

void va48_beacon(int cp)
{
	unsigned long off, len;

	va48_beacon_where(cp, &off, &len);
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(off)), len);
}

#define VA48_SVC_ALIVE		4096
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
		va48_beacon(22);
	}
	if (unlikely(n >= VA48_SVC_ALIVE)) {
		va48_svc_alive = true;
		va48_beacon(23);
	}
}

void va48_beacon_sigfault(void)
{
	static bool done;

	if (done)
		return;
	done = true;
	va48_beacon(24);
}

/*
 * va48_render_panic - render the panic string as white-on-black bitmap text.
 *
 * Uses font_vga_8x16 (8x16 glyphs, VA48_TEXT_SCALE=3 -> 24x48 px/char).
 * Written BEFORE the PANIC band so partial completion is diagnosable:
 * absent PANIC band = text rendering was interrupted by the watchdog.
 */
static void va48_render_panic(const char *msg)
{
	const struct font_desc *font = &font_vga_8x16;
	const u8   *fdata;
	const char *p;
	int fw, fh, cw, ch, cols;
	int x, row_top;
	unsigned long text_off  = VA48_TEXT_OFF;
	unsigned long clear_len;
	void *clear_va;

	if (!msg || !font || !font->data)
		return;

	fdata = (const u8 *)font->data;
	fw   = (int)font->width;
	fh   = (int)font->height;
	cw   = fw * VA48_TEXT_SCALE;
	ch   = fh * VA48_TEXT_SCALE;
	cols = VA48_FB_W / cw;

	/* Pre-clear the text area to black. */
	clear_len = (unsigned long)(VA48_TEXT_ROWS * ch + 16) * VA48_FB_STRIDE;
	clear_va  = (void *)phys_to_virt(va48_beacon_pa(text_off));
	memset(clear_va, 0, clear_len);
	dcache_clean_inval_poc((unsigned long)clear_va,
			       (unsigned long)clear_va + clear_len);
	touch_nmi_watchdog();

	x       = 0;
	row_top = (int)(text_off / VA48_FB_STRIDE);

	for (p = msg; *p; p++) {
		unsigned char c = (unsigned char)*p;
		const u8 *glyph;
		int gy, sy, gx, sx;
		unsigned long row_flush_start, row_flush_end;

		/* Newline or line wrap */
		if (c == '\n' || x >= cols) {
			row_flush_start = VA48_SPLASH_BASE
				+ (unsigned long)row_top * VA48_FB_STRIDE;
			row_flush_end   = row_flush_start
				+ (unsigned long)ch * VA48_FB_STRIDE;
			dcache_clean_inval_poc(
				(unsigned long)phys_to_virt(row_flush_start),
				(unsigned long)phys_to_virt(row_flush_end));
			touch_nmi_watchdog();

			x = 0;
			row_top += ch;
			if (row_top + ch > VA48_FB_H)
				break;
			if (c == '\n')
				continue;
		}

		if (c < 32 || c > 126)
			c = '?';

		glyph = fdata + (unsigned int)c * (unsigned int)fh;

		for (gy = 0; gy < fh; gy++) {
			u8 row_bits = glyph[gy];

			for (sy = 0; sy < VA48_TEXT_SCALE; sy++) {
				int py = row_top + gy * VA48_TEXT_SCALE + sy;
				u32 *ln = (u32 *)phys_to_virt(
					(phys_addr_t)VA48_SPLASH_BASE +
					(phys_addr_t)py * VA48_FB_STRIDE +
					(phys_addr_t)(x * cw) * 4);

				for (gx = 0; gx < fw; gx++) {
					u32 pixel = (row_bits & (0x80u >> gx))
						    ? 0xFFFFFFFFU
						    : 0x00000000U;
					for (sx = 0; sx < VA48_TEXT_SCALE; sx++)
						ln[gx * VA48_TEXT_SCALE + sx] = pixel;
				}
			}
		}
		x++;
	}

	/* Flush the final partial row. */
	if (x > 0) {
		unsigned long s = VA48_SPLASH_BASE
			+ (unsigned long)row_top * VA48_FB_STRIDE;
		dcache_clean_inval_poc(
			(unsigned long)phys_to_virt(s),
			(unsigned long)phys_to_virt(s +
				(unsigned long)ch * VA48_FB_STRIDE));
		touch_nmi_watchdog();
	}
}

void va48_beacon_panic(const char *msg)
{
	if (!va48_beacon_linear)
		return;
	va48_render_panic(msg);
	va48_beacon_paint((void *)phys_to_virt(va48_beacon_pa(VA48_PANIC_OFF)),
			  VA48_PANIC_BAND);
}

void va48_beacon_reboot(int cp)
{
	if (!va48_beacon_linear)
		return;
	va48_beacon(cp);
}

/*
 * VA48 R9  FIX: verifiedbootstate=orange -> green
 * VA48 R14 FIX: set panic_timeout=0 directly in C (overrides
 *               CONFIG_PANIC_TIMEOUT=-1 before parse_args runs).
 *
 * Called at CP1 (after early_ioremap_init, before paging_init) so
 * panic_timeout=0 is in effect for any crash during paging_init.
 */
static void __init va48_fix_cmdline(void)
{
	extern char boot_command_line[];
	extern int panic_timeout;
	char *p = strstr(boot_command_line, "verifiedbootstate=orange");

	if (p)
		memcpy(p + 18, "green ", 6);

	/*
	 * Override CONFIG_PANIC_TIMEOUT=-1 directly so panic() spins
	 * forever instead of calling emergency_restart() immediately.
	 * parse_args() (which would parse "panic=0" from cmdline) runs
	 * only after setup_arch() returns — too late for paging_init.
	 */
	panic_timeout = 0;
}

/*
 * va48_beacon_paginit - paint a sub-band from inside paging_init().
 *
 * Three slots between the CP3 and CP4 kernel bands:
 *   sub 0 @ 0x092000 : before map_kernel()
 *   sub 1 @ 0x0a4000 : after  map_kernel(), before map_mem()
 *   sub 2 @ 0x0b6000 : after  map_mem()
 *
 * Uses early_ioremap because cpu_replace_ttbr1 has not yet run.
 */
void __init va48_beacon_paginit(int sub)
{
	static const struct {
		unsigned long off;
		unsigned long len;
	} slots[] = {
		{ 0x092000UL, 0x10000UL },
		{ 0x0a4000UL, 0x10000UL },
		{ 0x0b6000UL, 0x10000UL },
	};
	void __iomem *io;

	if ((unsigned int)sub >= ARRAY_SIZE(slots))
		return;
	io = early_ioremap(va48_beacon_pa(slots[sub].off), slots[sub].len);
	if (!io)
		return;
	memset_io(io, 0xff, slots[sub].len);
	early_iounmap(io, slots[sub].len);
}

void __init __no_sanitize_address setup_arch(char **cmdline_p)
"""

patch("arch/arm64/kernel/setup.c", [
    ("linux/io.h + atomic.h + string.h + font.h + nmi.h includes",
     "#include <linux/mm.h>\n",
     "#include <linux/mm.h>\n"
     "#include <linux/io.h>\n"
     "#include <linux/atomic.h>\n"
     "#include <linux/string.h>\n"
     "#include <linux/font.h>\n"
     "#include <linux/nmi.h>\n"),

    ("beacon helpers + defines", SETUP_FN_OLD, SETUP_FN_NEW),

    # r14: va48_fix_cmdline() moved here from CP5; sets panic_timeout=0
    # before arm64_memblock_init() and paging_init() run.
    ("CP1 after early_ioremap_init + cmdline fix",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n"
     "\tva48_fix_cmdline();\n"
     "\tva48_beacon_early(1);\n"),

    ("CP2+CP3 around paging_init",
     "\tarm64_memblock_init();\n\n\tpaging_init();\n",
     "\tarm64_memblock_init();\n\tva48_beacon_early(2);\n\n\tpaging_init();\n"
     "\tva48_beacon_linear = true;\n\tva48_beacon(3);\n"),

    ("CP4 after bootmem_init",
     "\tbootmem_init();\n",
     "\tbootmem_init();\n\tva48_beacon(4);\n"),

    # r14: va48_fix_cmdline() no longer called here (moved to CP1)
    ("CP5 after request_standard_resources",
     "\trequest_standard_resources();\n",
     "\trequest_standard_resources();\n\tva48_beacon(5);\n"),
])

# ----------------------------------------------------------------- init/main.c
patch("init/main.c", [
    ("beacon extern decl",
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "void va48_beacon(int cp);\n\n"
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n"),

    ("CP6 after mm_init",
     "\tmm_init();\n",
     "\tmm_init();\n\tva48_beacon(6);\n"),

    ("CP7 after vfs_caches_init",
     "\tvfs_caches_init();\n",
     "\tvfs_caches_init();\n\tva48_beacon(7);\n"),

    ("CP8 before arch_call_rest_init",
     "\tarch_call_rest_init();\n",
     "\tva48_beacon(8);\n\tarch_call_rest_init();\n"),

    ("CP9 after do_pre_smp_initcalls",
     "\tdo_pre_smp_initcalls();\n",
     "\tdo_pre_smp_initcalls();\n\tva48_beacon(9);\n"),

    ("CP10 after sched_init_smp",
     "\tsmp_init();\n\tsched_init_smp();\n",
     "\tsmp_init();\n\tsched_init_smp();\n\tva48_beacon(10);\n"),

    ("CP11 after driver_init",
     "\tcpuset_init_smp();\n\tdriver_init();\n",
     "\tcpuset_init_smp();\n\tdriver_init();\n\tva48_beacon(11);\n"),

    ("CP12-18 per initcall level",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n\t}\n",
     "\t\tstrcpy(command_line, saved_command_line);\n"
     "\t\tdo_initcall_level(level, command_line);\n"
     "\t\t/* VA48 BEACON CP12..CP19: band per completed initcall level */\n"
     "\t\tva48_beacon(12 + level);\n\t}\n"),

    ("CP20 before userspace init",
     "\tif (ramdisk_execute_command) {\n",
     "\tva48_beacon(20);\n\tif (ramdisk_execute_command) {\n"),

    ("CP21 after ramdisk exec failed",
     "\t\tpr_err(\"Failed to execute %s (error %d)\\n\",\n"
     "\t\t       ramdisk_execute_command, ret);\n",
     "\t\tva48_beacon(21);\n"
     "\t\tpr_err(\"Failed to execute %s (error %d)\\n\",\n"
     "\t\t       ramdisk_execute_command, ret);\n"),
])

# ----------------------------------------------------------------- kernel/panic.c
patch("kernel/panic.c", [
    ("panic beacon decl",
     "void __weak panic_smp_self_stop(void)\n",
     "/*\n"
     " * VA48 BEACON: defined in arch/arm64/kernel/setup.c.\n"
     " */\n"
     "#ifdef CONFIG_ARM64\n"
     "void va48_beacon_panic(const char *msg);\n"
     "#else\n"
     "static inline void va48_beacon_panic(const char *msg) { }\n"
     "#endif\n\n"
     "void __weak panic_smp_self_stop(void)\n"),

    ("panic beacon call",
     "\tpr_emerg(\"Kernel panic - not syncing: %s\\n\", buf);\n",
     "\tpr_emerg(\"Kernel panic - not syncing: %s\\n\", buf);\n"
     "\tva48_beacon_panic(buf);\n"),
])

# ------------------------------------------------ arch/arm64/kernel/syscall.c
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
patch("kernel/reboot.c", [
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

    ("CP26 at kernel_restart",
     "void kernel_restart(char *cmd)\n{\n\tkernel_restart_prepare(cmd);\n",
     "void kernel_restart(char *cmd)\n{\n"
     "\tva48_beacon_reboot(26);\n"
     "\tkernel_restart_prepare(cmd);\n"),

    ("CP25 at reboot syscall",
     "\tmutex_lock(&system_transition_mutex);\n\tswitch (cmd) {\n",
     "\tva48_beacon_reboot(25);\n"
     "\tmutex_lock(&system_transition_mutex);\n\tswitch (cmd) {\n"),

    ("CP28 at RESTART case",
     "\tcase LINUX_REBOOT_CMD_RESTART:\n\t\tkernel_restart(NULL);\n",
     "\tcase LINUX_REBOOT_CMD_RESTART:\n"
     "\t\tva48_beacon_reboot(28);\n"
     "\t\tkernel_restart(NULL);\n"),

    ("CP29 at RESTART2 case",
     "\t\tbuffer[sizeof(buffer) - 1] = '\\0';\n\n\t\tkernel_restart(buffer);\n",
     "\t\tbuffer[sizeof(buffer) - 1] = '\\0';\n\n"
     "\t\tva48_beacon_reboot(29);\n"
     "\t\tkernel_restart(buffer);\n"),
])

# -------------------------------------------------------- arch/arm64/mm/mmu.c
# NEW in r14: sub-beacons inside paging_init() so we can see exactly
# which call (map_kernel vs map_mem) is crashing the device.
patch("arch/arm64/mm/mmu.c", [
    ("va48_beacon_paginit extern decl before paging_init",
     "void __init paging_init(void)\n",
     "/* VA48 BEACON r14: defined in arch/arm64/kernel/setup.c */\n"
     "void __init va48_beacon_paginit(int sub);\n\n"
     "void __init paging_init(void)\n"),

    ("paginit sub-beacons around map_kernel and map_mem",
     "\tmap_kernel(pgdp);\n\tmap_mem(pgdp);\n",
     "\tva48_beacon_paginit(0);\n"
     "\tmap_kernel(pgdp);\n"
     "\tva48_beacon_paginit(1);\n"
     "\tmap_mem(pgdp);\n"
     "\tva48_beacon_paginit(2);\n"),
])

print("-" * 62)
print("applied=%d expected=%d failed=%d" % (applied, EXPECT, failed))
if failed or applied != EXPECT:
    print("BEACON PATCH FAILED - refusing to continue")
    import sys; sys.exit(1)
print("beacon OK")
print("  CP0          raw-phys band at boot")
print("  CP1-2        early_ioremap (cmdline+panic fix at CP1)")
print("  paginit 0-2  before map_kernel / after map_kernel / after map_mem")
print("  CP3-20       linear map (kernel health)")
print("  CP21-24      progress cluster (1x/2x/3x/4x)")
print("  CP25-27      reboot cluster (1x/2x/3x)")
print("  CP28-29      reason cluster (1x/2x)")
print("  TEXT         bitmap text @ 0x%x (24x48px/char, 52 cols x 8 rows)" %
      (0x6c0000 + 0x64000 + 0x10000))
print("  PANIC band @ 0x6c0000 written LAST (= done marker)")
print("  panic_timeout=0  set directly in va48_fix_cmdline at CP1")

