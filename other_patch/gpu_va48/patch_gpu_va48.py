#!/usr/bin/env python3
"""Experimental KGSL GPU-only VA48 patcher for audited SM8650 vendor sources.

Pass the graphics-kernel directory (or its parent). --check validates every
original anchor without writing. --patch writes a unified diff after successful
preflight. No ARM64 CPU configuration, CPU page table or device tree is edited.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
from pathlib import Path
import sys
import tempfile


FILES = ("Kconfig", "Kbuild", "kgsl_iommu.h", "kgsl_iommu.c")
MARKER = "QCOM_KGSL_GPU_VA48"
SOURCE_BASELINES = {
    "oneplus/sm8650_u_14.0.0_oneplus12": {
        "Kconfig": "88a36ffde3ceb0e20dbe0bcc6eefc4b05f7a4e3fc596d586b1e017a9c2727987",
        "Kbuild": "7110a09e4d4c1aaa6cb979b93e35108c47bc85b2b764c5565438b794d32be6c0",
        "kgsl_iommu.h": "ea0092046d65ec41eefa9cfa4e485303e78971e16d28fdb1877ff2495b73bac9",
        "kgsl_iommu.c": "5f2e2f1e6822e1e37727b6e7a6ed14821787e5ea5f1049113a6200c674b0fc0f",
    },
    "oneplus/sm8650_v_15.0.0_oneplus12": {
        "Kconfig": "4f5161d66c500cf1be21a9d537c52288a7d358203cd6e8ed8829fbad5a6fa1d3",
        "Kbuild": "ca56d21e6cafe11cc7f17ed2062257dd1d198726da9c5e6d0905177cee4b8a22",
        "kgsl_iommu.h": "bbd552051b4212832ee3480d9bb6d4e1fb83c8300e314313a9a707de1f1af1d9",
        "kgsl_iommu.c": "d4b9adc87334e9ec37611ef82f53015d1601a7f737139221caa9648984bb73a1",
    },
    "oneplus/sm8650_b_16.0.0_oneplus12": {
        "Kconfig": "4f5161d66c500cf1be21a9d537c52288a7d358203cd6e8ed8829fbad5a6fa1d3",
        "Kbuild": "ca56d21e6cafe11cc7f17ed2062257dd1d198726da9c5e6d0905177cee4b8a22",
        "kgsl_iommu.h": "bbd552051b4212832ee3480d9bb6d4e1fb83c8300e314313a9a707de1f1af1d9",
        "kgsl_iommu.c": "158fa146b934d10ddbdfc34e678863b00b2a0afc9f8b56e00c0193c5a07fc033",
    },
}


class PatchError(Exception):
    """The source is not an untouched supported baseline."""


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one original anchor, found {count}")
    return text.replace(old, new, 1)


def identify_baseline(originals: dict[str, str]) -> str:
    """Require all four files from one audited tree; reject mixed revisions.

    The baseline identifies only GPU source. A kernel version (including
    6.1.141) is not a substitute for a matching vendor SDK and module ABI.
    """
    actual = {name: hashlib.sha256(originals[name].encode()).hexdigest()
              for name in FILES}
    for branch, expected in SOURCE_BASELINES.items():
        if actual == expected:
            return branch
    raise PatchError("unknown source fingerprint or mixed source revisions; "
                     "refusing all source writes")


def build_changes(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Preflight the entire four-file change before any file can be written."""
    originals = {}
    for name in FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise PatchError(f"not a regular source file: {path}")
        originals[name] = path.read_text(encoding="utf-8")
        if MARKER in originals[name]:
            raise PatchError(f"{name}: GPU VA48 marker already present; refusing reapply")
    changed = dict(originals)
    changed["Kconfig"] = replace_one(changed["Kconfig"],
        "config DEVFREQ_GOV_QCOM_ADRENO_TZ\n",
        """config QCOM_KGSL_GPU_VA48
	bool "Experimental 48-bit GPU virtual addresses on SM8650"
	depends on ARM64 && 64BIT && QCOM_KGSL
	default n
	help
	  Experimentally extend only non-secure GPU TTBR0 page tables on
	  Adreno Gen7.2.0 after checking the powered SMMU ID2 register.
	  CPU virtual addresses, TTBR1/global, secure and compat mappings
	  keep their existing layout. A matching rebuilt KGSL module and
	  device testing are required. If unsure, say N.

config DEVFREQ_GOV_QCOM_ADRENO_TZ
""", "Kconfig option")
    changed["Kbuild"] = replace_one(changed["Kbuild"],
        "KDIR := $(TOP)/kernel_platform/common\n",
        "KDIR := $(TOP)/kernel_platform/common\n\n"
        "# Also support CONFIG_QCOM_KGSL_GPU_VA48=y for an external module build.\n"
        "ccflags-$(CONFIG_QCOM_KGSL_GPU_VA48) += -DCONFIG_QCOM_KGSL_GPU_VA48=1\n",
        "Kbuild external opt-in")
    changed["kgsl_iommu.h"] = replace_one(changed["kgsl_iommu.h"],
        "#define KGSL_IOMMU_VA_END64\t\t0x8000000000ULL\n",
        "#define KGSL_IOMMU_VA_END64\t\t0x8000000000ULL\n\n"
        "/* GPU-only experimental TTBR0 ranges; all end addresses are exclusive. */\n"
        "#define KGSL_IOMMU_SVM_END48\t\t(1ULL << 47)\n"
        "#define KGSL_IOMMU_VA_BASE48\t\t(1ULL << 47)\n"
        "#define KGSL_IOMMU_VA_END48\t\t(1ULL << 48)\n",
        "GPU address constants")
    changed["kgsl_iommu.h"] = replace_one(changed["kgsl_iommu.h"],
        "\tstruct regulator *cx_gdsc;\n};\n",
        "\tstruct regulator *cx_gdsc;\n"
        "\t/** @gpu_va48: GPU TTBR0-only opt-in after powered capability checks */\n"
        "\tbool gpu_va48;\n};\n", "GPU capability state")
    c = changed["kgsl_iommu.c"]
    c = replace_one(c, "#include <soc/qcom/secure_buffer.h>\n",
        "#include <soc/qcom/secure_buffer.h>\n#include <asm/processor.h>\n",
        "TASK_SIZE include")
    c = replace_one(c, "#define IDR1_PAGESIZE BIT(31)\n",
        "#define IDR1_PAGESIZE BIT(31)\n\n"
        "#define KGSL_IOMMU_IDR2_OFFSET 0x28\n"
        "#define IDR2_UBS GENMASK(11, 8)\n"
        "#define IDR2_PTFS_4K BIT(12)\n", "SMMU ID2 definitions")
    c = replace_one(c,
        "\tconst struct io_pgtable_cfg *cfg = NULL;\n\tvoid *domain = (void *)adreno_smmu->cookie;\n",
        "\tconst struct io_pgtable_cfg *cfg = NULL;\n"
        "\tstruct kgsl_iommu *iommu = KGSL_IOMMU(ctx->kgsldev);\n"
        "\tvoid *domain = (void *)adreno_smmu->cookie;\n",
        "allocation capability state")
    c = replace_one(c,
        "\tpt->info.cfg.quirks &= ~IO_PGTABLE_QUIRK_ARM_TTBR1;\n"
        "\tpt->info.cfg.tlb = &kgsl_iopgtbl_tlb_ops;\n",
        "\tpt->info.cfg.quirks &= ~IO_PGTABLE_QUIRK_ARM_TTBR1;\n"
        "\t/* Change only this copied TTBR0 cfg; the SMMU TTBR1 cfg is untouched. */\n"
        "\tif (iommu->gpu_va48)\n"
        "\t\tpt->info.cfg.ias = 48;\n"
        "\tpt->info.cfg.tlb = &kgsl_iopgtbl_tlb_ops;\n",
        "TTBR0 local IAS override")
    c = replace_one(c,
        "\tpt->ttbr0 = pt->info.cfg.arm_lpae_s1_cfg.ttbr;\n\n\treturn 0;\n",
        """	if (iommu->gpu_va48 && pt->info.cfg.ias != 48) {
		dev_err(&ctx->kgsldev->pdev->dev,
			"GPU VA48: TTBR0 allocator returned IAS=%u; refusing inconsistent ranges\\n",
			pt->info.cfg.ias);
		qcom_free_io_pgtable_ops(pt->pgtbl_ops);
		pt->pgtbl_ops = NULL;
		return -ERANGE;
	}

	if (iommu->gpu_va48 && pt->base.name == KGSL_MMU_GLOBAL_PT)
		dev_info(&ctx->kgsldev->pdev->dev,
			"GPU VA48: TTBR0 IAS=%u, inherited TTBR1 IAS=%u (unchanged)\\n",
			pt->info.cfg.ias, cfg->ias);

	pt->ttbr0 = pt->info.cfg.arm_lpae_s1_cfg.ttbr;

	return 0;
""".lstrip("\n"), "allocated IAS verification")
    for prefix in ("iommu_pt", "pt"):
        old = (f"\t\t{prefix}->base.va_start = KGSL_IOMMU_VA_BASE64;\n"
               f"\t\t{prefix}->base.va_end = KGSL_IOMMU_VA_END64;\n")
        new = (f"\t\t{prefix}->base.va_start = iommu->gpu_va48 ?\n"
               f"\t\t\tKGSL_IOMMU_VA_BASE48 : KGSL_IOMMU_VA_BASE64;\n"
               f"\t\t{prefix}->base.va_end = iommu->gpu_va48 ?\n"
               f"\t\t\tKGSL_IOMMU_VA_END48 : KGSL_IOMMU_VA_END64;\n")
        c = replace_one(c, old, new, prefix + " non-SVM range")
    c = replace_one(c,
        "\t\t\tpt->base.svm_start = KGSL_IOMMU_SVM_BASE64;\n"
        "\t\t\tpt->base.svm_end = KGSL_IOMMU_SVM_END64;\n",
        "\t\t\tpt->base.svm_start = KGSL_IOMMU_SVM_BASE64;\n"
        "\t\t\t/* SVM must fit the existing CPU VA; do not change CPU layout. */\n"
        "\t\t\tpt->base.svm_end = iommu->gpu_va48 ?\n"
        "\t\t\t\tmin_t(u64, KGSL_IOMMU_SVM_END48, TASK_SIZE) :\n"
        "\t\t\t\tKGSL_IOMMU_SVM_END64;\n", "CPU-limited SVM range")
    c = replace_one(c,
        '\t\t\t"64-bit SVM and non-SVM ranges should not overlap");\n',
        '\t\t\t"64-bit SVM and non-SVM ranges should not overlap");\n'
        "\tBUILD_BUG_ON_MSG(KGSL_IOMMU_SVM_END48 > KGSL_IOMMU_VA_BASE48 ||\n"
        "\t\tKGSL_IOMMU_VA_BASE48 >= KGSL_IOMMU_VA_END48 ||\n"
        "\t\tKGSL_IOMMU_SVM_BASE64 >= KGSL_IOMMU_SVM_END48,\n"
        '\t\t"GPU VA48 ranges must be ordered and disjoint");\n',
        "VA48 compile-time range check")
    c = replace_one(c,
        "int kgsl_iommu_bind(struct kgsl_device *device, struct platform_device *pdev)\n",
        """/* No CPU VA or TTBR1 changes: this only gates the KGSL TTBR0 experiment. */
static void kgsl_iommu_probe_gpu_va48(struct kgsl_device *device)
{
	struct kgsl_iommu *iommu = KGSL_IOMMU(device);
	struct kgsl_mmu *mmu = &device->mmu;
	u32 id2, ubs;
	int ret;

	iommu->gpu_va48 = false;
	if (!IS_ENABLED(CONFIG_QCOM_KGSL_GPU_VA48))
		return;

	if (PAGE_SIZE != SZ_4K ||
		!test_bit(KGSL_MMU_64BIT, &mmu->features) ||
		!test_bit(KGSL_MMU_IOPGTABLE, &mmu->features) ||
		!adreno_is_gen7_2_0(ADRENO_DEVICE(device))) {
		dev_warn(&device->pdev->dev,
			"GPU VA48: requires Gen7.2.0, 4K pages and 64-bit IOPGTABLE; keeping legacy mode\\n");
		return;
	}

	/* Never read an unpowered SMMU; require explicit usable CX and clocks. */
	if (!iommu->regbase || iommu->num_clks <= 0 ||
		IS_ERR_OR_NULL(iommu->cx_gdsc)) {
		dev_warn(&device->pdev->dev,
			"GPU VA48: SMMU registers/CX/clocks unavailable; keeping legacy mode\\n");
		return;
	}
	ret = regulator_enable(iommu->cx_gdsc);
	if (ret) {
		dev_warn(&device->pdev->dev,
			"GPU VA48: SMMU regulator enable failed: %d; keeping legacy mode\\n", ret);
		return;
	}

	ret = clk_bulk_prepare_enable(iommu->num_clks, iommu->clks);
	if (ret) {
		dev_warn(&device->pdev->dev,
			"GPU VA48: SMMU clock enable failed: %d; keeping legacy mode\\n", ret);
		goto disable_regulator;
	}

	id2 = readl_relaxed(iommu->regbase + KGSL_IOMMU_IDR2_OFFSET);
	clk_bulk_disable_unprepare(iommu->num_clks, iommu->clks);
	ubs = FIELD_GET(IDR2_UBS, id2);
	/* Accept only the audited 48-bit UBS encoding; reject all other values. */
	if (ubs == 5 && (id2 & IDR2_PTFS_4K)) {
		iommu->gpu_va48 = true;
		dev_info(&device->pdev->dev,
			"GPU VA48: powered SMMU ID2=0x%08x supports 48-bit TTBR0 (experimental)\\n", id2);
	} else {
		dev_warn(&device->pdev->dev,
			"GPU VA48: SMMU ID2=0x%08x lacks 48-bit/4K support; keeping legacy mode\\n", id2);
	}

disable_regulator:
	ret = regulator_disable(iommu->cx_gdsc);
	if (ret) {
		iommu->gpu_va48 = false;
		dev_warn(&device->pdev->dev,
			"GPU VA48: SMMU regulator disable failed: %d; keeping legacy mode\\n", ret);
	}
}

int kgsl_iommu_bind(struct kgsl_device *device, struct platform_device *pdev)
""", "powered GPU capability probe")
    c = replace_one(c,
        "\tkgsl_iommu_check_config(mmu, node);\n\n\t/* Probe the default pagetable */\n",
        "\tkgsl_iommu_check_config(mmu, node);\n"
        "\tkgsl_iommu_probe_gpu_va48(device);\n\n"
        "\t/* Probe the default pagetable */\n", "probe before first pagetable")
    changed["kgsl_iommu.c"] = c
    identify_baseline(originals)
    return originals, changed


