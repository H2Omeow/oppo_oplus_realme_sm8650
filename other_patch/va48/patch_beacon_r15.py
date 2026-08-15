#!/usr/bin/env python3
"""
VA48 BEACON PATCHER  (round 15)
================================

CHANGES VS r14
--------------
1. Replace ALL white-bar beacon rendering with text rendering.
   Instead of painting white bands (memset 0xff), every checkpoint
   writes a text line into the framebuffer so the user can read
   exactly which stage was reached.

2. Two text writer helpers:
   - va48_log_early(msg)  uses early_ioremap (maps entire line once)
   - va48_log_late(msg)   uses phys_to_virt + dcache_clean_inval_poc

3. Shared static counter va48_log_row tracks Y position (fb rows).
   Starts at 0, wraps at 2750 to avoid overlap with panic text area.

4. Font: font_vga_8x16 at scale 2 -> 16px wide x 32px tall per glyph,
   79 chars/line, FB width=1264px.

5. CP0 head.S white band stays unchanged (MMU off, can't do text there).
   va48_render_panic() / VA48_TEXT_OFF area stays unchanged.
"""

import sys
import os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

SPLASH_BASE_C   = "0xd5100000UL"
SPLASH_BASE_ASM = "0xd5100000"
CP0_BAND_ASM    = "0x20000"

EXPECT  = 31
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
print("VA48 BEACON PATCHER  (round 15: text rendering for all beacons)")
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
 * VA48 BEACON  (round 15: text rendering for all beacons)
 *
 * KEY CHANGES vs r14:
 *  1. Replace all white-bar beacon rendering with text rendering.
 *     va48_log_early() / va48_log_late() write text lines into FB.
 *  2. Shared va48_log_row counter tracks current Y position.
 *  3. va48_beacon_paint() and va48_beacon_where() removed (unused).
 *  4. va48_us_band[] table and VA48_STRIDE/VA48_BAND removed (unused).
 *  5. panic_timeout=0 and verifiedbootstate fix unchanged from r14.
 *  6. va48_render_panic() and VA48_TEXT_OFF area unchanged.
 */
#include <linux/font.h>

#define VA48_SPLASH_BASE	""" + SPLASH_BASE_C + r"""

#define VA48_PANIC_OFF		0x06c0000UL	/* PANIC band start offset */
#define VA48_PANIC_BAND		0x064000UL	/* 5x, ~81 fb rows         */

/*
 * Text region for panic: starts 0x10000 (16 fb rows) after the PANIC band.
 * Written BEFORE the PANIC band so partial output is detectable.
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
#define VA48_FB_STRIDE		5056		/* 1264 * 4 bytes, BGRA32 */
#define VA48_TEXT_ROWS		8

#include <linux/ptrace.h>

bool va48_beacon_linear __read_mostly;

/*
 * va48_log_row - current top fb-row for next log line; shared between
 * va48_log_early() and va48_log_late().  Starts at 0 (FB top).
 * Wraps at 2750 to avoid overlap with the panic text area.
 */
static int va48_log_row;

/*
 * va48_log_early - write a text line into the framebuffer using early_ioremap.
 *
 * Maps the entire line region (ch=32 rows * VA48_FB_STRIDE bytes = 161792 bytes)
 * once, clears it to black, renders glyphs, then unmaps.  This keeps the
 * number of early_ioremap calls to 1 per text line regardless of line length.
 *
 * font_vga_8x16, scale=2: 16px wide x 32px tall per glyph, 79 chars/line.
 * BGRA32 pixel: white=0xFFFFFFFF, black=0x00000000.
 */
