#!/usr/bin/env python3
"""Run with --source GRAPHICS_KERNEL for each audited tree, plus --cpu-patch FILE."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("gpu_patch", HERE / "patch_gpu_va48.py")
gpu_patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gpu_patch)
SOURCE = None
SOURCES = []
CPU_PATCH = None
CC = None


def c_function(source, name):
    """Extract the actual patched C body, rather than reproducing its algorithm."""
    match = re.search(r'(?m)^static [^\n]*\b' + re.escape(name) + r'\s*\([^;]*?\)\s*\{', source)
    if not match:
        raise ValueError('C function definition not found: ' + name)
    start = match.start()
    opening = match.end() - 1
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


# A minimal userspace shim supplies kernel allocation and unbalanced tree
# primitives. The reservation/search/removal routines below are extracted from
# the real patched driver. This verifies interval logic, not kernel locking or
# GPU/SMMU execution.
ALLOCATOR_SHIM = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdio.h>
#include <errno.h>
typedef uint64_t u64;
struct rb_node { struct rb_node *rb_left, *rb_right, *parent; };
struct rb_root { struct rb_node *rb_node; };
struct kgsl_iommu_addr_entry { struct rb_node node; u64 base, size; };
struct kgsl_iommu { bool gpu_va48; };
struct kgsl_mmu { struct kgsl_iommu iommu; };
struct kgsl_pagetable {
    struct rb_root rbtree;
    struct kgsl_mmu *mmu;
    u64 va_hint, va_start, va_end, svm_start, svm_end;
    u64 compat_va_start, compat_va_end;
    int lock;
};
static struct kgsl_iommu *to_kgsl_iommu(struct kgsl_pagetable *pt) { return &pt->mmu->iommu; }
#define rb_entry(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define rb_entry_safe(p, t, m) ((p) ? rb_entry(p, t, m) : NULL)
#define ALIGN(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define upper_32_bits(x) ((u64)(x) >> 32)
#define IS_ERR_VALUE(x) ((u64)(x) >= (u64)-4095)
#define WARN(x, ...) (x)
#define WARN_RATELIMIT(...) ((void)0)
#define GFP_ATOMIC 0
#define spin_lock(p) ((void)0)
#define spin_unlock(p) ((void)0)
static int addr_entry_cache;
#define kmem_cache_alloc(cache, flags) malloc(sizeof(struct kgsl_iommu_addr_entry))
#define kmem_cache_free(cache, p) free(p)
static struct rb_node *rb_first(const struct rb_root *r) {
    struct rb_node *n = r->rb_node;
    if (n) while (n->rb_left) n = n->rb_left;
    return n;
}
static struct rb_node *rb_last(const struct rb_root *r) {
    struct rb_node *n = r->rb_node;
    if (n) while (n->rb_right) n = n->rb_right;
    return n;
}
static struct rb_node *rb_next(struct rb_node *n) {
    if (n->rb_right) { n = n->rb_right; while (n->rb_left) n = n->rb_left; return n; }
    while (n->parent && n == n->parent->rb_right) n = n->parent;
    return n->parent;
}
static struct rb_node *rb_prev(struct rb_node *n) {
    if (n->rb_left) { n = n->rb_left; while (n->rb_right) n = n->rb_right; return n; }
    while (n->parent && n == n->parent->rb_left) n = n->parent;
    return n->parent;
}
static void rb_link_node(struct rb_node *n, struct rb_node *p, struct rb_node **slot) {
    n->parent = p; n->rb_left = n->rb_right = NULL; *slot = n;
}
static void rb_insert_color(struct rb_node *n, struct rb_root *r) { (void)n; (void)r; }
static void replace_node(struct rb_root *r, struct rb_node *old, struct rb_node *n) {
    if (!old->parent) r->rb_node = n;
    else if (old == old->parent->rb_left) old->parent->rb_left = n;
    else old->parent->rb_right = n;
    if (n) n->parent = old->parent;
}
static void rb_erase(struct rb_node *n, struct rb_root *r) {
    if (!n->rb_left) replace_node(r, n, n->rb_right);
    else if (!n->rb_right) replace_node(r, n, n->rb_left);
    else {
        struct rb_node *s = n->rb_right;
        while (s->rb_left) s = s->rb_left;
        if (s->parent != n) {
            replace_node(r, s, s->rb_right);
            s->rb_right = n->rb_right; s->rb_right->parent = s;
        }
        replace_node(r, n, s);
        s->rb_left = n->rb_left; s->rb_left->parent = s;
    }
}
'''


ALLOCATOR_CASES = r'''
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "line %d: %s\n", __LINE__, #x); return 1; } } while (0)
#define PAGE 4096ULL
#define LOW (1ULL << 47)
#define HIGH (1ULL << 48)
static void init_pt(struct kgsl_pagetable *p, struct kgsl_mmu *m, bool enabled) {
    struct kgsl_pagetable empty = {0}; *p = empty;
    m->iommu.gpu_va48 = enabled; p->mmu = m;
    p->va_start = LOW; p->va_end = HIGH; p->va_hint = LOW;
    p->svm_start = 1ULL << 36; p->svm_end = HIGH;
}
static u64 reserve_gpu(struct kgsl_pagetable *p, u64 size, u64 align) {
    u64 addr = _get_unmapped_area(p, p->va_start, p->va_end, size, align);
    if (!IS_ERR_VALUE(addr) && _insert_gpuaddr(p, addr, size)) return (u64)-ENOMEM;
    return addr;
}
static void clear_pt(struct kgsl_pagetable *p) {
    while (p->rbtree.rb_node) {
        struct kgsl_iommu_addr_entry *e = rb_entry(p->rbtree.rb_node, struct kgsl_iommu_addr_entry, node);
        _remove_gpuaddr(p, e->base);
    }
}
static int no_overlap(struct kgsl_pagetable *p) {
    struct rb_node *n; u64 end = 0;
    for (n = rb_first(&p->rbtree); n; n = rb_next(n)) {
        struct kgsl_iommu_addr_entry *e = rb_entry(n, struct kgsl_iommu_addr_entry, node);
        if (e->base < end) return 0;
        end = e->base + e->size;
    }
    return 1;
}
int main(void) {
    struct kgsl_pagetable p; struct kgsl_mmu m;
    u64 addr, top, blocks[64]; int i;
    /* SVM first, including an interval that straddles the GPU lower bound. */
    init_pt(&p, &m, true);
    CHECK(kgsl_iommu_set_svm_region(&p, LOW - PAGE, 3 * PAGE) == 0);
    CHECK(IS_ERR_VALUE(_get_unmapped_area_hint(&p, LOW, HIGH, PAGE, PAGE)));
    addr = reserve_gpu(&p, PAGE, PAGE);
    CHECK(addr == LOW + 2 * PAGE); CHECK(no_overlap(&p));
    CHECK(kgsl_iommu_set_svm_region(&p, addr, PAGE) == -ENOMEM);
    CHECK(_remove_gpuaddr(&p, addr) == 0);
    CHECK(kgsl_iommu_set_svm_region(&p, addr, PAGE) == 0);
    clear_pt(&p);
    /* Non-SVM first: imports cannot replace it; release makes the address usable. */
    init_pt(&p, &m, true);
    addr = reserve_gpu(&p, 2 * PAGE, PAGE); CHECK(addr == LOW);
    CHECK(kgsl_iommu_set_svm_region(&p, LOW + PAGE, PAGE) == -ENOMEM);
    CHECK(kgsl_iommu_set_svm_region(&p, LOW + 2 * PAGE, PAGE) == 0);
    CHECK(_remove_gpuaddr(&p, addr) == 0);
    CHECK(kgsl_iommu_set_svm_region(&p, addr, 2 * PAGE) == 0);
    CHECK(no_overlap(&p)); clear_pt(&p);
    /* Final page is valid, exclusive end and wrapped or zero lengths are not. */
    init_pt(&p, &m, true);
    CHECK(kgsl_iommu_set_svm_region(&p, HIGH - PAGE, PAGE) == 0);
    CHECK(kgsl_iommu_set_svm_region(&p, HIGH, PAGE) == -ENOMEM);
    CHECK(kgsl_iommu_set_svm_region(&p, HIGH - PAGE, 2 * PAGE) == -ENOMEM);
    CHECK(kgsl_iommu_set_svm_region(&p, UINT64_MAX - PAGE + 1, 2 * PAGE) == -ENOMEM);
    CHECK(kgsl_iommu_set_svm_region(&p, LOW, 0) == -ENOMEM);
    top = kgsl_iommu_find_svm_region(&p, p.svm_start, p.svm_end, PAGE, PAGE);
    CHECK(top == HIGH - 2 * PAGE);
    CHECK(kgsl_iommu_set_svm_region(&p, top, PAGE) == 0);
    CHECK(no_overlap(&p)); clear_pt(&p);
    /* Mixed aligned reservations, reusable holes, and the real sorted-tree walk. */
    init_pt(&p, &m, true);
    for (i = 0; i < 64; i++) {
        blocks[i] = LOW + (u64)i * 4 * PAGE;
        CHECK(kgsl_iommu_set_svm_region(&p, blocks[i], PAGE) == 0);
        addr = reserve_gpu(&p, PAGE, 2 * PAGE);
        CHECK(!IS_ERR_VALUE(addr)); CHECK(!(addr & (2 * PAGE - 1)));
        CHECK(no_overlap(&p));
    }
    for (i = 0; i < 64; i += 2) CHECK(_remove_gpuaddr(&p, blocks[i]) == 0);
    CHECK(reserve_gpu(&p, PAGE, PAGE) == LOW); CHECK(no_overlap(&p));
    clear_pt(&p);
    /* The original fast path stays active only with the feature disabled. */
    init_pt(&p, &m, false);
    CHECK(_get_unmapped_area_hint(&p, LOW, HIGH, PAGE, PAGE) == LOW);
    CHECK(p.va_hint == LOW + PAGE);
    puts("actual KGSL C allocator cases passed"); return 0;
}
'''


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class GpuOnlyVa48Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "graphics-kernel"
        shutil.copytree(SOURCE, self.root)
        self.initial = hashes(self.root)
        self.cpu_initial = (hashlib.sha256(CPU_PATCH.read_bytes()).hexdigest()
                            if CPU_PATCH else None)

    def tearDown(self):
        self.assertEqual(hashes(SOURCE), ORIGINAL_SOURCE_HASHES)
        if CPU_PATCH:
            self.assertEqual(hashlib.sha256(CPU_PATCH.read_bytes()).hexdigest(), self.cpu_initial)

    def patched(self):
        return gpu_patch.build_changes(self.root)

    def test_preflight_and_check_never_write_sources(self):
        originals, changed = self.patched()
        self.assertEqual(set(originals), set(gpu_patch.FILES))
        self.assertEqual(set(changed), set(gpu_patch.FILES))
        diff = Path(self.temp.name) / "gpu-only-va48.patch"
        self.assertEqual(gpu_patch.main([str(self.root.parent), "--check", "--patch", str(diff)]), 0)
        self.assertEqual(hashes(self.root), self.initial)
        self.assertIn("a/graphics-kernel/kgsl_iommu.c", diff.read_text())

    def test_apply_success_changes_exact_whitelist_and_refuses_reapply(self):
        originals, changed = self.patched()
        gpu_patch.apply_changes(self.root, originals, changed)
        actual = hashes(self.root)
        changed_names = {p for p in self.initial if actual[p] != self.initial[p]}
        self.assertEqual(changed_names, set(gpu_patch.FILES))
        for name in gpu_patch.FILES:
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), changed[name])
        applied_hashes = hashes(self.root)
        with self.assertRaisesRegex(gpu_patch.PatchError, "already present"):
            self.patched()
        self.assertEqual(hashes(self.root), applied_hashes)

    def test_missing_last_anchor_refuses_all_writes(self):
        c = self.root / "kgsl_iommu.c"
        c.write_text(c.read_text().replace("\tkgsl_iommu_check_config(mmu, node);",
                                         "\t/* removed known anchor */"), newline="\n")
        before = hashes(self.root)
        self.assertEqual(gpu_patch.main([str(self.root)]), 1)
        self.assertEqual(hashes(self.root), before)

    def test_unknown_source_fingerprint_refuses_all_writes(self):
        k = self.root / "Kconfig"
        k.write_text(k.read_text() + "\n# unaudited source drift\n", newline="\n")
        before = hashes(self.root)
        with self.assertRaisesRegex(gpu_patch.PatchError, "unknown source fingerprint"):
            self.patched()
        self.assertEqual(hashes(self.root), before)

    def test_complete_baseline_identified(self):
        originals, _ = self.patched()
        branch = gpu_patch.identify_baseline(originals)
        self.assertIn(branch, gpu_patch.SOURCE_BASELINES)
        actual = {name: hashlib.sha256(originals[name].encode()).hexdigest()
                  for name in gpu_patch.FILES}
        self.assertEqual(actual, gpu_patch.SOURCE_BASELINES[branch])
        for expected in gpu_patch.SOURCE_BASELINES.values():
            self.assertEqual(set(expected), set(gpu_patch.FILES))

    def test_automatic_source_pins_preserve_audited_gpu_source(self):
        manifest = json.loads((HERE / "source-refs.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), {"6.1.57", "6.1.75", "6.1.118", "6.1.141"})
        allowed_dependency_paths = {
            "vendor/qcom/opensource/mm-drivers",
            "vendor/qcom/opensource/synx-kernel",
        }
        for version, entry in manifest.items():
            self.assertEqual(entry["vendor_base_version"], version)
            self.assertEqual(entry["validation"], "experimental-unverified")
            self.assertIn(entry["source_baseline"], gpu_patch.SOURCE_BASELINES)
            for source in [entry["kernel"], entry["modules"], *entry.get("overlays", [])]:
                self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
                self.assertRegex(source["repository"], r"^OnePlusOSS/[A-Za-z0-9_.-]+$")
            for overlay in entry.get("overlays", []):
                self.assertTrue(overlay["paths"])
                self.assertLessEqual(set(overlay["paths"]), allowed_dependency_paths)
                evidence = overlay["api_compatibility_evidence"]
                self.assertEqual(evidence["target_vendor_header_sha256"],
                                 evidence["overlay_reference_header_sha256"])
                self.assertEqual(entry["api_header_sha256"][evidence["target_vendor_header"]],
                                 evidence["target_vendor_header_sha256"])

    def test_mixed_known_baselines_refuse_all_writes(self):
        originals, _ = self.patched()
        mixed = None
        # Use actual downloaded files. V15/B16 differ in only one file, so
        # swapping that file is another complete valid tree, not a mixed one.
        for source in SOURCES:
            for name in gpu_patch.FILES:
                alternate = (source / name).read_text(encoding="utf-8")
                if alternate == originals[name]:
                    continue
                candidate = dict(originals, **{name: alternate})
                try:
                    gpu_patch.identify_baseline(candidate)
                except gpu_patch.PatchError:
                    mixed = name, alternate
                    break
            if mixed:
                break
        if mixed is None:
            self.skipTest("pass U14 and V15/B16 sources to check real mixed revisions")
        name, alternate = mixed
        (self.root / name).write_text(alternate, encoding="utf-8", newline="\n")
        before = hashes(self.root)
        with self.assertRaisesRegex(gpu_patch.PatchError, "mixed source revisions"):
            self.patched()
        self.assertEqual(hashes(self.root), before)

    def test_disabled_and_failed_capability_keep_legacy_ranges(self):
        _, changed = self.patched()
        c = changed["kgsl_iommu.c"]
        probe = c.split("static void kgsl_iommu_probe_gpu_va48", 1)[1].split("int kgsl_iommu_bind", 1)[0]
        self.assertIn("iommu->num_clks <= 0", probe)
        self.assertIn("IS_ERR_OR_NULL(iommu->cx_gdsc)", probe)
        self.assertLess(probe.index("IS_ERR_OR_NULL(iommu->cx_gdsc)"), probe.index("id2 = readl_relaxed"))
        self.assertLess(probe.index("iommu->gpu_va48 = false;"), probe.index("!IS_ENABLED(CONFIG_QCOM_KGSL_GPU_VA48)"))
        self.assertIn("default n", changed["Kconfig"])
        self.assertIn("depends on ARM64 && 64BIT && QCOM_KGSL", changed["Kconfig"])
        self.assertIn("ccflags-$(CONFIG_QCOM_KGSL_GPU_VA48)", changed["Kbuild"])
        for prefix in ("iommu_pt", "pt"):
            self.assertIn(prefix + "->base.va_start = iommu->gpu_va48 ?\n\t\t\tKGSL_IOMMU_VA_BASE48 : KGSL_IOMMU_VA_BASE64;", c)
            self.assertIn(prefix + "->base.va_end = iommu->gpu_va48 ?\n\t\t\tKGSL_IOMMU_VA_END48 : KGSL_IOMMU_VA_END64;", c)
        # Exact supported UBS values reject a floating/all-ones register read.
        for ubs in range(16):
            for page4k in (False, True):
                enabled = ubs == 5 and page4k
                self.assertEqual(enabled, (ubs, page4k) == (5, True))
        self.assertIn("ubs == 5 && (id2 & IDR2_PTFS_4K)", probe)

    def test_power_acquisition_checked_before_read_and_released(self):
        _, changed = self.patched()
        c = changed["kgsl_iommu.c"]
        probe = c.split("static void kgsl_iommu_probe_gpu_va48", 1)[1].split("int kgsl_iommu_bind", 1)[0]
        self.assertLess(probe.index("ret = regulator_enable"), probe.index("ret = clk_bulk_prepare_enable"))
        self.assertLess(probe.index("ret = clk_bulk_prepare_enable"), probe.index("id2 = readl_relaxed"))
        self.assertLess(probe.index("goto disable_regulator;"), probe.index("id2 = readl_relaxed"))
        self.assertLess(probe.index("id2 = readl_relaxed"), probe.index("clk_bulk_disable_unprepare"))
        self.assertLess(probe.index("clk_bulk_disable_unprepare"), probe.index("ret = regulator_disable"))
        bind = c.split("int kgsl_iommu_bind", 1)[1]
        self.assertLess(bind.index("kgsl_iommu_check_config"), bind.index("kgsl_iommu_probe_gpu_va48"))
        self.assertLess(bind.index("kgsl_iommu_probe_gpu_va48"), bind.index("iommu_probe_user_context"))

    def test_ranges_boundaries_cpu39_cpu48_and_unchanged_special_ranges(self):
        _, changed = self.patched()
        c = changed["kgsl_iommu.c"]
        h = changed["kgsl_iommu.h"]
        svm_start, svm_end, va_start, va_end = 0x1000000000, 1 << 48, 1 << 47, 1 << 48
        self.assertLess(svm_start, svm_end)
        self.assertEqual(svm_end, va_end)
        self.assertLess(va_start, va_end)
        self.assertEqual(va_end - 1, 0xffffffffffff)
        for cpu_bits, expected_svm_end in ((39, 1 << 39), (48, 1 << 48)):
            self.assertEqual(min(svm_end, 1 << cpu_bits), expected_svm_end)
            self.assertLessEqual(min(svm_end, 1 << cpu_bits), va_end)
        self.assertIn("min_t(u64, KGSL_IOMMU_SVM_END48, TASK_SIZE)", c)
        self.assertIn("#define KGSL_IOMMU_VA_END48\t\t(1ULL << 48)", h)
        self.assertIn("#define KGSL_IOMMU_SVM_END48\t\t(1ULL << 48)", h)
        for line in (SOURCE / "kgsl_iommu.h").read_text().splitlines():
            if "#define KGSL_IOMMU_" in line and any(s in line for s in ("SECURE", "GLOBAL", "SVM_END32", "SVM_BASE32")):
                self.assertIn(line, h)
        for line in (SOURCE / "kgsl_iommu.c").read_text().splitlines():
            if any(s in line for s in ("compat_va_", "global_base =", "svm_end = KGSL_IOMMU_SVM_END32")):
                self.assertIn(line, c)

    def test_ttbr1_cfg_immutable_and_returned_ias_checked(self):
        _, changed = self.patched()
        c = changed["kgsl_iommu.c"]
        alloc = c.split("static int kgsl_iopgtbl_alloc", 1)[1].split("static struct kgsl_pagetable *kgsl_iommu_default", 1)[0]
        self.assertLess(alloc.index("pt->info.cfg = *cfg;"), alloc.index("pt->info.cfg.ias = 48;"))
        self.assertNotIn("cfg->ias =", alloc)
        self.assertIn("~IO_PGTABLE_QUIRK_ARM_TTBR1", alloc)
        self.assertIn("pt->info.cfg.ias != 48", alloc)
        self.assertIn("qcom_free_io_pgtable_ops(pt->pgtbl_ops);", alloc)
        self.assertIn("return -ERANGE;", alloc)
        self.assertIn("pt->base.name == KGSL_MMU_GLOBAL_PT", alloc)
        self.assertIn("pt->info.cfg.ias, cfg->ias", alloc)

    def test_shared_range_reservations_use_actual_c_allocator(self):
        _, changed = self.patched()
        c = changed['kgsl_iommu.c']
        hint = c_function(c, '_get_unmapped_area_hint')
        self.assertIn('to_kgsl_iommu(pagetable)->gpu_va48', hint)
        self.assertLess(hint.index('gpu_va48'), hint.index('hint = ALIGN'))
        for name in ('kgsl_iommu_set_svm_region', 'get_gpuaddr'):
            routine = c_function(c, name)
            self.assertLess(routine.index('spin_lock'), routine.index('_insert_gpuaddr'))
            self.assertLess(routine.index('_insert_gpuaddr'), routine.rindex('spin_unlock'))
        names = ('_find_gpuaddr', '_remove_gpuaddr', '_insert_gpuaddr',
                 '_get_unmapped_area_hint', '_get_unmapped_area',
                 '_get_unmapped_area_topdown', 'kgsl_iommu_find_svm_region',
                 'iommu_addr_in_svm_ranges', 'kgsl_iommu_set_svm_region')
        source = ALLOCATOR_SHIM + '\n'.join(c_function(c, n) for n in names) + ALLOCATOR_CASES
        c_path = Path(self.temp.name) / 'allocator.c'
        exe = Path(self.temp.name) / ('allocator.exe' if os.name == 'nt' else 'allocator')
        c_path.write_text(source, encoding='utf-8')
        compiler = CC or os.environ.get('CC') or shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
        self.assertTrue(compiler, 'A C compiler is required to verify actual allocator routines; use --cc')
        compiled = subprocess.run([compiler, '-std=c11', '-O0', str(c_path), '-o', str(exe)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        checked = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn('actual KGSL C allocator cases passed', checked.stdout)

    def test_gpu_capacity_property_changes_only_after_capability_probe(self):
        originals, changed = self.patched()
        prop = c_function(changed['kgsl.c'], 'kgsl_get_gpu_va64_size')
        self.assertIn('u64 va_size = KGSL_IOMMU_VA_END64 - KGSL_IOMMU_VA_BASE64;', prop)
        self.assertIn('if (dev_priv->device->mmu.iommu.gpu_va48)', prop)
        self.assertIn('va_size = KGSL_IOMMU_VA_END48 - KGSL_IOMMU_VA_BASE48;', prop)
        self.assertEqual(changed['kgsl.c'].replace(prop, c_function(originals['kgsl.c'], 'kgsl_get_gpu_va64_size')), originals['kgsl.c'])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, action="append", type=Path,
                        help="audited GPU tree; repeat to test multiple baselines")
    parser.add_argument("--cpu-patch", type=Path)
    parser.add_argument('--cc', help='C compiler for the extracted allocator regression')
    args, remaining = parser.parse_known_args()
    SOURCES = [source.resolve() for source in args.source]
    CPU_PATCH = args.cpu_patch.resolve() if args.cpu_patch else None
    CC = args.cc
    successful = True
    for SOURCE in SOURCES:
        ORIGINAL_SOURCE_HASHES = hashes(SOURCE)
        print(f"\nTesting audited source: {SOURCE}", flush=True)
        result = unittest.main(argv=[sys.argv[0], "-v", *remaining], exit=False).result
        successful = result.wasSuccessful() and successful
    raise SystemExit(0 if successful else 1)
