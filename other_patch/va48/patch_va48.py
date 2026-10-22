#!/usr/bin/env python3
"""
VA48 用户空间扩展补丁：保留 VA39 内核布局（SM8650 / 6.1.118）。

已测试范围：用户已在一加 ACE5、6.1.118 上验证基础 r16 正常开机；
其他机型和内核版本未测试。本声明不代表新增屏幕日志已通过实机验证。

用途：将用户空间地址上限从 512 GiB 扩展到 256 TiB，允许高地址固定映射，
同时保留默认 VA39 分配窗口和预编译厂商模块所依赖的内核虚拟地址布局。

关键处理：
  A：新增 KERNEL_VA_BITS=39，固定 PAGE_OFFSET、VA_BITS_MIN、VMEMMAP_START。
  B/I：线性映射大小和可映射物理范围使用固定后的 PAGE_OFFSET。
  C/D/E：适配 4 KiB 页面、四级页表下共享顶层目录的 VA39 内核布局。
  F/H：C 与汇编中的恒等映射均按 VA_BITS=48 计算 T0SZ，匹配四级页表。
  G：保留 CONFIG_MODVERSIONS，但跳过符号版本 CRC 比较，以兼容已有厂商模块。
       这会放宽模块 ABI 检查，不能由此保证任意模块兼容。
  K：将 mmap ASLR 位数上限限制为 24，防止随机偏移超过 VA39 默认分配窗口。
  J：mmap_base 越界时夹回 STACK_TOP 对应的 VA39 窗口，兼顾 AArch32 任务。

注意：用户和内核地址宽度分别由 TCR_EL1.T0SZ/T1SZ 控制。仅修改配置会移动
内核布局；本补丁同时调整相关地址计算和页表初始化。KVM 等依赖完整 VA48
内核布局的功能不在已验证范围内。

在内核源码根目录运行：python3 patch_va48.py [tree_root]
任一必需锚点缺失时返回非零，构建工作流同时校验 14 处修改和生成配置。
"""

import sys, os, re

KERNEL_VA_BITS = 39

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

applied, failed = [], []


def patch(relpath, edits, required=True):
    p = os.path.join(ROOT, relpath)
    if not os.path.exists(p):
        failed.append(f"{relpath}: FILE NOT FOUND")
        return
    with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
        src = f.read()
    orig = src
    for name, old, new in edits:
        n = src.count(old)
        if n == 0:
            if "already" in name:
                continue
            failed.append(f"{relpath} [{name}]: anchor not found")
            continue
        if n > 1:
            failed.append(f"{relpath} [{name}]: anchor matched {n}x (ambiguous)")
            continue
        src = src.replace(old, new, 1)
        applied.append(f"{relpath} [{name}]")
    if src != orig:
        with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(src)


def patch_re(relpath, edits):
    """Same contract as patch(), but anchors are multiline regexes.

    Used where the OEM/GKI tree diverges textually from mainline for a site we
    still need to hit exactly once.  Kept separate from patch() so the plain
    string anchors above stay easy to eyeball against the source.
    """
    p = os.path.join(ROOT, relpath)
    if not os.path.exists(p):
        failed.append(f"{relpath}: FILE NOT FOUND")
        return
    with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
        src = f.read()
    orig = src
    for name, pattern, new in edits:
        rx = re.compile(pattern, re.M)
        n = len(rx.findall(src))
        if n != 1:
            failed.append(f"{relpath} [{name}]: regex matched {n}x (want 1)")
            continue
        # lambda replacement: no backslash/group interpretation in `new`
        src = rx.sub(lambda m: new, src, count=1)
        applied.append(f"{relpath} [{name}]")
    if src != orig:
        with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(src)