def make_patch(originals: dict[str, str], changed: dict[str, str]) -> str:
    return "".join("".join(difflib.unified_diff(
        originals[name].splitlines(keepends=True), changed[name].splitlines(keepends=True),
        fromfile="a/graphics-kernel/" + name, tofile="b/graphics-kernel/" + name))
        for name in FILES)


def apply_changes(root: Path, originals: dict[str, str], changed: dict[str, str]) -> None:
    """Stage all replacements, then replace; roll back if an OS write fails."""
    staged = {}
    replaced = []
    try:
        for name in FILES:
            if (root / name).read_text(encoding="utf-8") != originals[name]:
                raise PatchError(f"{name}: source changed after preflight")
        for name in FILES:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    dir=root, prefix=".gpu-va48-", delete=False) as out:
                out.write(changed[name])
                staged[name] = Path(out.name)
            os.chmod(staged[name], (root / name).stat().st_mode)
        for name in FILES:
            os.replace(staged[name], root / name)
            replaced.append(name)
    except OSError:
        for name in replaced:
            (root / name).write_text(originals[name], encoding="utf-8", newline="\n")
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tree_root", type=Path)
    parser.add_argument("--check", action="store_true", help="preflight only; no source writes")
    parser.add_argument("--patch", type=Path, help="also save the reviewed unified diff")
    args = parser.parse_args(argv)
    root = args.tree_root.resolve()
    if not (root / "kgsl_iommu.c").is_file() and (root / "graphics-kernel").is_dir():
        root /= "graphics-kernel"
    try:
        if args.patch and args.patch.resolve().is_relative_to(root):
            raise PatchError("the unified diff must be saved outside the source tree")
        originals, changed = build_changes(root)
        baseline = identify_baseline(originals)
        if args.patch:
            args.patch.parent.mkdir(parents=True, exist_ok=True)
            args.patch.write_text(make_patch(originals, changed), encoding="utf-8", newline="\n")
        if not args.check:
            apply_changes(root, originals, changed)
        print("GPU-only VA48 preflight passed" if args.check else "Applied GPU-only VA48 patch")
        print(f"Audited GPU source baseline: {baseline}")
        for name in FILES:
            sha = hashlib.sha256(changed[name].encode()).hexdigest()
            print(f"{name}: patched sha256={sha}")
        print("CPU/ARM64 sources, device tree and existing CPU VA48 patch are untouched.")
        return 0
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"Refused GPU VA48 patch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
