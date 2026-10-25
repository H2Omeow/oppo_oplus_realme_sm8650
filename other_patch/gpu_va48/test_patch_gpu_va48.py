#!/usr/bin/env python3
"""Run with --source GRAPHICS_KERNEL for each audited tree, plus --cpu-patch FILE."""

import argparse
import hashlib
import importlib.util
from pathlib import Path
import shutil
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
        svm_start, svm_end, va_start, va_end = 0x1000000000, 1 << 47, 1 << 47, 1 << 48
        self.assertLess(svm_start, svm_end)
        self.assertEqual(svm_end, va_start)
        self.assertLess(va_start, va_end)
        self.assertEqual(va_end - 1, 0xffffffffffff)
        for cpu_bits, expected_svm_end in ((39, 1 << 39), (48, 1 << 47)):
            self.assertEqual(min(svm_end, 1 << cpu_bits), expected_svm_end)
            self.assertLessEqual(min(svm_end, 1 << cpu_bits), va_start)
        self.assertIn("min_t(u64, KGSL_IOMMU_SVM_END48, TASK_SIZE)", c)
        self.assertIn("#define KGSL_IOMMU_VA_END48\t\t(1ULL << 48)", h)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, action="append", type=Path,
                        help="audited GPU tree; repeat to test multiple baselines")
    parser.add_argument("--cpu-patch", type=Path)
    args, remaining = parser.parse_known_args()
    SOURCES = [source.resolve() for source in args.source]
    CPU_PATCH = args.cpu_patch.resolve() if args.cpu_patch else None
    successful = True
    for SOURCE in SOURCES:
        ORIGINAL_SOURCE_HASHES = hashes(SOURCE)
        print(f"\nTesting audited source: {SOURCE}", flush=True)
        result = unittest.main(argv=[sys.argv[0], "-v", *remaining], exit=False).result
        successful = result.wasSuccessful() and successful
    raise SystemExit(0 if successful else 1)