# ---------------------------------------------------------------- A: memory.h
patch("arch/arm64/include/asm/memory.h", [
    (
        "A1 introduce KERNEL_VA_BITS",
        "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)\n",
        "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)\n"
        "/*\n"
        " * VA48-USER / VA39-KERNEL decoupling.\n"
        " * Base r16 boot tested on OnePlus ACE5 / 6.1.118; other devices untested.\n"
        " * VA_BITS drives the USER half (TASK_SIZE_64 = 1<<vabits_actual) so apps\n"
        " * can request high-address mappings. KERNEL_VA_BITS pins the KERNEL half\n"
        " * to its 39-bit layout so prebuilt vendor modules that inlined PAGE_OFFSET,\n"
        " * PAGE_END, VMALLOC_START and vmemmap stay correct.\n"
        " */\n"
        f"#define KERNEL_VA_BITS\t\t({KERNEL_VA_BITS})\n",
    ),
    (
        "A2 pin PAGE_OFFSET",
        "#define PAGE_OFFSET\t\t(_PAGE_OFFSET(VA_BITS))\n",
        "#define PAGE_OFFSET\t\t(_PAGE_OFFSET(KERNEL_VA_BITS))\n",
    ),
    (
        "A3 pin VMEMMAP_START",
        "#define VMEMMAP_START\t\t(-(UL(1) << (VA_BITS - VMEMMAP_SHIFT)))\n",
        "#define VMEMMAP_START\t\t(-(UL(1) << (KERNEL_VA_BITS - VMEMMAP_SHIFT)))\n",
    ),
    (
        "A4 pin VA_BITS_MIN",
        "#if VA_BITS > 48\n"
        "#define VA_BITS_MIN\t\t(48)\n"
        "#else\n"
        "#define VA_BITS_MIN\t\t(VA_BITS)\n"
        "#endif\n",
        "/*\n"
        " * Pinned: PAGE_END, MODULES_VADDR, VMEMMAP_SIZE and DEFAULT_MAP_WINDOW_64\n"
        " * all derive from VA_BITS_MIN.  Keeping it at 39 keeps the kernel map and\n"
        " * the default mmap window exactly where stock VA39 put them; explicit\n"
        " * MAP_FIXED hints above DEFAULT_MAP_WINDOW still reach TASK_SIZE (256 TiB)\n"
        " * via arch_get_mmap_end(), preserving explicit high-address mappings.\n"
        " */\n"
        "#define VA_BITS_MIN\t\t(KERNEL_VA_BITS)\n",
    ),
])

# ---------------------------------------------------------------- B: init.c
patch("arch/arm64/mm/init.c", [
    (
        "B1 linear_region_size",
        "\ts64 linear_region_size = PAGE_END - _PAGE_OFFSET(vabits_actual);\n",
        "\t/* VA48/VA39: vabits_actual is 48 but the linear map is pinned at\n"
        "\t * KERNEL_VA_BITS.  Using _PAGE_OFFSET(vabits_actual) here would give\n"
        "\t * 255.75 TiB instead of 256 GiB and drive memstart_addr far below\n"
        "\t * real DRAM through the CONFIG_RANDOMIZE_BASE block below. */\n"
        "\ts64 linear_region_size = PAGE_END - PAGE_OFFSET;\n",
    ),
])

# ---------------------------------------------------------------- C-F: mmu.c
patch("arch/arm64/mm/mmu.c", [
    (
        "C1 map_mem PGD-sharing BUILD_BUG_ON",
        "\tBUILD_BUG_ON(pgd_index(direct_map_end - 1) == pgd_index(direct_map_end));\n",
        "\t/* VA48/VA39: with 4 levels PGDIR_SHIFT==39, so the pinned linear map\n"
        "\t * shares PGD 511 with modules/vmalloc/fixmap/vmemmap.  Harmless here:\n"
        "\t * early_fixmap_init() and map_kernel() populate that p4d entry before\n"
        "\t * map_mem() runs, so alloc_init_pud()'s p4d_none() test is false and\n"
        "\t * P4D_TABLE_PXN is never applied to the shared entry.  PXN is still\n"
        "\t * enforced on the linear map's own PUDs (idx 0-255) and at PTE level. */\n"
        "\tBUILD_BUG_ON(0);\n",
    ),
    (
        "D1 map_kernel fixmap pud-reuse BUG_ON",
        "\t\tBUG_ON(!IS_ENABLED(CONFIG_ARM64_16K_PAGES));\n"
        "\t\tbm_pgdp = pgd_offset_pgd(pgdp, FIXADDR_START);\n",
        "\t\t/* VA48/VA39 on 4k/4levels also lands here; the pud-reuse path\n"
        "\t\t * below is granule agnostic. */\n"
        "\t\tbm_pgdp = pgd_offset_pgd(pgdp, FIXADDR_START);\n",
    ),
    (
        "E1 early_fixmap_init BUG_ON",
        "\t\tBUG_ON(!IS_ENABLED(CONFIG_ARM64_16K_PAGES));\n"
        "\t\tpudp = pud_offset_kimg(p4dp, addr);\n",
        "\t\t/* VA48/VA39 on 4k/4levels also lands here. */\n"
        "\t\tpudp = pud_offset_kimg(p4dp, addr);\n",
    ),
    (
        "F1 idmap_t0sz uses VA_BITS not VA_BITS_MIN",
        "\tidmap_t0sz = 63UL - __fls(__pa_symbol(_end) | GENMASK(VA_BITS_MIN - 1, 0));\n",
        "\t/* VA48/VA39: the idmap tables are built to VA_BITS depth\n"
        "\t * (IDMAP_PGD_ORDER = PHYS_MASK_SHIFT - PGDIR_SHIFT = 9, a level-0\n"
        "\t * table).  Deriving t0sz from the pinned VA_BITS_MIN would give 25,\n"
        "\t * i.e. a 39-bit TTBR0 input that makes the CPU begin the walk at\n"
        "\t * level 1 and misread idmap_pg_dir.  Use VA_BITS -> t0sz 16. */\n"
        "\tidmap_t0sz = 63UL - __fls(__pa_symbol(_end) | GENMASK(VA_BITS - 1, 0));\n",
    ),
])

