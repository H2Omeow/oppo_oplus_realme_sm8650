#!/usr/bin/env python3
"""
VA48 BEACON PATCHER  (round 12)
================================

ROUND 11 RESULT
---------------
r11 added enlarged nibble bands (UNIT=96 KiB, ~33 px/step) and a 12.5 MiB
pre-clear, but the bands were NOT visible in the photograph.  Photo analysis:
  - PANIC band: only ~20 of 81 fb rows reached DRAM (truncated flush)
  - PMSG_CLEAR region: brightness ≈103 == bootloader splash background,
    NOT the ≈15 that a completed memset-0 would leave
  - Conclusion: NMI/hardware watchdog fired inside dcache_clean_inval_poc()
    on the PANIC band, restarting the device before PMSG_CLEAR ever ran.

ROOT CAUSES FIXED IN r12
-------------------------
RC1  CONFIG_PANIC_TIMEOUT=-1 makes panic() call emergency_restart()
     immediately after va48_beacon_panic() returns.  So even if the beacon
     completes, the user has zero time to read the screen before the device
     reboots.  Fix: va48_fix_cmdline() appends " panic=0" so panic spins
     forever; user reads the screen, then manually power-cycles.

RC2  Write order was PANIC band → PMSG_CLEAR → PMSG bands.  A watchdog
     firing during step 1 kills steps 2 and 3.  Fix: render text FIRST,
     write PANIC band LAST (becomes a "done" marker, not a "started" marker).

RC3  Nibble bands required 12.5 MiB pre-clear before any information was
     visible.  Even at 1 GiB/s that is ~12 ms with interrupts off — plenty
     of time for an NMI hardlockup detector to fire.  Fix: switch to direct
     bitmap-font text rendering.  Each line of 52 chars flushes ≈220 KB, done
     in <0.5 ms; a touch_nmi_watchdog() between lines keeps the detector fed.

WHAT r12 DOES
-------------
  1. Renders the panic() string as white-on-black bitmap text directly into
     the splash framebuffer, using the kernel's own font_vga_8x16 (8×16 px
     glyphs, CONFIG_FONT_8x16=y on this device).  Scale factor 3 gives 24×48
     px per character: 52 columns × 8 text rows = 416 characters maximum.

  2. Text area starts at VA48_TEXT_OFF (32 KiB after the PANIC band offset,
     16 fb rows of black separation).  The area is first memset-0 (black bg),
     then characters are rendered row by row.  After each screen row the
     modified lines are flushed and touch_nmi_watchdog() is called.

  3. PANIC band is written LAST.  Its presence means text rendering finished;
     its absence means the device rebooted before text was ready.

  4. va48_fix_cmdline() appends " panic=0" so the device does not auto-reboot.

HOW TO READ r12
---------------
  - Boot: fastboot boot r12_boot.img
  - Screen freezes → photograph it (no time pressure, panic=0 hangs forever)
  - White bands in top cluster: CP0-CP20 (kernel health), unchanged from r11
  - Lower cluster (variable thickness): CP21-CP29 as before
  - Below that: white-on-black text = raw panic() message
  - Thick white PANIC bar: appears BELOW the text only if text is complete
  - Power-cycle manually when done reading

GEOMETRY (unchanged kernel cluster; text replaces nibble bands)
-------
  VA48_TEXT_OFF    = VA48_PANIC_OFF + VA48_PANIC_BAND + 0x10000
                   = 0x6c0000 + 0x64000 + 0x10000 = 0x734000
                   ≈ fb row 1491 (32 KiB = ~6 rows gap after PANIC band)
  FONT             font_vga_8x16: 8 px wide, 16 px tall per glyph
  SCALE            3  →  24 px wide, 48 px tall per character on screen
  COLUMNS          1264 / 24 = 52 characters per line
  MAX ROWS         8 text rows = 416 characters, 384 fb rows
  PANIC_BAND stays at 0x6c0000, written AFTER text is flushed.
"""

import re
import sys
import os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

SPLASH_BASE_C   = "0xd5100000UL"
SPLASH_BASE_ASM = "0xd5100000"
CP0_BAND_ASM    = "0x20000"

EXPECT  = 27
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
print("VA48 BEACON PATCHER  (round 12: direct text rendering)")
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
 * VA48 BEACON  (round 12: direct bitmap-text panic display)
 *
 * All CP band infrastructure is unchanged from r11.  The PMSG nibble-band
 * encoding is replaced by direct text rendering using font_vga_8x16.
 *
 * KEY FIXES vs r11:
 *  1. va48_fix_cmdline() now appends " panic=0" so panic() spins forever
 *     instead of calling emergency_restart() immediately (panic=-1 was the
 *     compiled default), giving the user unlimited time to read the screen.
 *  2. Text is rendered BEFORE the PANIC band; the PANIC band is written last
 *     as a "done" marker.  If the watchdog fires mid-render the PANIC band
 *     is absent, clearly indicating incomplete output.
 *  3. dcache flush is split per text row with touch_nmi_watchdog() between
 *     rows so the NMI hardlockup detector does not fire.
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
 *   SCALE 3       → 24 px wide, 48 px tall on screen
 *   columns       = 1264 / 24 = 52
 *   max rows      = 8   → 416 characters, 384 fb rows of text
 *   clear size    = (8 * 48 + 16) * 1264 * 4 ≈ 2 MiB  (fast)
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
 * Uses font_vga_8x16 (8×16 glyphs, VA48_TEXT_SCALE=3 → 24×48 px/char).
 * The text region is pre-cleared to black, then rendered row by row.
 * After each screen row the modified lines are flushed to DRAM and
 * touch_nmi_watchdog() is called to prevent hardlockup detector trips.
 *
 * Written BEFORE the PANIC band so partial completion is diagnosable:
 * absent PANIC band = text rendering was interrupted by the watchdog.
 */
