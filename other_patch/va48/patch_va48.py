#!/usr/bin/env python3
"""VA48 用户空间扩展补丁：保留 VA39 内核布局，适配已知 ARM64 6.1.x 源码。

基础 r16 仅在一加 ACE5、6.1.118 上测试正常开机；其他版本和设备未实机
验证。源码预检通过仅表示已知结构匹配，不能代替编译、模块和开机验证。

用户和内核地址宽度分别由 TCR_EL1.T0SZ/T1SZ 控制。本补丁保留原 r16 的
14 项修改：用户态 VA48、内核态 VA39、4 KiB 页面和四级页表、24 位 mmap
ASLR 上限及 mmap_base 越界保护。仍跳过模块符号 CRC 比较并保留 vermagic
检查；放宽检查不能保证任意厂商模块 ABI 兼容。

用法：python3 patch_va48.py [tree_root] [--check] [--config .config]
--check 只预检，不写入源码。可在生成配置后用 --config 检查 ARM64、4 KiB
页面及地址位数前提。任一版本、前提或锚点不符合时，所有源码保持原样。
"""

import argparse
from pathlib import Path
import os
import re
import sys
import tempfile


EXPECT = 14
KERNEL_VA_BITS = 39


def whitespace_pattern(text):
    """只容忍同一行中已有水平空白的宽度变化，不跨行吞并代码。"""
    lines = []
    for line in text.rstrip("\n").split("\n"):
        parts = re.split(r"([ \t]+)", line)
        lines.append("".join(r"[ \t]+" if part and part.isspace()
                             else re.escape(part) for part in parts))
    return re.compile(r"^" + r"\n".join(lines) + r"$", re.M)


class PatchPlan:
    def __init__(self, root):
        self.root = root
        self.original = {}
        self.sources = {}
        self.newlines = {}
        self.applied = []
        self.failed = []

    def read(self, relpath):
        if relpath in self.sources:
            return self.sources[relpath]
        try:
            raw = (self.root / relpath).read_bytes()
        except OSError as exc:
            self.failed.append(f"{relpath}: 无法读取：{exc}")
            return None
        text = raw.decode("utf-8", errors="surrogateescape")
        normalized = text.replace("\r\n", "\n")
        if "\r" in normalized or ("\r\n" in text and "\n" in text.replace("\r\n", "")):
            self.failed.append(f"{relpath}: 不支持混合或单独 CR 换行，请先统一源码换行")
            return None
        self.original[relpath] = raw
        self.sources[relpath] = normalized
        self.newlines[relpath] = "\r\n" if "\r\n" in text else "\n"
        return normalized

    def require(self, relpath, anchor, description):
        source = self.read(relpath)
        if source is not None and len(list(whitespace_pattern(anchor).finditer(source))) != 1:
            self.failed.append(f"{relpath}: 前提不匹配：{description}")

    def edit(self, relpath, name, old, new):
        """在内存中应用一个明确锚点；匹配失败不改动磁盘。"""
        source = self.read(relpath)
        if source is None:
            return
        variants = old if isinstance(old, tuple) else (old,)
        matches = []
        for variant in variants:
            matches.extend(whitespace_pattern(variant).finditer(source))
        if len(matches) != 1:
            self.failed.append(f"{relpath} [{name}]: 已知锚点匹配 {len(matches)} 处，应为 1 处；源码可能已修改或版本不兼容")
            return
        match = matches[0]
        self.sources[relpath] = source[:match.start()] + new.rstrip("\n") + source[match.end():]
        self.applied.append(f"{relpath} [{name}]")

    def write(self):
        """全部预检成功后再替换文件；预先落盘临时文件，失败时恢复已替换文件。"""
        staged = {}
        replaced = []
        try:
            for relpath, source in self.sources.items():
                raw = source.replace("\n", self.newlines[relpath]).encode("utf-8", errors="surrogateescape")
                if raw == self.original[relpath]:
                    continue
                path = self.root / relpath
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".va48-", delete=False) as temporary:
                    staged[relpath] = Path(temporary.name)
                    temporary.write(raw)
                os.chmod(staged[relpath], path.stat().st_mode)
            # 防止预检期间源码被其他操作修改。
            for relpath in staged:
                if (self.root / relpath).read_bytes() != self.original[relpath]:
                    raise OSError(f"{relpath}: 预检后源码发生变化，取消写入")
            for relpath, temporary in staged.items():
                os.replace(temporary, self.root / relpath)
                replaced.append(relpath)
        except OSError:
            for relpath in replaced:
                (self.root / relpath).write_bytes(self.original[relpath])
            raise
        finally:
            for temporary in staged.values():
                if temporary.exists():
                    temporary.unlink()