# ---------------------------------------------------------------- H: assembler.h
# THE ROUND-2 FIX.  This is the asm twin of patch F and it runs FIRST, in
# __cpu_setup, before the MMU is ever enabled.  Missing it is why round 1 hung.
patch("arch/arm64/include/asm/assembler.h", [
    (
        "H1 idmap_get_t0sz asm uses VA_BITS not VA_BITS_MIN",
        "\t.macro\tidmap_get_t0sz, reg\n"
        "\tadrp\t\\reg, _end\n"
        "\torr\t\\reg, \\reg, #(1 << VA_BITS_MIN) - 1\n"
        "\tclz\t\\reg, \\reg\n",
        "\t/*\n"
        "\t * VA48/VA39: asm twin of the idmap_t0sz computation in mmu.c.\n"
        "\t * proc.S:__cpu_setup loads TCR_TxSZ(VA_BITS) (T0SZ=16) and then\n"
        "\t * OVERWRITES T0SZ with this macro's result, before the MMU is\n"
        "\t * enabled.  With the pinned VA_BITS_MIN=39 it yields t0sz=25, i.e.\n"
        "\t * a 39-bit TTBR0 input, so the CPU starts the TTBR0 walk at level 1\n"
        "\t * while init_idmap_pg_dir is a level-0 table (IDMAP_PGD_ORDER =\n"
        "\t * PHYS_MASK_SHIFT - PGDIR_SHIFT = 9).  The very first instruction\n"
        "\t * fetch after MMU enable then translates through garbage, and the\n"
        "\t * fault vectors are unmapped too -> silent hard hang, no console,\n"
        "\t * no panic, no watchdog.  Must use VA_BITS -> t0sz=16.\n"
        "\t */\n"
        "\t.macro\tidmap_get_t0sz, reg\n"
        "\tadrp\t\\reg, _end\n"
        "\torr\t\\reg, \\reg, #(1 << VA_BITS) - 1\n"
        "\tclz\t\\reg, \\reg\n",
    ),
])

# ---------------------------------------------------------------- I: mmu.c hotplug
patch("arch/arm64/mm/mmu.c", [
    (
        "I1 arch_get_mappable_range pins linear map base",
        "\tu64 start_linear_pa = __pa(_PAGE_OFFSET(vabits_actual));\n",
        "\t/* VA48/VA39: vabits_actual is 48, but the linear map is pinned at\n"
        "\t * KERNEL_VA_BITS.  _PAGE_OFFSET(48) is 0xffff000000000000, which is\n"
        "\t * NOT in the linear map, so __pa() on it returns garbage and\n"
        "\t * memory hotplug / memremap_pages would compute a bogus range.\n"
        "\t * Same class of bug as patch B1. */\n"
        "\tu64 start_linear_pa = __pa(PAGE_OFFSET);\n",
    ),
])

# ---------------------------------------------------------------- J: util.c
patch("mm/util.c", [
    (
        "J1 clamp mmap_base underflow",
        "\tmm->mmap_base = mmap_base(random_factor, rlim_stack);\n",
        "\tmm->mmap_base = mmap_base(random_factor, rlim_stack);\n"
        "\t\t/* VA48/VA39: STACK_TOP is DEFAULT_MAP_WINDOW_64 (512 GiB, pinned by\n"
        "\t\t * VA_BITS_MIN) while TASK_SIZE_64 is 256 TiB, so mmap_base() =\n"
        "\t\t * STACK_TOP - gap - rnd wraps below zero if arch_mmap_rnd() ever\n"
        "\t\t * exceeds ~511 GiB.  Patch K1 caps mmap_rnd_bits at 24 (rnd <= 64\n"
        "\t\t * GiB) so it cannot; this is the last line of defence.\n"
        "\t\t *\n"
        "\t\t * Clamp back INTO the VA39 window, not to TASK_SIZE_64 / 4: with a\n"
        "\t\t * NULL hint arch_get_mmap_base() returns mmap_base unchanged, so a\n"
        "\t\t * 64 TiB base would put every mapping in the process at a 47-bit\n"
        "\t\t * address and break VA39-assuming userspace (ART, BoringSSL) just as\n"
        "\t\t * badly as the underflow, only more quietly.  STACK_TOP rather than\n"
        "\t\t * DEFAULT_MAP_WINDOW_64 so compat (AArch32) tasks clamp correctly. */\n"
        "\t\tif (mm->mmap_base >= STACK_TOP)\n"
        "\t\t\tmm->mmap_base = STACK_TOP - (STACK_TOP >> 3);\n",
    ),
])