static void va48_render_panic(const char *msg)
{
	const struct font_desc *font = &font_vga_8x16;
	const u8   *fdata;
	const char *p;
	int fw, fh, cw, ch, cols;
	int x, row_top;		/* current char column and screen row (px) */
	unsigned long text_off  = VA48_TEXT_OFF;
	unsigned long clear_len;
	void *clear_va;

	if (!msg || !font || !font->data)
		return;

	fdata = (const u8 *)font->data;
	fw   = (int)font->width;	/* 8  */
	fh   = (int)font->height;	/* 16 */
	cw   = fw * VA48_TEXT_SCALE;	/* 24 px / char */
	ch   = fh * VA48_TEXT_SCALE;	/* 48 px / char */
	cols = VA48_FB_W / cw;		/* 52 columns   */

	/* Pre-clear the text area to black (fast: ~2 MiB). */
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
			/* Flush completed screen row before moving on. */
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

/*
 * va48_beacon_panic - called from panic() after vscnprintf() fills buf.
 *
 * Order (RC2 fix):
 *   1. Render text  — most important, done first
 *   2. Write PANIC band — acts as "text is complete" marker
 * If the watchdog fires between 1 and 2, the text is visible but the PANIC
 * bar is absent, which is a clear diagnostic signal.
 */
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
 * VA48 R9 FIX: verifiedbootstate=orange → green
 * VA48 R12 FIX: append "panic=0" so panic() spins forever (RC1).
 *
 * boot_command_line[] is writable here; setup_arch() runs before
 * start_kernel() copies it to saved_command_line, and the core_param
 * "panic" is parsed by parse_args() in start_kernel() which runs after
 * setup_arch(), so appending here takes effect before parse_args() reads it.
 */
static void __init va48_fix_cmdline(void)
{
	extern char boot_command_line[];
	char *p = strstr(boot_command_line, "verifiedbootstate=orange");
	size_t len;

	if (p)
		memcpy(p + 18, "green ", 6);

	/* Append " panic=0" if it fits and is not already present. */
	if (!strstr(boot_command_line, "panic=")) {
		len = strlen(boot_command_line);
		if (len + 8 < COMMAND_LINE_SIZE)
			memcpy(boot_command_line + len, " panic=0", 9);
	}
}

void __init __no_sanitize_address setup_arch(char **cmdline_p)
"""

patch("arch/arm64/kernel/setup.c", [
    ("linux/io.h + atomic.h + string.h + font.h includes",
     "#include <linux/mm.h>\n",
     "#include <linux/mm.h>\n"
     "#include <linux/io.h>\n"
     "#include <linux/atomic.h>\n"
     "#include <linux/string.h>\n"
     "#include <linux/font.h>\n"),

    ("beacon helpers + defines", SETUP_FN_OLD, SETUP_FN_NEW),

    ("CP1 after early_ioremap_init",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n",
     "\tearly_fixmap_init();\n\tearly_ioremap_init();\n\tva48_beacon_early(1);\n"),

    ("CP2+CP3 around paging_init",
     "\tarm64_memblock_init();\n\n\tpaging_init();\n",
     "\tarm64_memblock_init();\n\tva48_beacon_early(2);\n\n\tpaging_init();\n"
     "\tva48_beacon_linear = true;\n\tva48_beacon(3);\n"),

    ("CP4 after bootmem_init",
     "\tbootmem_init();\n",
     "\tbootmem_init();\n\tva48_beacon(4);\n"),

    ("CP5 + cmdline fix after request_standard_resources",
     "\trequest_standard_resources();\n",
     "\trequest_standard_resources();\n\tva48_beacon(5);\n\tva48_fix_cmdline();\n"),
])

# ------------------------------------------------------------------ init/main.c
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
     " * Renders the panic message as white-on-black text, then paints the\n"
     " * thick PANIC block as a 'done' marker.  See patch_beacon_r12.py.\n"
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

print("-" * 62)
print("applied=%d expected=%d failed=%d" % (applied, EXPECT, failed))
if failed or applied != EXPECT:
    print("BEACON PATCH FAILED - refusing to continue")
    sys.exit(1)
print("beacon OK")
print("  CP0        raw-phys band at boot")
print("  CP1-2      early_ioremap")
print("  CP3-20     linear map (kernel health)")
print("  CP21-24    progress cluster (1x/2x/3x/4x)")
print("  CP25-27    reboot cluster (1x/2x/3x)")
print("  CP28-29    reason cluster (1x/2x)")
print("  TEXT       bitmap text @ 0x%x (24x48px/char, 52 cols x 8 rows)" %
      (0x6c0000 + 0x64000 + 0x10000))
print("  PANIC band @ 0x6c0000 written LAST (= done marker)")
print("  panic=0    appended by va48_fix_cmdline -> hangs forever")