static void __init va48_log_early(const char *msg)
{
	const struct font_desc *font = &font_vga_8x16;
	const u8 *fdata;
	int fw, fh, scale, cw, ch, cols;
	const char *p;
	int x;
	phys_addr_t line_pa;
	unsigned long line_bytes;
	void __iomem *line_io;
	int row_top;

	if (!msg || !font || !font->data)
		return;
	if (va48_log_row > 2750)
		return;

	fdata = (const u8 *)font->data;
	fw    = font->width;   /* 8  */
	fh    = font->height;  /* 16 */
	scale = 2;
	cw    = fw * scale;    /* 16 px/char */
	ch    = fh * scale;    /* 32 rows/char */
	cols  = VA48_FB_W / cw; /* 79 */
	row_top    = va48_log_row;
	line_pa    = (phys_addr_t)VA48_SPLASH_BASE + (phys_addr_t)row_top * VA48_FB_STRIDE;
	line_bytes = (unsigned long)ch * VA48_FB_STRIDE;

	line_io = early_ioremap(line_pa, line_bytes);
	if (!line_io) { va48_log_row += ch; return; }

	/* Clear background to black */
	memset_io(line_io, 0, line_bytes);

	/* Render each character */
	for (p = msg, x = 0; *p && x < cols; p++, x++) {
		unsigned char c = (unsigned char)*p;
		const u8 *glyph;
		int gy, sy, gx, sx;

		if (c < 32 || c > 126) c = '?';
		glyph = fdata + (unsigned int)c * (unsigned int)fh;

		for (gy = 0; gy < fh; gy++) {
			u8 bits = glyph[gy];
			for (sy = 0; sy < scale; sy++) {
				int fb_row = gy * scale + sy;   /* 0..ch-1 within this line */
				/* offset within line_io to start of pixel (x*cw, fb_row) */
				unsigned long pix_off =
					(unsigned long)fb_row * VA48_FB_STRIDE
					+ (unsigned long)(x * cw) * 4;
				for (gx = 0; gx < fw; gx++) {
					u32 pixel = (bits & (0x80u >> gx))
						    ? 0xFFFFFFFFU : 0x00000000U;
					for (sx = 0; sx < scale; sx++) {
						unsigned long off = pix_off + ((unsigned long)(gx * scale + sx)) * 4;
						iowrite32(pixel, (u8 __iomem *)line_io + off);
					}
				}
			}
		}
	}

	early_iounmap(line_io, line_bytes);
	va48_log_row += ch;
}

/*
 * va48_log_late - write a text line into the framebuffer using phys_to_virt.
 *
 * Called after the linear map is up (va48_beacon_linear == true).
 * Clears background, renders glyphs, flushes dcache.
 *
 * font_vga_8x16, scale=2: 16px wide x 32px tall per glyph, 79 chars/line.
 * BGRA32 pixel: white=0xFFFFFFFF, black=0x00000000.
 */
static void va48_log_late(const char *msg)
{
	const struct font_desc *font = &font_vga_8x16;
	const u8 *fdata;
	int fw, fh, scale, cw, ch, cols;
	int x, gy, sy, gx, sx;
	const char *p;
	phys_addr_t fb_base = (phys_addr_t)VA48_SPLASH_BASE;
	int row_top;

	if (!msg || !font || !font->data)
		return;
	if (va48_log_row > 2750)
		return;

	fdata = (const u8 *)font->data;
	fw    = font->width;
	fh    = font->height;
	scale = 2;
	cw    = fw * scale;
	ch    = fh * scale;
	cols  = VA48_FB_W / cw;
	row_top = va48_log_row;

	/* Clear bg */
	{
		unsigned long line_bytes = (unsigned long)ch * VA48_FB_STRIDE;
		void *va = (void *)phys_to_virt(fb_base + (phys_addr_t)row_top * VA48_FB_STRIDE);
		memset(va, 0, line_bytes);
		dcache_clean_inval_poc((unsigned long)va, (unsigned long)va + line_bytes);
	}

	x = 0;
	for (p = msg; *p && x < cols; p++, x++) {
		unsigned char c = (unsigned char)*p;
		const u8 *glyph;
		if (c < 32 || c > 126) c = '?';
		glyph = fdata + (unsigned int)c * (unsigned int)fh;

		for (gy = 0; gy < fh; gy++) {
			u8 bits = glyph[gy];
			for (sy = 0; sy < scale; sy++) {
				int py = row_top + gy * scale + sy;
				u32 *ln = (u32 *)phys_to_virt(
					fb_base + (phys_addr_t)py * VA48_FB_STRIDE
					+ (phys_addr_t)(x * cw) * 4);
				for (gx = 0; gx < fw; gx++) {
					u32 pixel = (bits & (0x80u >> gx)) ? 0xFFFFFFFFU : 0x00000000U;
					for (sx = 0; sx < scale; sx++)
						ln[gx * scale + sx] = pixel;
				}
			}
		}
	}

	/* Flush the written line */
	{
		unsigned long line_bytes = (unsigned long)ch * VA48_FB_STRIDE;
		void *va = (void *)phys_to_virt(fb_base + (phys_addr_t)row_top * VA48_FB_STRIDE);
		dcache_clean_inval_poc((unsigned long)va, (unsigned long)va + line_bytes);
		touch_nmi_watchdog();
	}
	va48_log_row += ch;
}

static phys_addr_t va48_beacon_pa(unsigned long off)
{
	return (phys_addr_t)VA48_SPLASH_BASE + (phys_addr_t)off;
}

