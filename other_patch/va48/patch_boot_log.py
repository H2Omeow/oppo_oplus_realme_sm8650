#!/usr/bin/env python3
"""SM8650 / 6.1.118 可选屏幕故障日志，独立于 VA48 补丁。

沿用旧 beacon 的一加12连续启动画面地址与尺寸；新日志尚未实机验证。
panic/oops/重启时显示 printk 尾部，启动卡死时保留最后执行的 initcall。
无法处理不能继续执行代码的 CPU、供电或显示故障。
不跟踪 init 系统调用、不抓取进程快照、不修改启动验证状态。
屏幕使用原有 ASCII 字库，屏幕提示和内核原始日志保留英文。
"""

from pathlib import Path
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
HELPERS = r'''
/* 可选启动故障画面：沿用一加12连续启动画面的 framebuffer。 */
#include <linux/atomic.h>
#include <linux/font.h>
#include <linux/io.h>
#include <linux/kmsg_dump.h>
#include <linux/panic.h>
#include <linux/sched.h>
#include <linux/string.h>

#define BOOTLOG_FB_BASE 0xd5100000UL
#define BOOTLOG_FB_W 1264
#define BOOTLOG_FB_H 2780
#define BOOTLOG_STRIDE 5056
#define BOOTLOG_LINE_H 32
#define BOOTLOG_COLS (BOOTLOG_FB_W / 16)
#define BOOTLOG_ROWS (BOOTLOG_FB_H / BOOTLOG_LINE_H)

static bool bootlog_ready;
static bool bootlog_frozen;
static atomic_t bootlog_dumping = ATOMIC_INIT(0);
static atomic_t bootlog_checkpoint_busy = ATOMIC_INIT(0);
static char bootlog_kmsg[16384];

/* 缓冲区使用静态存储，故障处理期间不分配内存、不休眠。 */
static void bootlog_line(const char *text, unsigned int row)
{
	const struct font_desc *font = &font_vga_8x16;
	u32 *base;
	unsigned int x, gy, sy, gx, sx;
	unsigned long addr;

	if (!text || !font->data || row >= BOOTLOG_ROWS)
		return;
	base = phys_to_virt((phys_addr_t)BOOTLOG_FB_BASE +
			   row * BOOTLOG_LINE_H * BOOTLOG_STRIDE);
	memset(base, 0, BOOTLOG_LINE_H * BOOTLOG_STRIDE);
	for (x = 0; text[x] && x < BOOTLOG_COLS; x++) {
		unsigned char c = text[x];
		const u8 *glyph;

		if (c < 32 || c > 126)
			c = '?';
		glyph = (const u8 *)font->data + c * 16;
		for (gy = 0; gy < 16; gy++)
			for (sy = 0; sy < 2; sy++) {
				u32 *pixels = base + (gy * 2 + sy) * (BOOTLOG_STRIDE / 4) + x * 16;
				for (gx = 0; gx < 8; gx++)
					for (sx = 0; sx < 2; sx++)
						pixels[gx * 2 + sx] =
							(glyph[gy] & (0x80U >> gx)) ? 0xffffffffU : 0;
			}
	}
	addr = (unsigned long)base;
	dcache_clean_inval_poc(addr, addr + BOOTLOG_LINE_H * BOOTLOG_STRIDE);
}

/* 按屏幕宽度换行，避免丢失第79列边界处的字符。 */
static size_t bootlog_next_row(const char *buf, size_t len, size_t pos)
{
	unsigned int col = 0;

	while (pos < len && buf[pos] != '\n' && col < BOOTLOG_COLS) {
		pos++;
		col++;
	}
	if (pos < len && buf[pos] == '\n')
		pos++;
	return pos;
}

static size_t bootlog_tail_start(const char *buf, size_t len, unsigned int rows)
{
	size_t start = 0, scan = 0;
	unsigned int count = 0;

	while (scan < len) {
		scan = bootlog_next_row(buf, len, scan);
		if (++count > rows)
			start = bootlog_next_row(buf, len, start);
	}
	return start;
}

static void bootlog_show(const char *reason)
{
	struct kmsg_dump_iter iter;
	size_t len = 0, pos;
	unsigned int row;
	char line[BOOTLOG_COLS + 1];

	if (!READ_ONCE(bootlog_ready) ||
	    atomic_cmpxchg(&bootlog_dumping, 0, 1))
		return;
	/* 已停止的 CPU 可能持有 checkpoint_busy，故障处理不能等待该锁。 */
	WRITE_ONCE(bootlog_frozen, true);
	kmsg_dump_rewind(&iter);
	kmsg_dump_get_buffer(&iter, true, bootlog_kmsg, sizeof(bootlog_kmsg), &len);
	len = min_t(size_t, len, sizeof(bootlog_kmsg) - 1);
	bootlog_kmsg[len] = '\0';
	for (row = 0; row < BOOTLOG_ROWS; row++)
		bootlog_line("", row);
	bootlog_line(reason, 0);
	bootlog_line("Recent kernel log: fault/PC/LR/Call trace when available", 1);
	pos = bootlog_tail_start(bootlog_kmsg, len, BOOTLOG_ROWS - 2);
	for (row = 2; pos < len && row < BOOTLOG_ROWS; row++) {
		size_t end = bootlog_next_row(bootlog_kmsg, len, pos);
		size_t n = end - pos;

		if (n && bootlog_kmsg[end - 1] == '\n')
			n--;
		memcpy(line, bootlog_kmsg + pos, n);
		line[n] = '\0';
		bootlog_line(line, row);
		pos = end;
	}
	atomic_set(&bootlog_dumping, 0);
}

static void bootlog_dump(struct kmsg_dumper *dumper,
			enum kmsg_dump_reason reason)
{
	/* kmsg_dump 在内核输出回溯后调用，以便保留故障现场。 */
	if (reason == KMSG_DUMP_PANIC) {
		panic_timeout = 0;
		bootlog_show("KERNEL PANIC - recent kernel log below");
	} else if (reason == KMSG_DUMP_OOPS) {
		bootlog_show("KERNEL OOPS - recent kernel log below");
	}
}

static struct kmsg_dumper bootlog_dumper = {
	.dump = bootlog_dump,
	.max_reason = KMSG_DUMP_OOPS,
};

static void __init bootlog_init(void)
{
	int ret;

	WRITE_ONCE(bootlog_ready, true);
	ret = kmsg_dump_register(&bootlog_dumper);
	if (ret)
		pr_err("bootlog: kmsg dumper registration failed: %d\n", ret);
	bootlog_line("BOOT: paging_init completed; crash log capture ready", 0);
}

void bootlog_initcall_start(initcall_t fn)
{
	char line[BOOTLOG_COLS + 1];

	if (!READ_ONCE(bootlog_ready) || READ_ONCE(bootlog_frozen) ||
	    atomic_cmpxchg(&bootlog_checkpoint_busy, 0, 1))
		return;
	snprintf(line, sizeof(line), "LAST INITCALL (not proof of fault): %ps", fn);
	bootlog_line(line, 0);
	atomic_set(&bootlog_checkpoint_busy, 0);
}

void bootlog_initcall_finish(initcall_t fn, int ret)
{
	/* initcall 返回负值不一定致命，仅记录返回值，不直接判定为根因。 */
	if (ret < 0)
		pr_warn("bootlog: initcall %ps returned %d (may be non-fatal)\n", fn, ret);
}

void bootlog_userspace(void)
{
	if (READ_ONCE(bootlog_ready) && !READ_ONCE(bootlog_frozen))
		bootlog_line("BOOT: kernel initcalls finished; starting userspace", 0);
}

void bootlog_reboot(const char *cmd)
{
	pr_emerg("bootlog: reboot requested by pid=%d comm=%s reason=%s\n",
		 task_pid_nr(current), current->comm, cmd ? cmd : "<none>");
	bootlog_show("REBOOT REQUEST - not necessarily a kernel crash");
}

'''