# ---------------------------------------------------------------- K: arm64 Kconfig
# THE ROUND-3 FIX.  ARCH_MMAP_RND_BITS_MAX is derived from VA_BITS upstream, so
# it does NOT follow the VA_BITS_MIN pin and jumps 24 -> 33 when the VA_BITS
# choice is flipped.  See the K entry in the module docstring.
# Regex, not a plain anchor: mainline writes the 4K/VA48 default as
#     default 33 if ARM64_VA_BITS=48
# while the android14-6.1 GKI tree folds 52-bit VA into the same line as
#     default 33 if (ARM64_VA_BITS=48 || ARM64_VA_BITS=52)
# Both must hit, and exactly once.
patch_re("arch/arm64/Kconfig", [
    (
        "K1 cap ARCH_MMAP_RND_BITS_MAX at the VA39 value",
        r"^\tdefault 33 if "
        r"(?:\(ARM64_VA_BITS=48 \|\| ARM64_VA_BITS=52\)|ARM64_VA_BITS=48)$",
        "# VA48/VA39: userspace is pinned to the VA39 layout via VA_BITS_MIN, but\n"
        "# this default is derived from VA_BITS (48 - PAGE_SHIFT - 3 = 33) and so\n"
        "# does not follow the pin.  33 bits makes arch_mmap_rnd() return up to\n"
        "# 32 TiB, which underflows mmap_base() = STACK_TOP(512 GiB) - gap - rnd\n"
        "# for ~98% of execs and hands the process a kernel-space mmap_base.\n"
        "# 24 is the ARM64_VA_BITS=39 value, i.e. stock VA39 ASLR entropy.  This\n"
        "# also caps mmap_rnd_bits_max, so /proc/sys/vm/mmap_rnd_bits cannot be\n"
        "# raised back into the broken range at runtime.\n"
        "\tdefault 24 if (ARM64_VA_BITS=48 || ARM64_VA_BITS=52)",
    ),
])

# ---------------------------------------------------------------- G: version.c
patch("kernel/module/version.c", [
    (
        "G1 check_version bypass",
        "\tElf_Shdr *sechdrs = info->sechdrs;\n"
        "\tunsigned int versindex = info->index.vers;\n"
        "\tunsigned int i, num_versions;\n"
        "\tstruct modversion_info *versions;\n",
        "\tElf_Shdr *sechdrs = info->sechdrs;\n"
        "\tunsigned int versindex = info->index.vers;\n"
        "\tunsigned int i, num_versions;\n"
        "\tstruct modversion_info *versions;\n"
        "\n"
        "\t/*\n"
        "\t * VA48/VA39 build: CONFIG_PGTABLE_LEVELS 3->4 changes the genksyms\n"
        "\t * type graph, so symbols such as module_layout, _dev_err and\n"
        "\t * kmalloc_caches get new CRCs even though their in-memory layout is\n"
        "\t * unchanged (VA-independent symbols like strlen/memcpy/jiffies are\n"
        "\t * byte-identical, which is how we know the divergence is purely the\n"
        "\t * pgtable-level fold).  Without this, all 143 first-stage vendor\n"
        "\t * modules are rejected -ENOEXEC and the device cannot mount /data.\n"
        "\t * vermagic is untouched (CONFIG_MODVERSIONS stays =y) so same_magic()\n"
        "\t * still gates genuinely foreign modules.\n"
        "\t */\n"
        "\treturn 1;\n",
    ),
])

# ---------------------------------------------------------------- report
print("=" * 68)
for a in applied:
    print("  OK    " + a)
for f in failed:
    print("  FAIL  " + f)
print("=" * 68)
print(f"applied={len(applied)} failed={len(failed)}")

if failed:
    print("\nERROR: incomplete patch set - refusing to continue.")
    print("A partially patched tree builds fine and then dies before console output.")
    sys.exit(1)

EXPECT = 14
if len(applied) != EXPECT:
    print(f"\nERROR: expected {EXPECT} edits, applied {len(applied)}.")
    sys.exit(1)

print(f"\nall {EXPECT} edits applied")
sys.exit(0)