void __init va48_beacon_early(int cp)
{
	char msg[64];
	const char *label = msg;
	switch (cp) {
	case 1:  label = "CP01: early_ioremap_init OK"; break;
	case 2:  label = "CP02: before paging_init()"; break;
	default: snprintf(msg, sizeof(msg), "CP%02d", cp); break;
	}
	va48_log_early(label);
}

void va48_beacon(int cp)
{
	char msg[64];
	const char *label = msg;
	switch (cp) {
	case 3:  label = "CP03: paging_init() OK"; break;
	case 4:  label = "CP04: bootmem_init() OK"; break;
	case 5:  label = "CP05: request_standard_resources OK"; break;
	case 6:  label = "CP06: mm_init() OK"; break;
	case 7:  label = "CP07: vfs_caches_init() OK"; break;
	case 8:  label = "CP08: before arch_call_rest_init()"; break;
	case 9:  label = "CP09: do_pre_smp_initcalls() OK"; break;
	case 10: label = "CP10: sched_init_smp() OK"; break;
	case 11: label = "CP11: driver_init() OK"; break;
	case 20: label = "CP20: before userspace"; break;
	case 21: label = "CP21: /init exec FAILED"; break;
	case 22: label = "CP22: first syscall"; break;
	case 23: label = "CP23: 4096 syscalls done"; break;
	case 24: label = "CP24: fatal signal"; break;
	case 25: label = "CP25: reboot() syscall"; break;
	case 26: label = "CP26: kernel_restart()"; break;
	case 27: label = "CP27: emergency_restart()"; break;
	case 28: label = "CP28: RESTART"; break;
	case 29: label = "CP29: RESTART2"; break;
	default:
		if (cp >= 12 && cp <= 19)
			snprintf(msg, sizeof(msg), "CP%d: initcall level %d OK", cp, cp - 12);
		else
			snprintf(msg, sizeof(msg), "CP%02d", cp);
		break;
	}
	va48_log_late(label);
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

void va48_beacon_sigfault(int signo, int code, unsigned long far,
				  const char *str)
{
	struct pt_regs *regs;
	char msg[192];
	bool bssl;
	static bool done;
	static bool bssl_done;

	bssl = strncmp(current->comm, "boringssl_self_", 15) == 0;
	if (bssl) {
		if (bssl_done)
			return;
		bssl_done = true;
	} else {
		if (done)
			return;
		done = true;
	}
	regs = current_pt_regs();
	pr_emerg("VA48 SIGFAULT pid=%d comm=%s sig=%d code=%d far=0x%016lx esr=0x%016lx pc=0x%016lx sp=0x%016lx type=%s\n",
		task_pid_nr(current), current->comm, signo, code, far,
		current->thread.fault_code, regs ? regs->pc : 0,
		regs ? regs->sp : 0, str ? str : "?");
	if (regs) {
		snprintf(msg, sizeof(msg),
			 "SIGFAULT pid=%d sig=%d code=%d far=%016lx",
			 task_pid_nr(current), signo, code, far);
		va48_log_late(msg);
		snprintf(msg, sizeof(msg), "ESR=%016lx PC=%016lx SP=%016lx",
			 current->thread.fault_code, regs->pc, regs->sp);
		va48_log_late(msg);
	}
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
	{
		void *va = (void *)phys_to_virt(va48_beacon_pa(VA48_PANIC_OFF));
		memset(va, 0xff, VA48_PANIC_BAND);
		dcache_clean_inval_poc((unsigned long)va,
				       (unsigned long)va + VA48_PANIC_BAND);
	}
}

void va48_beacon_reboot(int cp)
{
	if (!va48_beacon_linear)
		return;
	va48_beacon(cp);
}

void va48_beacon_reason(const char *reason)
{
	char msg[192];

	if (!va48_beacon_linear || !reason)
		return;
	pr_emerg("VA48 RESTART2 pid=%d tgid=%d comm=%s reason=%s\n",
		 task_pid_nr(current), task_tgid_nr(current), current->comm,
		 reason);
	snprintf(msg, sizeof(msg), "RESTART2 pid=%d comm=%s",
		 task_pid_nr(current), current->comm);
	va48_log_late(msg);
	va48_log_late(reason);
}

void va48_beacon_exit(long code)
{
	char msg[192];

	if (!va48_beacon_linear)
		return;
	if (strncmp(current->comm, "boringssl_self_", 15) != 0)
		return;
	pr_emerg("VA48 BSSL exit pid=%d code=0x%lx comm=%s\n",
		 task_pid_nr(current), code, current->comm);
	snprintf(msg, sizeof(msg), "BSSL exit pid=%d code=0x%lx",
		 task_pid_nr(current), code);
	va48_log_late(msg);
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
	char *p;

	/* verifiedbootstate=orange -> green */
	p = strstr(boot_command_line, "verifiedbootstate=orange");
	if (p)
		memcpy(p + 18, "green ", 6);

	/*
	 * Override CONFIG_PANIC_TIMEOUT=-1 directly so panic() spins
	 * forever instead of calling emergency_restart() immediately.
	 */
	panic_timeout = 0;

	/*
	 * Disable phoenix HLOS watchdog hang detection.
	 * hang_oplus_main_on=1 in bootloader cmdline causes phoenix to
	 * reboot the device via RESTART2 after 240 s if oplus_main does
	 * not pet the watchdog -- which never happens on a VA48 kernel
	 * that the system does not recognise as a "normal" boot.
	 * Patch the in-RAM copy of boot_command_line to 0 so that
	 * phoenix_hlos_watchdog_init reads hang_oplus_main_on=0.
	 */
	p = strstr(boot_command_line, "hang_oplus_main_on=1");
	if (p)
		p[19] = '0';

	/*
	 * shutdown_panic=-1 in cmdline causes phoenix to treat any
	 * kernel panic as a reason to reboot immediately.  Set to 0.
	 */
	p = strstr(boot_command_line, "shutdown_panic=-1");
	if (p)
		memcpy(p + 15, "0 ", 2);

	/*
	 * Patch DT /chosen bootargs in-place so phoenix reads
	 * hang_oplus_main_on=0 and never starts the HLOS watchdog.
	 *
	 * phoenix uses of_find_node_opts_by_path("/chosen") +
	 * of_property_read_string("bootargs") — it reads directly from
	 * the live DT blob in memory, NOT from saved_command_line or
	 * boot_command_line.  The DT property value is a NUL-terminated
	 * string stored inside the flattened DT; it is writable at this
	 * point and stays in memory for the lifetime of the kernel.
	 */
	{
		struct device_node *chosen;
		const char *prop;

		chosen = of_find_node_opts_by_path("/chosen", NULL);
		if (!chosen)
			chosen = of_find_node_opts_by_path("/chosen@0", NULL);
		if (chosen &&
		    of_property_read_string(chosen, "bootargs", &prop) == 0 &&
		    prop) {
			/*
			 * prop points into the DT blob — cast away const and
			 * edit in-place.  The string is writable here; GKI
			 * does not mark DT memory read-only before MMU init.
			 */
			char *bp = (char *)prop;
			char *q = strstr(bp, "hang_oplus_main_on=1");
			if (q)
				q[19] = '0';
		}
	}
}

/*
 * va48_beacon_paginit - write a text sub-beacon from inside paging_init().
 *
 * Uses va48_log_early() because cpu_replace_ttbr1 has not yet run.
 */
void __init va48_beacon_paginit(int sub)
{
	static const char * const labels[] = {
		"PAGINIT-0: before map_kernel()",
		"PAGINIT-1: after map_kernel()",
		"PAGINIT-2: after map_mem()",
	};
	if ((unsigned int)sub < ARRAY_SIZE(labels))
		va48_log_early(labels[sub]);
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
    ("beacon extern decl + saved_cmdline patcher decl",
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "void va48_beacon(int cp);\n\n"
     "/*\n"
     " * va48_fix_saved_cmdline - patch saved_command_line after setup_command_line()\n"
     " * has copied boot_command_line + extra_command_line (bootconfig) into it.\n"
     " *\n"
     " * hang_oplus_main_on=1 comes from bootconfig (extra_command_line), NOT from\n"
     " * the kernel cmdline itself, so patching boot_command_line is ineffective.\n"
     " * saved_command_line is the merged string that /proc/cmdline exposes and that\n"
     " * oplus_bsp_dfr_phoenix reads via phx_parse_cmdline().\n"
     " */\n"
     "static void __init va48_fix_saved_cmdline(void)\n"
     "{\n"
     "\textern char *saved_command_line;\n"
     "\tchar *p;\n"
     "\n"
     "\tif (!saved_command_line)\n"
     "\t\treturn;\n"
     "\n"
     "\t/* Disable phoenix HLOS watchdog hang detection */\n"
     "\tp = strstr(saved_command_line, \"hang_oplus_main_on=1\");\n"
     "\tif (p)\n"
     "\t\tp[19] = '0';\n"
     "\n"
     "\t/* Disable phoenix panic-triggered reboot */\n"
     "\tp = strstr(saved_command_line, \"shutdown_panic=-1\");\n"
     "\tif (p)\n"
     "\t\tmemcpy(p + 15, \"0 \", 2);\n"
     "}\n\n"
     "asmlinkage __visible void __init __no_sanitize_address start_kernel(void)\n"),

    ("CP6 after mm_init",
     "\tmm_init();\n",
     "\tmm_init();\n\tva48_beacon(6);\n"),

    ("fix saved_cmdline after setup_command_line",
     "\tsetup_command_line(command_line);\n",
     "\tsetup_command_line(command_line);\n\tva48_fix_saved_cmdline();\n"),

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
     "void va48_beacon_sigfault(int signo, int code, unsigned long far,\n"
     "\t\t\t   const char *str);\n\n"
     "void arm64_force_sig_fault(int signo, int code, unsigned long far,\n"
     "\t\t\t   const char *str)\n"
     "{\n"
     "\tva48_beacon_sigfault(signo, code, far, str);\n"
     "\tarm64_show_signal(signo, str);\n"),
])

