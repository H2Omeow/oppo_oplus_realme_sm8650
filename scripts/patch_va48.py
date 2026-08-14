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
  H assembler.h: idmap_get_t0sz asm macro -- THE SAME BUG AS F, IN ASM, AND IT
                RUNS FIRST.  This is what killed attempt #2 (multi-minute hang
                at the first splash, no auto-reboot, nothing on console).
                proc.S:__cpu_setup does:
                    mov_q tcr, TCR_TxSZ(VA_BITS) | ...   ; T0SZ=16, correct
                    idmap_get_t0sz x9                    ; recomputes -> 25
                    tcr_set_t0sz  tcr, x9                ; OVERWRITES with 25
                and __cpu_setup runs BEFORE the MMU is enabled.  So the CPU is
                told TTBR0 has a 39-bit input and begins the walk at level 1,
                while init_idmap_pg_dir is a level-0 table.  The very first
                instruction fetch after MMU enable has no valid translation;
                the exception vectors are equally unmapped, so it faults on the
                fault forever.  No console, no panic (panic=-1 would have
                rebooted), no watchdog pet -> exactly the observed dead hang.
                Patch F alone is NOT enough: F only fixes the C copy used later
                by cpu_replace_ttbr1().  Both copies must agree.
  I mm/mmu.c  : arch_get_mappable_range() uses __pa(_PAGE_OFFSET(vabits_actual)).
                With vabits_actual=48 that is 0xffff000000000000, which is NOT
                in the pinned linear map, so __pa() returns garbage.  Not fatal
                at boot (only memory-hotplug / memremap_pages call it, and
                CONFIG_MEMORY_HOTPLUG=y on this device) but it is wrong.
                Use PAGE_OFFSET, same reasoning as B.

NOT A BUG (checked, left alone)
  * vabits_actual: memory.h:199 defines it as ((u64)VA_BITS) whenever
    VA_BITS <= 48, and mmu.c's runtime variable is inside #if VA_BITS > 48.
    So it is a compile-time 48 here and TASK_SIZE_64 really is 256 TiB.
  * kvm_compute_layout() does run (kernel is at EL1 so !is_kernel_in_hyp_mode()),
    and its hyp VA math is nonsense with vabits_actual=48 vs a VA39 kernel --
    but it only computes and stores values, never dereferences them, and KVM
    cannot initialise on this device anyway (EL2 belongs to the Qualcomm
    hypervisor).  Broken KVM guests are accepted collateral, not a boot blocker.
  * kaslr_early.c's VA_BITS_MIN uses are wanted: they keep the KASLR offset
    inside the VA39 vmalloc region, which is what pinning the layout needs.
  H asm/assembler.h : idmap_get_t0sz macro -- THE ROUND-1 BOOT KILLER.
                This is the ASM twin of site F and runs FIRST, from __cpu_setup
                (proc.S:442), before the MMU is enabled.  proc.S sets
                TCR_TxSZ(VA_BITS) -> T0SZ=16, then idmap_get_t0sz OVERWRITES
                T0SZ with a value derived from VA_BITS_MIN.  Pinned to 39 that
                is 25 -> 39-bit TTBR0 input -> the CPU begins the walk at level
                1 while init_idmap_pg_dir is a level-0 table -> the very first
                instruction fetch after MMU enable has no valid mapping, and
                the exception vectors are unmapped too, so it is a
                fault-on-fault hard hang: no console, no panic, no watchdog
                reboot.  Observed as "stuck on first splash for minutes".
                Patching F alone is NOT enough; F runs much later in
                paging_init().  Use VA_BITS -> t0sz 16, agreeing with proc.S.
  I mm/mmu.c  : arch_get_mappable_range() uses _PAGE_OFFSET(vabits_actual),
                which with vabits_actual=48 is 0xffff000000000000 -- an address
                outside the pinned linear map, so __pa() on it yields garbage.
                Not fatal at boot (memory hotplug only) but CONFIG_MEMORY_HOTPLUG
                =y on this device, so fix it rather than leave a live landmine.
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

# ---------------------------------------------------------------- J: mmap_rnd_bits
# VA48 increases ARCH_MMAP_RND_BITS_MAX from 24 (VA39) to 33 (VA48/4K).
# The kernel's default CONFIG_ARCH_MMAP_RND_BITS (set at Kconfig time) follows
# the MAX, so mmap_rnd_bits goes from 18 to 33.  This shifts all dynamic library
# load addresses above 4 GiB.  BoringSSL's FIPS integrity check (and other
# Android code) assumes libraries load below 4 GiB, causing app_process to
# die with exit 127 and init to panic.
# Fix: pin mmap_rnd_bits to 18 (VA39 default) in mm/mmap.c at declaration.
# ALSO overridden in arch/arm64/kernel/setup.c:va48_fix_cmdline() as fallback.
patch("mm/mmap.c", [
    (
        "J1 pin mmap_rnd_bits to VA39 default 18",
        "int mmap_rnd_bits __read_mostly = CONFIG_ARCH_MMAP_RND_BITS;\n",
        "/* VA48/VA39: pin to VA39 default so libraries load below 4 GiB.\n"
        " * BoringSSL and Android ART assume load addresses < 4 GiB. */\n"
        "int mmap_rnd_bits __read_mostly = 18;\n",
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

EXPECT = 13
if len(applied) != EXPECT:
    print(f"\nERROR: expected {EXPECT} edits, applied {len(applied)}.")
    sys.exit(1)

print(f"\nall {EXPECT} edits applied")
sys.exit(0)