def check_version(plan):
    source = plan.read("Makefile")
    if source is None:
        return
    version = []
    for field in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        values = re.findall(r"^" + field + r"[ \t]*=[ \t]*([0-9]+)[ \t]*$", source, re.M)
        if len(values) != 1:
            plan.failed.append(f"Makefile: 无法唯一确定 {field}")
            return
        version.append(int(values[0]))
    if version[:2] != [6, 1]:
        plan.failed.append(f"Makefile: 仅支持 ARM64 6.1.x，当前为 {'.'.join(map(str, version))}")
    else:
        print("源码版本：" + ".".join(map(str, version)))


def check_config(plan, config_path):
    if config_path is None:
        print("未指定生成配置；应用后仍须检查 ARM64、4 KiB、VA48、四级页表和 mmap ASLR 上限。")
        return
    try:
        config = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        plan.failed.append(f"配置 {config_path}: 无法读取：{exc}")
        return
    values = dict(re.findall(r"^(CONFIG_[A-Z0-9_]+)=(\S+)$", config, re.M))
    for name in ("CONFIG_ARM64", "CONFIG_ARM64_4K_PAGES"):
        if values.get(name) != "y":
            plan.failed.append(f"配置前提：{name}=y 是必需项（请提供生成的 .config）")
    for name in ("CONFIG_ARM64_16K_PAGES", "CONFIG_ARM64_64K_PAGES", "CONFIG_KASAN_GENERIC", "CONFIG_KASAN_SW_TAGS"):
        if values.get(name) == "y":
            plan.failed.append(f"配置前提：不支持 {name}=y")
    va_bits = values.get("CONFIG_ARM64_VA_BITS")
    if va_bits not in ("39", "48"):
        plan.failed.append("配置前提：CONFIG_ARM64_VA_BITS 必须为 39（应用前）或 48（最终配置）")
    expected_levels = {"39": "3", "48": "4"}.get(va_bits)
    if expected_levels is not None and values.get("CONFIG_PGTABLE_LEVELS") != expected_levels:
        plan.failed.append(f"配置前提：VA{va_bits}/4 KiB 应使用 {expected_levels} 级页表")


