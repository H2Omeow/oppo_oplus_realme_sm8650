#!/usr/bin/env python3
"""
VA48-with-VA39-kernel-layout patcher  (oneplus sm8650, kernel 6.1.118 GKI)

GOAL
  EAC (VRChat) needs ten MAP_FIXED PAGE_EXECUTE_READWRITE pages at
  0x700100000000 .. 0x700a00000000  (~112 TiB).  VA39 user ceiling is 512 GiB,
  so all ten fail -> "Failed to initialize Wine helper buffer" / error 60105.

  48-bit USER VA requires CONFIG_ARM64_VA_BITS=48 -> CONFIG_PGTABLE_LEVELS=4.
  A naive config flip ALSO moves the whole KERNEL side of the address space
  (PAGE_OFFSET 0xffffff8000000000 -> 0xffff000000000000), which breaks every
  prebuilt vendor module that inlined __va() / vmemmap / VMALLOC bounds.

STRATEGY
  TCR_EL1.T0SZ (user) and T1SZ (kernel) are architecturally independent.
  So: build VA_BITS=48 (user gets 256 TiB via TASK_SIZE_64 = 1<<vabits_actual)
  while pinning the ENTIRE kernel-side layout to its VA39 values, so that
  PAGE_OFFSET / PAGE_END / MODULES_VADDR / KIMAGE_VADDR / VMALLOC_START /
  VMEMMAP_START are bit-identical to what the 483 prebuilt .ko files expect.

CONSEQUENCE (why this needs more than memory.h)
  With 4 levels PGDIR_SHIFT becomes 39, so a linear map pinned at
  -(1<<39) collapses the linear map AND modules/vmalloc/fixmap/vmemmap into a
  single PGD entry (511).  Three arm64 assertions assume that never happens on
  4K pages, and idmap_t0sz is derived from VA_BITS_MIN.  All four must move.

PATCH SITES
  A memory.h  : introduce KERNEL_VA_BITS=39; pin PAGE_OFFSET, VA_BITS_MIN,
                VMEMMAP_START   (PAGE_END/MODULES_VADDR/VMEMMAP_SIZE follow
                VA_BITS_MIN automatically)
  B mm/init.c : linear_region_size uses _PAGE_OFFSET(vabits_actual) directly,
                which would yield 255.75 TiB instead of 256 GiB and then drive
                memstart_addr far below real DRAM via CONFIG_RANDOMIZE_BASE
                -> silent total corruption.  Use PAGE_OFFSET.
  C mm/mmu.c  : map_mem BUILD_BUG_ON(pgd_index(direct_map_end-1)==...)
                Guards hierarchical PXNTable on a shared table.  Cannot
                materialise here: early_fixmap_init + map_kernel populate
                pgd511's p4d entry BEFORE map_mem, so alloc_init_pud's
                `if (p4d_none(p4d))` is false and NO_EXEC_MAPPINGS' P4D_TABLE_PXN
                is never applied to the shared entry.  PXN still applies at PUD
                level to the linear map's own puds (idx 0-255) and at PTE level.
  D mm/mmu.c  : map_kernel BUG_ON(!IS_ENABLED(CONFIG_ARM64_16K_PAGES))
  E mm/mmu.c  : early_fixmap_init same BUG_ON
                Both only assert "shared top-level pgd should only happen on
                16k/4levels".  The pud-reuse code path they guard is granule
                agnostic and is exactly what we need on 4k/4levels.
  F mm/mmu.c  : idmap_t0sz = 63 - __fls(pa | GENMASK(VA_BITS_MIN-1,0))
                With VA_BITS_MIN=39 -> t0sz=25 -> 39-bit TTBR0 input -> HW
                starts the walk at level 1, but idmap_pg_dir is a level-0 table
                (IDMAP_PGD_ORDER = PHYS_MASK_SHIFT-PGDIR_SHIFT = 9).  Fatal.
                Use VA_BITS (48) -> t0sz=16 -> level-0 start.  Matches stock.
  G kernel/module/version.c : check_version() -> early return 1.
                MEASURED: flipping to 4 levels changes the CRC of module_layout
                (0xea759d7f -> 0x248147a7), _dev_err, kmalloc_caches, ... while
                VA-independent symbols (strlen/memcpy/jiffies/_printk) are
                byte-identical.  All 143 first-stage modules therefore fail
                check_version() and are rejected with -ENOEXEC.  No UFS, no
                SMMU, no /data -> first splash then reset.  CONFIG_MODVERSIONS
                stays =y so vermagic still matches in same_magic().

Run from the kernel source root:  python3 patch_va48.py [tree_root]
Exits non-zero if ANY anchor is missing (never silently half-patches).
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


# ---------------------------------------------------------------- A: memory.h
patch("arch/arm64/include/asm/memory.h", [
    (
        "A1 introduce KERNEL_VA_BITS",
        "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)\n",
        "#define VA_BITS\t\t\t(CONFIG_ARM64_VA_BITS)\n"
        "/*\n"
        " * VA48-USER / VA39-KERNEL decoupling.\n"
        " * VA_BITS drives the USER half (TASK_SIZE_64 = 1<<vabits_actual) so EAC\n"
        " * can place its fixed 112 TiB pages.  KERNEL_VA_BITS pins the KERNEL half\n"
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
        " * via arch_get_mmap_end(), which is what EAC relies on.\n"
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

EXPECT = 10
if len(applied) != EXPECT:
    print(f"\nERROR: expected {EXPECT} edits, applied {len(applied)}.")
    sys.exit(1)

print("\nall 10 edits applied")
sys.exit(0)