EDITS = {
    'arch/arm64/kernel/setup.c': [
        ('crash-screen helpers',
         'void __init __no_sanitize_address setup_arch(char **cmdline_p)\n',
         HELPERS + 'void __init __no_sanitize_address setup_arch(char **cmdline_p)\n'),
        ('register after paging_init',
         '\tarm64_memblock_init();\n\n\tpaging_init();\n',
         '\tarm64_memblock_init();\n\n\tpaging_init();\n\tbootlog_init();\n'),
    ],
    'init/main.c': [
        ('logger declarations', '#include <linux/init.h>\n',
         '#include <linux/init.h>\n'
         'void bootlog_initcall_start(initcall_t fn);\n'
         'void bootlog_initcall_finish(initcall_t fn, int ret);\n'
         'void bootlog_userspace(void);\n'),
        ('last running initcall',
         '\tdo_trace_initcall_start(fn);\n\tret = fn();\n\tdo_trace_initcall_finish(fn, ret);\n',
         '\tdo_trace_initcall_start(fn);\n\tbootlog_initcall_start(fn);\n'
         '\tret = fn();\n\tbootlog_initcall_finish(fn, ret);\n'
         '\tdo_trace_initcall_finish(fn, ret);\n'),
        ('userspace transition', '\tif (ramdisk_execute_command) {\n',
         '\tbootlog_userspace();\n\tif (ramdisk_execute_command) {\n'),
    ],
    'kernel/reboot.c': [
        ('reboot logger declaration', '#include <linux/reboot.h>\n',
         '#include <linux/reboot.h>\nvoid bootlog_reboot(const char *cmd);\n'),
        ('reboot reason and kernel log tail',
         'void kernel_restart(char *cmd)\n{\n\tkernel_restart_prepare(cmd);\n',
         'void kernel_restart(char *cmd)\n{\n\tbootlog_reboot(cmd);\n\tkernel_restart_prepare(cmd);\n'),
    ],
}

def main():
    # 写入文件前校验所有锚点，避免锚点缺失导致只应用部分补丁。
    pending, applied, errors = {}, [], []
    for rel, edits in EDITS.items():
        path = ROOT / rel
        try:
            src = path.read_text(encoding='utf-8', errors='surrogateescape')
        except OSError as exc:
            errors.append(f'{rel}: {exc}')
            continue
        for label, old, new in edits:
            count = src.count(old)
            if count != 1:
                errors.append(f'{rel} [{label}]: anchor matched {count}x (want 1)')
            else:
                src = src.replace(old, new, 1)
                applied.append(f'{rel} [{label}]')
        pending[path] = src
    expected = sum(map(len, EDITS.values()))
    if errors or len(applied) != expected:
        print('\n'.join('FAIL ' + error for error in errors))
        print(f'applied=0 expected={expected} failed={len(errors)}; no files written')
        return 1
    for path, src in pending.items():
        path.write_text(src, encoding='utf-8', errors='surrogateescape', newline='\n')
    print('\n'.join('OK ' + item for item in applied))
    print(f'applied={len(applied)} expected={expected} failed=0')
    print('boot log patch OK')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