def build_plan(plan):
    memory = "arch/arm64/include/asm/memory.h"
    mmu = "arch/arm64/mm/mmu.c"
    kconfig = "arch/arm64/Kconfig"

    # 页表级数与布局推导必须仍符合原 r16 的结构，不能靠放宽锚点适配新架构。
    for anchor, description in (
        ("config ARM64_4K_PAGES", "ARM64 4 KiB 页面配置"),
        ("config ARM64_VA_BITS_48", "ARM64 VA48 配置"),
        ("\tdefault 3 if ARM64_4K_PAGES && ARM64_VA_BITS_39", "VA39/4 KiB 三级页表"),
        ("\tdefault 4 if !ARM64_64K_PAGES && ARM64_VA_BITS_48", "VA48/4 KiB 四级页表"),
    ):
        plan.require(kconfig, anchor, description)
    for anchor, description in (
        ("#define VMEMMAP_SIZE\t((_PAGE_END(VA_BITS_MIN) - PAGE_OFFSET) >> VMEMMAP_SHIFT)", "VMEMMAP_SIZE 依赖 VA_BITS_MIN"),
        ("#define PAGE_END\t\t(_PAGE_END(VA_BITS_MIN))", "普通 PAGE_END 依赖 VA_BITS_MIN"),
        ("#define MODULES_VADDR\t\t(_PAGE_END(VA_BITS_MIN))", "模块起始地址依赖 VA_BITS_MIN"),
    ):
        plan.require(memory, anchor, description)
    original_memory = plan.read(memory)
    if original_memory is not None and re.search(r"^#define[ \t]+KERNEL_VA_BITS\b", original_memory, re.M):
        plan.failed.append(f"{memory}: 已有 KERNEL_VA_BITS，拒绝重复应用或覆盖其他补丁")

    # A：固定内核布局；用户地址宽度仍由 CONFIG_ARM64_VA_BITS 决定。
    plan.edit(memory, "A1 introduce KERNEL_VA_BITS",
              "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)",
              "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)\n"
              "/*\n"
              " * 用户态 VA48 / 内核态 VA39：用户可显式请求高地址映射，\n"
              " * 内核保持厂商预编译模块依赖的 PAGE_OFFSET 和 vmemmap 布局。\n"
              " * 基础 r16 仅在一加 ACE5、6.1.118 测试正常开机，其他未实机验证。\n"
              " */\n"
              f"#define KERNEL_VA_BITS\t\t({KERNEL_VA_BITS})")
    plan.edit(memory, "A2 pin PAGE_OFFSET",
              "#define PAGE_OFFSET\t\t(_PAGE_OFFSET(VA_BITS))",
              "#define PAGE_OFFSET\t\t(_PAGE_OFFSET(KERNEL_VA_BITS))")
    plan.edit(memory, "A3 pin VMEMMAP_START",
              "#define VMEMMAP_START\t\t(-(UL(1) << (VA_BITS - VMEMMAP_SHIFT)))",
              "#define VMEMMAP_START\t\t(-(UL(1) << (KERNEL_VA_BITS - VMEMMAP_SHIFT)))")
    plan.edit(memory, "A4 pin VA_BITS_MIN",
              "#if VA_BITS > 48\n#define VA_BITS_MIN\t\t(48)\n#else\n#define VA_BITS_MIN\t\t(VA_BITS)\n#endif",
              "/* 内核映射和默认 mmap 窗口保留 VA39；显式高地址映射仍可到达 TASK_SIZE。 */\n"
              "#define VA_BITS_MIN\t\t(KERNEL_VA_BITS)")

    plan.edit("arch/arm64/mm/init.c", "B1 linear_region_size",
              "\ts64 linear_region_size = PAGE_END - _PAGE_OFFSET(vabits_actual);",
              "\t/* 使用固定后的内核线性映射大小，避免按 VA48 计算随机物理基址。 */\n"
              "\ts64 linear_region_size = PAGE_END - PAGE_OFFSET;")

    # C/D/E：4 KiB 四级页表下，VA39 内核映射共用顶层目录。
    plan.edit(mmu, "C1 map_mem PGD-sharing BUILD_BUG_ON",
              "\tBUILD_BUG_ON(pgd_index(direct_map_end - 1) == pgd_index(direct_map_end));",
              "\t/* 四级页表的 PGD 511 被线性映射和内核其他映射共享；\n"
              "\t * early_fixmap_init/map_kernel 已建立共享表项，线性映射的 PXN\n"
              "\t * 仍由自身 PUD/PTE 执行。保留原 r16 的共享目录处理。 */\n"
              "\tBUILD_BUG_ON(0);")
    plan.edit(mmu, "D1 map_kernel fixmap pud-reuse BUG_ON",
              "\t\tBUG_ON(!IS_ENABLED(CONFIG_ARM64_16K_PAGES));\n\t\tbm_pgdp = pgd_offset_pgd(pgdp, FIXADDR_START);",
              "\t\t/* VA48/VA39 的 4 KiB 四级页表也需复用此 PUD。 */\n"
              "\t\tbm_pgdp = pgd_offset_pgd(pgdp, FIXADDR_START);")
    plan.edit(mmu, "E1 early_fixmap_init BUG_ON",
              "\t\tBUG_ON(!IS_ENABLED(CONFIG_ARM64_16K_PAGES));\n\t\tpudp = pud_offset_kimg(p4dp, addr);",
              "\t\t/* VA48/VA39 的 4 KiB 四级页表也进入共享 PUD 路径。 */\n"
              "\t\tpudp = pud_offset_kimg(p4dp, addr);")
    plan.edit(mmu, "F1 idmap_t0sz uses VA_BITS not VA_BITS_MIN",
              "\tidmap_t0sz = 63UL - __fls(__pa_symbol(_end) | GENMASK(VA_BITS_MIN - 1, 0));",
              "\t/* 恒等映射使用 VA48 四级页表，T0SZ 必须按 VA_BITS 计算。 */\n"
              "\tidmap_t0sz = 63UL - __fls(__pa_symbol(_end) | GENMASK(VA_BITS - 1, 0));")

    # H：首次启用 MMU 前执行的汇编路径必须与 C 侧恒等映射计算一致。
    plan.edit("arch/arm64/include/asm/assembler.h", "H1 idmap_get_t0sz asm uses VA_BITS not VA_BITS_MIN",
              "\t.macro\tidmap_get_t0sz, reg\n\tadrp\t\\reg, _end\n"
              "\torr\t\\reg, \\reg, #(1 << VA_BITS_MIN) - 1\n\tclz\t\\reg, \\reg",
              "\t/* 恒等映射为四级页表，不能使用固定为 39 的 VA_BITS_MIN。 */\n"
              "\t.macro\tidmap_get_t0sz, reg\n\tadrp\t\\reg, _end\n"
              "\torr\t\\reg, \\reg, #(1 << VA_BITS) - 1\n\tclz\t\\reg, \\reg")
    plan.edit(mmu, "I1 arch_get_mappable_range pins linear map base",
              "\tu64 start_linear_pa = __pa(_PAGE_OFFSET(vabits_actual));",
              "\t/* 热插拔与 memremap_pages 也必须以真实 VA39 线性映射起点计算。 */\n"
              "\tu64 start_linear_pa = __pa(PAGE_OFFSET);")
    plan.edit("mm/util.c", "J1 clamp mmap_base underflow",
              "\t\tmm->mmap_base = mmap_base(random_factor, rlim_stack);",
              "\t\tmm->mmap_base = mmap_base(random_factor, rlim_stack);\n"
              "\t\t/* 默认分配保留 VA39 窗口；越界时以 STACK_TOP 恢复，兼顾 AArch32。\n"
              "\t\t * 24 位随机化上限负责常规约束，此处防止 mmap_base 下溢。 */\n"
              "\t\tif (mm->mmap_base >= STACK_TOP)\n"
              "\t\t\tmm->mmap_base = STACK_TOP - (STACK_TOP >> 3);")

    # K：仅接受已知的两种 VA48 ASLR 默认值写法，不匹配任意 Kconfig 表达式。
    plan.edit(kconfig, "K1 cap ARCH_MMAP_RND_BITS_MAX at the VA39 value",
              ("\tdefault 33 if ARM64_VA_BITS=48",
               "\tdefault 33 if (ARM64_VA_BITS=48 || ARM64_VA_BITS=52)"),
              "# 默认 mmap 窗口保留 VA39，随机偏移上限也必须保留原 24 位，\n"
              "# 避免 VA48 默认的 33 位偏移使 mmap_base 越过 STACK_TOP。\n"
              "\tdefault 24 if (ARM64_VA_BITS=48 || ARM64_VA_BITS=52)")
    plan.edit("kernel/module/version.c", "G1 check_version bypass",
              "\tElf_Shdr *sechdrs = info->sechdrs;\n"
              "\tunsigned int versindex = info->index.vers;\n"
              "\tunsigned int i, num_versions;\n\tstruct modversion_info *versions;",
              "\tElf_Shdr *sechdrs = info->sechdrs;\n"
              "\tunsigned int versindex = info->index.vers;\n"
              "\tunsigned int i, num_versions;\n\tstruct modversion_info *versions;\n\n"
              "\t/* 原 r16 保留的模块 CRC 放宽：页表层级变化会影响 genksyms CRC。\n"
              "\t * 保留 CONFIG_MODVERSIONS 和 vermagic；跳过 CRC 不能保证任意 ABI\n"
              "\t * 兼容，仍需使用与目标设备和内核版本匹配的厂商模块。 */\n"
              "\treturn 1;")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tree_root", nargs="?", default=".", help="内核源码根目录")
    parser.add_argument("--check", action="store_true", help="只检查全部前提和 14 处锚点，不修改文件")
    parser.add_argument("--config", type=Path, help="生成的内核 .config；相对路径以源码根目录为基准")
    args = parser.parse_args(argv)
    root = Path(args.tree_root).resolve()
    plan = PatchPlan(root)
    check_version(plan)
    config_path = args.config
    if config_path is not None and not config_path.is_absolute():
        config_path = root / config_path
    if config_path is None and (root / ".config").exists():
        config_path = root / ".config"
    check_config(plan, config_path)
    build_plan(plan)
    if len(plan.applied) != EXPECT:
        plan.failed.append(f"预期 {EXPECT} 项修改，实际仅有 {len(plan.applied)} 项锚点通过")
    print("=" * 68)
    for entry in plan.applied:
        print("  OK    " + entry)
    for entry in plan.failed:
        print("  FAIL  " + entry)
    if plan.failed:
        print(f"checked={len(plan.applied)} failed={len(plan.failed)}")
        print("预检失败：未修改任何源码。请检查源码分支、配置或已有补丁。")
        return 1
    if args.check:
        print(f"checked={EXPECT} failed=0")
        print(f"全部 {EXPECT} 项预检通过；--check 未修改文件。")
        return 0
    try:
        plan.write()
    except OSError as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        return 1
    print(f"applied={EXPECT} failed=0")
    print(f"all {EXPECT} edits applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