# ---------------------------------------------------------------- kernel/reboot.c
patch("kernel/reboot.c", [
    ("CP27 + reboot beacon decl at emergency_restart",
     "void emergency_restart(void)\n{\n\tkmsg_dump(KMSG_DUMP_EMERG);\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "#include <linux/panic.h>\n"
     "#ifdef CONFIG_ARM64\n"
     "void va48_beacon_reboot(int cp);\n"
     "void va48_beacon_reason(const char *reason);\n"
     "#else\n"
     "static inline void va48_beacon_reboot(int cp) { }\n"
     "static inline void va48_beacon_reason(const char *reason) { }\n"
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
     "\t\tva48_beacon_reason(buffer);\n"
     "\t\t/*\n"
     "\t\t * VA48 r15j: on boringssl self-check failure do NOT return to\n"
     "\t\t * init (that would make init abort() and produce the misleading\n"
     "\t\t * \"Attempted to kill init!\" panic).  panic() here with\n"
     "\t\t * panic_timeout=0 so the screen freezes on the real first cause:\n"
     "\t\t * caller pid/comm + reason already rendered by va48_beacon_reason.\n"
     "\t\t */\n"
     "\t\tif (strncmp(buffer, \"boringssl-self-check-failed\",\n"
     "\t\t\t    sizeof(\"boringssl-self-check-failed\") - 1) == 0)\n"
     "\t\t\tpanic(\"va48: boringssl self-check-failed\");\n"
     "\t\tkernel_restart(buffer);\n"),
])

