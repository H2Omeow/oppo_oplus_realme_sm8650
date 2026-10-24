#!/usr/bin/env python3
"""ARM64 / 6.1.x 可选屏幕故障日志，独立于 VA48 补丁。

默认仅使用设备树 /chosen 中参数完整的 simple-framebuffer；不猜测屏幕地址。
可显式选择旧一加12布局或提供自定义布局，内核仍会检查物理范围是否保留。
本次跨版本适配和屏幕输出均未实机验证。
panic/oops/重启时显示 printk 尾部，启动卡死时保留最后执行的 initcall。
无法处理不能继续执行代码的 CPU、供电或显示故障。
不跟踪 init 系统调用、不抓取进程快照、不修改启动验证状态。
屏幕使用原有 ASCII 字库，屏幕提示和内核原始日志保留英文。
"""

import argparse
from pathlib import Path
import re
import sys

HELPERS = r'''
/* 可选故障画面：只映射经过参数与保留范围检查的 framebuffer。 */
#include <linux/atomic.h>
#include <linux/font.h>
#include <linux/io.h>
#include <linux/kmsg_dump.h>
#include <linux/memblock.h>
#include <linux/of_address.h>
#include <linux/panic.h>
#include <linux/sched.h>
#include <linux/string.h>
#include <asm/cacheflush.h>

#define BOOTLOG_LINE_H 32
#define BOOTLOG_MAX_COLS 512
#define BOOTLOG_MAX_BYTES (64UL * 1024 * 1024)
#define BOOTLOG_AUTO @AUTO@

struct bootlog_fb_config {
	phys_addr_t base;
	u32 width, height, stride;
	u64 span;
};

static struct bootlog_fb_config bootlog_fb = { @FRAMEBUFFER@ };
static u8 *bootlog_pixels;
static unsigned int bootlog_cols, bootlog_rows;
static bool bootlog_fb_validated;

static bool bootlog_ready;
static bool bootlog_frozen;
static initcall_t bootlog_last_initcall;
static atomic_t bootlog_dumping = ATOMIC_INIT(0);
static atomic_t bootlog_checkpoint_busy = ATOMIC_INIT(0);
static char bootlog_kmsg[16384];

static bool __init bootlog_valid_layout(const struct bootlog_fb_config *fb)
{
	u64 size = (u64)fb->height * fb->stride;

	return fb->base && !(fb->base & 3) &&
		fb->width >= 16 && fb->width <= 8192 &&
		fb->height >= 96 && fb->height <= 8192 &&
		!(fb->stride & 3) && fb->stride >= (u64)fb->width * 4 &&
		fb->stride <= 65536 && size <= BOOTLOG_MAX_BYTES &&
		size <= fb->span && fb->span <= BOOTLOG_MAX_BYTES &&
		fb->base <= (phys_addr_t)~0ULL - fb->span;
}

/* 此检查在 bootmem_init 之前执行，不在 memblock 元数据释放后访问它。 */
static bool __init bootlog_reserved(const struct bootlog_fb_config *fb)
{
	phys_addr_t start, end;
	u64 index;

	for_each_reserved_mem_range(index, &start, &end)
		if (start <= fb->base && end >= fb->base &&
		    fb->span <= end - fb->base)
			return true;
	return false;
}

static bool __init bootlog_read_simplefb(void)
{
	struct device_node *chosen, *node;
	struct bootlog_fb_config candidate = { 0 };
	unsigned int found = 0;

	chosen = of_find_node_by_path("/chosen");
	if (!chosen)
		return false;
	for_each_available_child_of_node(chosen, node) {
		struct resource res;
		struct bootlog_fb_config fb = { 0 };
		const char *format;

		if (!of_device_is_compatible(node, "simple-framebuffer"))
			continue;
		if (of_address_to_resource(node, 0, &res) ||
		    of_property_read_u32(node, "width", &fb.width) ||
		    of_property_read_u32(node, "height", &fb.height) ||
		    of_property_read_u32(node, "stride", &fb.stride) ||
		    of_property_read_string(node, "format", &format))
			continue;
		/* 黑白字形支持这四种 32 位像素格式，其他格式不尝试写入。 */
		if (strcmp(format, "x8r8g8b8") && strcmp(format, "a8r8g8b8") &&
		    strcmp(format, "x8b8g8r8") && strcmp(format, "a8b8g8r8"))
			continue;
		fb.base = res.start;
		fb.span = resource_size(&res);
		if (!bootlog_valid_layout(&fb) || !bootlog_reserved(&fb))
			continue;
		candidate = fb;
		found++;
	}
	of_node_put(chosen);
	/* 多屏布局存在歧义时关闭输出，避免选择错误的显示缓冲区。 */
	if (found != 1)
		return false;
	bootlog_fb = candidate;
	return true;
}

/* 缓冲区使用静态存储，故障处理期间不分配内存、不休眠。 */
static void bootlog_line(const char *text, unsigned int row)
{
	const struct font_desc *font = &font_vga_8x16;
	u32 *base;
	unsigned int x, gy, sy, gx, sx, pixel;
	unsigned long addr;

	if (!text || !font->data || !smp_load_acquire(&bootlog_ready) || row >= bootlog_rows)
		return;
	base = (u32 *)(bootlog_pixels + row * BOOTLOG_LINE_H * bootlog_fb.stride);
	for (pixel = 0; pixel < BOOTLOG_LINE_H * (bootlog_fb.stride / 4); pixel++)
		base[pixel] = 0xff000000U;
	for (x = 0; text[x] && x < bootlog_cols; x++) {
		unsigned char c = text[x];
		const u8 *glyph;

		if (c < 32 || c > 126)
			c = '?';
		glyph = (const u8 *)font->data + c * 16;
		for (gy = 0; gy < 16; gy++)
			for (sy = 0; sy < 2; sy++) {
				u32 *pixels = base + (gy * 2 + sy) * (bootlog_fb.stride / 4) + x * 16;
				for (gx = 0; gx < 8; gx++)
					for (sx = 0; sx < 2; sx++)
						pixels[gx * 2 + sx] =
							(glyph[gy] & (0x80U >> gx)) ? 0xffffffffU : 0xff000000U;
			}
	}
	addr = (unsigned long)base;
	dcache_clean_poc(addr, addr + BOOTLOG_LINE_H * bootlog_fb.stride);
}

/* 按屏幕宽度换行，避免丢失第79列边界处的字符。 */
static size_t bootlog_next_row(const char *buf, size_t len, size_t pos)
{
	unsigned int col = 0;

	while (pos < len && buf[pos] != '\n' && col < bootlog_cols) {
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
	char line[BOOTLOG_MAX_COLS + 1];

	if (!smp_load_acquire(&bootlog_ready) ||
	    atomic_cmpxchg(&bootlog_dumping, 0, 1))
		return;
	/* 已停止的 CPU 可能持有 checkpoint_busy，故障处理不能等待该锁。 */
	WRITE_ONCE(bootlog_frozen, true);
	kmsg_dump_rewind(&iter);
	kmsg_dump_get_buffer(&iter, true, bootlog_kmsg, sizeof(bootlog_kmsg), &len);
	len = min_t(size_t, len, sizeof(bootlog_kmsg) - 1);
	bootlog_kmsg[len] = '\0';
	for (row = 0; row < bootlog_rows; row++)
		bootlog_line("", row);
	bootlog_line(reason, 0);
	bootlog_line("Recent kernel log: fault/PC/LR/Call trace when available", 1);
	pos = bootlog_tail_start(bootlog_kmsg, len, bootlog_rows - 2);
	for (row = 2; pos < len && row < bootlog_rows; row++) {
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
	pr_emerg("bootlog: last initcall=%ps (not proof of fault)\n",
		 READ_ONCE(bootlog_last_initcall));
	if (reason == KMSG_DUMP_PANIC) {
		/* 无有效屏幕时保持系统原有 panic 超时策略。 */
		if (smp_load_acquire(&bootlog_ready))
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

	if (BOOTLOG_AUTO)
		bootlog_fb_validated = bootlog_read_simplefb();
	else
		bootlog_fb_validated = bootlog_valid_layout(&bootlog_fb) &&
			bootlog_reserved(&bootlog_fb);
	if (!bootlog_fb_validated)
		pr_warn("bootlog: no unambiguous reserved framebuffer; screen disabled, printk diagnostics remain available\n");
	ret = kmsg_dump_register(&bootlog_dumper);
	if (ret)
		pr_err("bootlog: kmsg dumper registration failed: %d\n", ret);
}

/* 等待常规内存分配器就绪后再建立持久映射，不占用 early_ioremap 槽位。 */
static int __init bootlog_map_screen(void)
{
	if (!bootlog_fb_validated)
		return 0;
	bootlog_pixels = memremap(bootlog_fb.base, bootlog_fb.span, MEMREMAP_WB);
	if (!bootlog_pixels) {
		pr_warn("bootlog: framebuffer mapping failed; screen disabled\n");
		return 0;
	}
	bootlog_cols = bootlog_fb.width / 16;
	bootlog_rows = bootlog_fb.height / BOOTLOG_LINE_H;
	smp_store_release(&bootlog_ready, true);
	bootlog_line("BOOT: framebuffer mapped; crash log capture ready", 0);
	return 0;
}
core_initcall(bootlog_map_screen);

void bootlog_initcall_start(initcall_t fn)
{
	char line[BOOTLOG_MAX_COLS + 1];

	WRITE_ONCE(bootlog_last_initcall, fn);
	if (!smp_load_acquire(&bootlog_ready) || READ_ONCE(bootlog_frozen) ||
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
	if (smp_load_acquire(&bootlog_ready) && !READ_ONCE(bootlog_frozen))
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
    'lib/fonts/Kconfig': [
        ('built-in font dependency', 'config FONT_SUPPORT\n\ttristate\n',
         'config FONT_SUPPORT\n\ttristate\n'
         '\t# 故障画面直接使用内置字体，不依赖 framebuffer 控制台。\n'
         '\tdefault y\n'),
    ],
    'arch/arm64/kernel/setup.c': [
        ('crash-screen helpers',
         '@SETUP_ARCH@', '@HELPERS@@SETUP_ARCH@'),
        ('register after device tree initialization',
         '\t\tunflatten_device_tree();\n\n\tbootmem_init();\n',
         '\t\tunflatten_device_tree();\n\n\tbootlog_init();\n\tbootmem_init();\n'),
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

FORMATS = ('x8r8g8b8', 'a8r8g8b8', 'x8b8g8r8', 'a8b8g8r8')
MAX_BYTES = 64 * 1024 * 1024


def framebuffer_config(text):
    """只接受具名整数与已知像素格式，不把用户文本拼接为 C 代码。"""
    fields = {}
    for part in text.split(','):
        pair = part.strip().split('=', 1)
        if len(pair) != 2 or pair[0] in fields:
            raise ValueError('布局应为互不重复的 base,width,height,stride,format 键值对')
        fields[pair[0]] = pair[1]
    if set(fields) != {'base', 'width', 'height', 'stride', 'format'}:
        raise ValueError('布局必须包含 base,width,height,stride,format，且不能有其他字段')
    if fields['format'] not in FORMATS:
        raise ValueError('像素格式仅支持 ' + ', '.join(FORMATS))
    parsed = {}
    for key in ('base', 'width', 'height', 'stride'):
        value = fields[key]
        if not re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)', value):
            raise ValueError(f'{key} 必须是十进制或十六进制非负整数')
        parsed[key] = int(value, 16 if value.lower().startswith('0x') else 10)
    base, width, height, stride = (parsed[k] for k in ('base', 'width', 'height', 'stride'))
    span = height * stride
    if not (0 < base < 2**64 and base % 4 == 0 and
            16 <= width <= 8192 and 96 <= height <= 8192 and
            width * 4 <= stride <= 65536 and stride % 4 == 0 and
            span <= MAX_BYTES and base + span < 2**64):
        raise ValueError('布局越界：要求地址及步长4字节对齐、宽16..8192、高96..8192、步长至少宽×4且不超过65536、总长不超过64MiB')
    return parsed


def render_helpers(profile, framebuffer=None):
    if profile == 'auto':
        initializer = '.base = 0'
    else:
        config = framebuffer_config(
            'base=0xd5100000,width=1264,height=2780,stride=5056,format=x8r8g8b8'
            if profile == 'oneplus12-legacy' else framebuffer or '')
        initializer = (f'.base = 0x{config["base"]:x}ULL, .width = {config["width"]}, '
                       f'.height = {config["height"]}, .stride = {config["stride"]}, '
                       f'.span = {config["height"] * config["stride"]}ULL')
    return HELPERS.replace('@AUTO@', str(int(profile == 'auto'))).replace('@FRAMEBUFFER@', initializer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tree_root', nargs='?', default='.')
    parser.add_argument('--check', action='store_true', help='只进行全量预检，不写入文件')
    parser.add_argument('--profile', choices=('auto', 'oneplus12-legacy', 'custom'), default='auto',
                        help='auto只接受设备树明确的simple-framebuffer；旧一加12布局必须显式选择')
    parser.add_argument('--framebuffer', help='custom布局：base=0x...,width=...,height=...,stride=...,format=x8r8g8b8')
    args = parser.parse_args(argv)
    root = Path(args.tree_root)
    try:
        if bool(args.framebuffer) != (args.profile == 'custom'):
            raise ValueError('--framebuffer 必须且只能与 --profile custom 一起使用')
        helpers = render_helpers(args.profile, args.framebuffer)
        makefile = (root / 'Makefile').read_text(encoding='utf-8', errors='surrogateescape')
        version = [re.search(rf'^{key}\s*=\s*(\d+)\s*$', makefile, re.M) for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')]
        if not all(version) or (version[0].group(1), version[1].group(1)) != ('6', '1'):
            raise ValueError('只支持 Linux 6.1.x 源码；其他版本未做兼容验证')
    except (OSError, ValueError) as exc:
        print(f'FAIL {exc}')
        print(f'applied=0 expected={sum(map(len, EDITS.values()))} failed=1; no files written')
        return 1
    # 写入文件前校验所有锚点，避免锚点缺失导致只应用部分补丁。
    pending, applied, errors = {}, [], []
    for rel, edits in EDITS.items():
        path = root / rel
        try:
            src = path.read_text(encoding='utf-8', errors='surrogateescape')
        except OSError as exc:
            errors.append(f'{rel}: {exc}')
            continue
        if 'bootlog_' in src:
            errors.append(f'{rel}: 检测到已有日志补丁，拒绝重复应用')
            continue
        for label, old, new in edits:
            if old == '@SETUP_ARCH@':
                signatures = re.findall(r'^void __init (?:__no_sanitize_address )?setup_arch\(char \*\*cmdline_p\)\n', src, re.M)
                old = signatures[0] if len(signatures) == 1 else '@missing-setup-arch@'
                new = helpers + old
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
    if not args.check:
        for path, src in pending.items():
            path.write_text(src, encoding='utf-8', errors='surrogateescape', newline='\n')
    print('\n'.join('OK ' + item for item in applied))
    print(f'kernel=6.1.{version[2].group(1)} profile={args.profile}')
    if args.check:
        print(f'checked={len(applied)} expected={expected} failed=0; no files written')
        print('boot log preflight OK')
        return 0
    print(f'applied={len(applied)} expected={expected} failed=0')
    print('boot log patch OK')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