# ---------------------------------------------------------------- kernel/exit.c
# Record the real exit code of boringssl_self_test processes so we can see
# exactly which self-test failed and with what status (0 = success).
patch("kernel/exit.c", [
    ("va48_beacon_exit decl + call in do_exit",
     "void __noreturn do_exit(long code)\n{\n"
     "\tstruct task_struct *tsk = current;\n"
     "\tint group_dead;\n\n"
     "\tWARN_ON(irqs_disabled());\n\n"
     "\tsynchronize_group_exit(tsk, code);\n",
     "/* VA48 BEACON: defined in arch/arm64/kernel/setup.c */\n"
     "#ifdef CONFIG_ARM64\n"
     "void va48_beacon_exit(long code);\n"
     "#else\n"
     "static inline void va48_beacon_exit(long code) { }\n"
     "#endif\n\n"
     "void __noreturn do_exit(long code)\n{\n"
     "\tstruct task_struct *tsk = current;\n"
     "\tint group_dead;\n\n"
     "\tWARN_ON(irqs_disabled());\n\n"
     "\tsynchronize_group_exit(tsk, code);\n"
     "\tva48_beacon_exit(code);\n"),
])

# -------------------------------------------------------- arch/arm64/mm/mmu.c
# Sub-beacons inside paging_init() so we can see exactly which call
# (map_kernel vs map_mem) is crashing the device.
patch("arch/arm64/mm/mmu.c", [
    ("va48_beacon_paginit extern decl before paging_init",
     "void __init paging_init(void)\n",
     "/* VA48 BEACON r15: defined in arch/arm64/kernel/setup.c */\n"
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
print("  CP0          raw-phys band at boot (head.S, unchanged)")
print("  CP1-2        early text via va48_log_early (cmdline+panic fix at CP1)")
print("  paginit 0-2  text: before map_kernel / after map_kernel / after map_mem")
print("  CP3-20       late text via va48_log_late (kernel health)")
print("  CP21-29      late text via va48_log_late (userspace / reboot path)")
print("  TEXT         panic bitmap text @ 0x%x (24x48px/char, 52 cols x 8 rows)" %
      (0x6c0000 + 0x64000 + 0x10000))
print("  PANIC band @ 0x6c0000 written LAST (= done marker, unchanged)")
print("  panic_timeout=0  set directly in va48_fix_cmdline at CP1")
