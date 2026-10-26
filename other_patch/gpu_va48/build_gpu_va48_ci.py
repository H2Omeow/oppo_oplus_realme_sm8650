#!/usr/bin/env python3
"""Prepare public vendor sources and build GPU VA48 without a user-supplied SDK.

Only private vendor sources/output are changed. The already-built common kernel
is a read-only mixed-GKI input. Source revisions come from source-refs.json.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


strict = load_module("gpu_va48_strict_ci", HERE / "build_gpu_va48.py")
patcher = load_module("gpu_va48_patch_ci", HERE / "patch_gpu_va48.py")


def fixed_source(spec):
    if not isinstance(spec, dict) or spec.get("repository") not in (
        "OnePlusOSS/android_kernel_oneplus_sm8650",
        "OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650",
    ) or not re.fullmatch(r"[0-9a-f]{40}", spec.get("commit", "")):
        raise strict.BuildError("source map must specify an allowed public repository and full fixed commit")
    return spec


def run_stage(label, command, output, evidence, env, runner):
    print(f"GPU CI: {label}", flush=True)
    log = output / f"{len(evidence['stages']) + 1:02d}-{label}.log"
    stage = {"name": label, "command": command, "log": log.name, "status": "running"}
    evidence["stages"].append(stage)
    with log.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(command) + "\n")
        stream.flush()
        result = runner(command, stdout=stream, stderr=subprocess.STDOUT, text=True, env=env, check=False)
    stage["exit_code"] = result.returncode
    text = log.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        print(f"GPU CI: {label} failed; last 80 log lines ({log.name}):", flush=True)
        print("\n".join(text.splitlines()[-80:]), flush=True)
        stage["status"] = "failed"
        raise strict.BuildError(f"{label} failed with exit code {result.returncode}; see {log.name}")
    if re.search(r"(?:warning:.*(?:undefined|missing.*(?:symvers|symbol))|undefined!)", text, re.I):
        stage["status"] = "failed"
        print(f"GPU CI: {label} contains unresolved/missing symbols; last 80 log lines ({log.name}):", flush=True)
        print("\n".join(text.splitlines()[-80:]), flush=True)
        raise strict.BuildError(f"{label} has unresolved/missing symbols; see {log.name}")
    stage["status"] = "success"


def fetch_source(spec, destination, output, evidence, env, runner, label):
    fixed_source(spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    commands = (
        ["git", "init", "--quiet", str(destination)],
        ["git", "-C", str(destination), "fetch", "--no-tags", "--depth=1",
         "https://github.com/" + spec["repository"] + ".git", spec["commit"]],
        ["git", "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
    )
    for index, command in enumerate(commands):
        run_stage(f"{label}-{index + 1}", command, output, evidence, env, runner)
    actual = runner(["git", "-C", str(destination), "rev-parse", "HEAD"], capture_output=True,
                    text=True, check=True, env=env).stdout.strip()
    if actual != spec["commit"]:
        raise strict.BuildError(f"{label}: downloaded Git commit does not match the fixed source map")


def kernel_version(source):
    text = (source / "Makefile").read_text(encoding="utf-8")
    parts = []
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        match = re.search(r"^" + key + r"\s*=\s*(\d+)\s*$", text, re.M)
        if not match:
            raise strict.BuildError(f"vendor Makefile lacks {key}")
        parts.append(match[1])
    return ".".join(parts)


def require_file(path):
    if not path.is_file() or not path.stat().st_size:
        raise strict.BuildError(f"required public-source build input is absent: {path}")
    return path


def common_snapshot(common, source, cpu_patch):
    files = [common / name for name in (*strict.COMMON_FILES, "include/generated/autoconf.h", "include/generated/utsrelease.h")]
    files += [source / name for name in strict.CPU_HEADERS]
    if cpu_patch:
        files.append(cpu_patch)
    return {str(path): strict.sha256(require_file(path)) for path in files}


# The pinned vendor sources predate the CI Clang. Newer Clang promotes extra
# -Wformat/-Wenum/etc. diagnostics that the vendor tree builds with -Werror, so
# untouched vendor files (kernel/sched/walt/walt.c, arch/arm64/gunyah/reset.c)
# break the modules build. Demote only these new compiler diagnostics; -Werror
# stays on for everything else and no symbol/CRC/vermagic check is relaxed.
NEW_CLANG_WARNINGS = (
    "-Wno-error=format",
    "-Wno-error=enum-conversion",
    "-Wno-error=sometimes-uninitialized",
    "-Wno-error=unused-but-set-variable",
    "-Wno-error=missing-field-initializers",
    "-Wno-error=sign-compare",
    "-Wno-error=unused-function",
    "-Wno-error=unused-variable",
    "-Wno-error=strict-prototypes",
    "-Wno-error=deprecated-non-prototype",
    "-Wno-error=incompatible-function-pointer-types",
    "-Wno-error=constant-conversion",
    "-Wno-error=cast-function-type-mismatch",
    "-Wno-error=tautological-constant-out-of-range-compare",
    "-Wno-error=frame-larger-than",
    "-Wno-error=pointer-bool-conversion",
    "-Wno-error=address-of-packed-member",
)

# pineapple_GKI.config turns on OPlus scheduler modules that have nothing to do
# with the GPU address space. They call vendor hooks (register_trace_android_vh_*)
# that the GKI common tree we build against does not declare, so the native
# modules stage dies before modpost and never writes Module.symvers. Turning the
# subtree off is what keeps that symbol table reachable; kconfig cascades the
# "depends on OPLUS_FEATURE_SCHED_ASSIST" options (TASK_SCHED, TASK_LOAD,
# LOADBALANCE, PIPELINE, SCHED_SPREAD, TICK_GRAN) by itself, and CPU_JANKINFO
# (sched_info) is listed because it calls sched_assist exports directly.
# KGSL imports none of these; its only OPlus touchpoint is MM_OSVELTE, which is
# an independent memory-debug option and stays untouched.
KGSL_IRRELEVANT_MODULES = (
    "CONFIG_OPLUS_FEATURE_SCHED_ASSIST",
    "CONFIG_OPLUS_FEATURE_CPU_JANKINFO",
)


def make_args(kernel, vendor_out, common, release, jobs):
    return ["make", "-C", str(kernel), f"O={vendor_out}", f"KBUILD_MIXED_TREE={common}",
            f"KERNELRELEASE={release}", "ARCH=arm64", "LLVM=1", "LOCALVERSION=",
            "KBUILD_MODPOST_WARN=", "KBUILD_MODPOST_NOFINAL=",
            "KCFLAGS=" + " ".join(NEW_CLANG_WARNINGS), f"-j{jobs}"]


def validate_vendor_symbols(common, vendor_out):
    base = strict.symbols(require_file(common / "vmlinux.symvers"))
    vendor = strict.symbols(require_file(vendor_out / "Module.symvers"))
    for name, entry in vendor.items():
        if name in base and not strict.same_export(base[name], entry):
            raise strict.BuildError(f"vendor/GKI export CRC/type/namespace conflict: {name}")
    for name in ("module_layout", "qcom_alloc_io_pgtable_ops", "qcom_free_io_pgtable_ops"):
        if name not in vendor:
            raise strict.BuildError(f"vendor native modules build did not generate required export: {name}")


def validate_source_api(profile, kernel, evidence):
    expected = profile.get("api_header_sha256")
    if not isinstance(expected, dict) or not expected:
        raise strict.BuildError("source map must bind the vendor dependency API headers")
    actual = {}
    for name, digest in expected.items():
        relative = strict.relative_path(name)
        if str(relative) != "include/linux/soc/qcom/msm_hw_fence.h" or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise strict.BuildError("source map contains an unsupported dependency API header binding")
        actual[name] = strict.sha256(require_file(kernel.joinpath(*relative.parts)))
        if actual[name] != digest:
            raise strict.BuildError(f"vendor dependency API header differs from source map: {name}")
    evidence["verified_vendor_api_header_sha256"] = actual
    for overlay in profile.get("overlays", []):
        api = overlay.get("api_compatibility_evidence", {})
        name = api.get("target_vendor_header")
        if (name not in actual or api.get("target_vendor_header_sha256") != actual[name]
                or api.get("overlay_reference_header") != name
                or api.get("overlay_reference_header_sha256") != actual[name]):
            raise strict.BuildError("dependency overlay lacks matching pinned vendor API evidence")
        fixed_source(api.get("overlay_reference_kernel"))


def validate_prepared_config(common, vendor_out, release):
    current = strict.read_config(require_file(common / ".config"))
    vendor = strict.read_config(require_file(vendor_out / ".config"))
    for key in ("CONFIG_ARM64", "CONFIG_64BIT", "CONFIG_ARM64_4K_PAGES", "CONFIG_MODULES", "CONFIG_MODVERSIONS"):
        if current.get(key) != "y":
            raise strict.BuildError(f"common kernel requires {key}=y")
    if vendor.get("CONFIG_ARM_SMMU") not in ("y", "m") or vendor.get("CONFIG_ARCH_PINEAPPLE") != "y":
        raise strict.BuildError("vendor configuration requires ARM_SMMU and ARCH_PINEAPPLE")
    critical = strict.CPU_KEYS | {k for k in current.keys() | vendor.keys()
        if k.startswith(("CONFIG_ARM64_VA_BITS_", "CONFIG_ARM64_PA_BITS_", "CONFIG_KASAN"))}
    for key in critical:
        if current.get(key, "n") != vendor.get(key, "n"):
            raise strict.BuildError(f"prepared vendor CPU configuration mismatch: {key}")
    common_auto = strict.read_autoconf(require_file(common / "include/generated/autoconf.h"))
    vendor_auto = strict.read_autoconf(require_file(vendor_out / "include/generated/autoconf.h"))
    for key in critical:
        if common_auto.get(key, "n") != current.get(key, "n"):
            raise strict.BuildError(f"stale common generated autoconf.h: {key}")
        if vendor_auto.get(key, "n") != vendor.get(key, "n"):
            raise strict.BuildError(f"stale vendor generated autoconf.h: {key}")
    if strict.read_release(require_file(vendor_out / "include/generated/utsrelease.h")) != release:
        raise strict.BuildError("prepared vendor utsrelease does not match the current common kernel")


def export_dependency_modules(output, vendor_out, synx, fence, gpu, release, expected_vermagic, available, runner, env):
    roots = {"vendor-kernel": vendor_out, "synx": synx, "hw-fence": fence, "kgsl": gpu}
    candidates = {}
    for label, root in roots.items():
        for path in root.rglob("*.ko"):
            name = path.stem.replace("-", "_")
            if name in candidates and candidates[name][1] != path:
                raise strict.BuildError(f"ambiguous duplicate module output: {name}")
            candidates[name] = (label, path, root)
    required = {"msm_kgsl", "arm_smmu", "qcom_iommu_util", "synx_driver", "ipclite", "msm_hw_fence"}
    if not required <= candidates.keys():
        raise strict.BuildError("build did not produce required GPU dependency modules: " + ", ".join(sorted(required - candidates.keys())))
    inventory = {}
    pending = list(sorted(required))
    while pending:
        name = pending.pop()
        if name in inventory:
            continue
        if name not in candidates:
            raise strict.BuildError(f"built module runtime dependency is missing: {name}")
        label, path, root = candidates[name]
        binary = path.read_bytes()
        if len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", binary, 16) != (1, 183):
            raise strict.BuildError(f"dependency is not an AArch64 module: {name}")
        magic = runner(["modinfo", "-F", "vermagic", str(path)], capture_output=True, text=True, check=True, env=env).stdout.strip()
        if not magic.split() or magic.split()[0] != release or "modversions" not in magic.split() or "aarch64" not in magic.split():
            raise strict.BuildError(f"dependency module release/modversions mismatch: {name}")
        if magic.split() != expected_vermagic.split():
            raise strict.BuildError(f"dependency module vermagic differs from validated KGSL: {name}")
        imported = {}
        for line in runner(["modprobe", "--dump-modversions", str(path)], capture_output=True, text=True, check=True, env=env).stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not re.fullmatch(r"0x[0-9a-fA-F]{8,16}", fields[0]):
                raise strict.BuildError(f"cannot parse dependency imported CRCs: {name}")
            crc, symbol = int(fields[0], 16), fields[1]
            if symbol not in available or available[symbol][0] != crc:
                raise strict.BuildError(f"dependency import CRC mismatch: {name}/{symbol}")
            imported[symbol] = f"0x{crc:08x}"
        if "module_layout" not in imported:
            raise strict.BuildError(f"dependency has no checked module_layout CRC: {name}")
        for line in runner(["llvm-nm", "--undefined-only", "--format=posix", str(path)], capture_output=True, text=True, check=True, env=env).stdout.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[1] not in ("U", "w", "v") or (fields[1] == "U" and fields[0] not in imported):
                raise strict.BuildError(f"dependency strong import lacks checked CRC: {name}")
        deps_text = runner(["modinfo", "-F", "depends", str(path)], capture_output=True, text=True, check=True, env=env).stdout.strip()
        deps = []
        for dependency in filter(None, deps_text.split(",")):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", dependency):
                raise strict.BuildError(f"invalid dependency module name returned by modinfo: {name}")
            deps.append(dependency.replace("-", "_"))
        pending.extend(deps)
        relative = Path("dependencies") / label / path.relative_to(root)
        inventory[name] = {"path": str(relative).replace("\\", "/"), "source_path": str(path.relative_to(root)).replace("\\", "/"),
                           "build_group": label, "sha256": strict.sha256(path), "vermagic": magic,
                           "depends": deps, "imported_crcs": imported}
    for name, entry in inventory.items():
        destination = output / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidates[name][1], destination)
    (output / "module-manifest.json").write_text(json.dumps({"experimental": True, "hardware_tested": False,
        "automatic_install_or_flash": False, "modules": inventory}, indent=2) + "\n", encoding="utf-8")
    return inventory


def build(args, *, runner=subprocess.run, fetcher=fetch_source, refs=None):
    common, common_source, output = args.common_out.resolve(), args.common_source.resolve(), args.output.resolve()
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in (common, common_source)):
        raise strict.BuildError("GPU output must be separate from current common source/output")
    if output.exists() and any(output.iterdir()):
        raise strict.BuildError("GPU output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"status": "failed", "experimental": True, "hardware_tested": False,
                "kernel_version": args.kernel_version, "stages": [], "user_sdk_required": False}
    protected = {}
    cpu_patch = getattr(args, "cpu_patch", None)
    if cpu_patch is None:
        candidate = HERE.parent / "va48/patch_va48.py"
        cpu_patch = candidate if candidate.is_file() else None
    if cpu_patch:
        cpu_patch = cpu_patch.resolve()
    try:
        protected = common_snapshot(common, common_source, cpu_patch)
        evidence["common_protected_sha256_before"] = protected
        current_cfg = strict.read_config(common / ".config")
        if current_cfg.get("CONFIG_MODULE_SIG_FORCE") == "y":
            raise strict.BuildError("enforced module signing is unsupported without a trusted signing key")
        release = strict.read_release(common / "include/generated/utsrelease.h")
        if not (release == args.kernel_version or release.startswith(args.kernel_version + "-")):
            raise strict.BuildError("current common release does not match --kernel-version")
        if refs is None:
            refs = json.loads((HERE / "source-refs.json").read_text(encoding="utf-8"))
        profile = refs.get(args.kernel_version)
        if not isinstance(profile, dict):
            raise strict.BuildError("no verified fixed public-source mapping exists for this kernel version")
        evidence["source_profile"] = profile
        fixed_source(profile["kernel"])
        fixed_source(profile["modules"])
        env = dict(os.environ)
        for key in strict.INJECTED_ENV:
            env.pop(key, None)
        env["LC_ALL"] = "C"
        env["GIT_TERMINAL_PROMPT"] = "0"
        with tempfile.TemporaryDirectory(prefix="gpu-va48-public-") as temp:
            work = Path(temp)
            tree = work / "sources"
            fetcher(profile["modules"], tree, output, evidence, env, runner, "fetch-modules")
            kernel = tree / "kernel_platform/msm-kernel"
            fetcher(profile["kernel"], kernel, output, evidence, env, runner, "fetch-kernel")
            if kernel_version(kernel) != args.kernel_version:
                raise strict.BuildError("vendor source base version differs; refusing release-number spoofing")
            validate_source_api(profile, kernel, evidence)
            for index, overlay in enumerate(profile.get("overlays", [])):
                fixed_source(overlay)
                overlay_root = work / f"overlay-{index}"
                fetcher(overlay, overlay_root, output, evidence, env, runner, f"fetch-overlay-{index}")
                for name in overlay.get("paths", []):
                    relative = strict.relative_path(name)
                    if str(relative) not in ("vendor/qcom/opensource/mm-drivers", "vendor/qcom/opensource/synx-kernel"):
                        raise strict.BuildError("source map overlay may only fill the declared public dependency directories")
                    origin, target = overlay_root.joinpath(*relative.parts), tree.joinpath(*relative.parts)
                    if not origin.is_dir() or target.exists():
                        raise strict.BuildError("dependency overlay is absent or would overwrite existing source")
                    shutil.copytree(origin, target, symlinks=True)
            gpu = tree / "vendor/qcom/opensource/graphics-kernel"
            synx = tree / "vendor/qcom/opensource/synx-kernel"
            mm = tree / "vendor/qcom/opensource/mm-drivers"
            fence = mm / "hw_fence"
            for path in (gpu / "Kbuild", synx / "Kbuild", fence / "Kbuild", mm / "config/kalamammdrivers.conf",
                         mm / "config/kalamammdriversconf.h", kernel / "scripts/kconfig/merge_config.sh"):
                require_file(path)
            # Verify GPU source fingerprints before any expensive native build.
            patcher.build_changes(gpu)
            evidence["gpu_source_baseline"] = patcher.identify_baseline({name: (gpu / name).read_text(encoding="utf-8") for name in patcher.FILES})
            evidence["vendor_cpu_headers_before"] = {}
            for name in strict.CPU_HEADERS:
                target = require_file(kernel / name)
                evidence["vendor_cpu_headers_before"][name] = strict.sha256(target)
                shutil.copyfile(common_source / name, target)
            evidence["vendor_cpu_headers_aligned_to_current_common"] = True
            vendor_out = work / "vendor-out"
            vendor_out.mkdir()
            fragment = require_file(kernel / "arch/arm64/configs/vendor/pineapple_GKI.config")
            extra = work / "gpu-vendor.fragment"
            lines = ["CONFIG_ARCH_PINEAPPLE=y", "CONFIG_ARM_SMMU=m", "CONFIG_QCOM_IOMMU_UTIL=m",
                     "CONFIG_QTI_IOMMU_SUPPORT=m", "CONFIG_QCOM_SECURE_BUFFER=m", "CONFIG_QCOM_RPROC_COMMON=m"]
            critical = strict.CPU_KEYS | {k for k in current_cfg if k.startswith(("CONFIG_ARM64_VA_BITS_", "CONFIG_ARM64_PA_BITS_", "CONFIG_KASAN"))}
            for key in sorted(critical):
                value = current_cfg.get(key, "n")
                lines.append(f"# {key} is not set" if value == "n" else f"{key}={value}")
            # Last fragment wins in merge_config.sh, so this overrides pineapple_GKI.config.
            lines += [f"# {key} is not set" for key in KGSL_IRRELEVANT_MODULES]
            extra.write_text("\n".join(lines) + "\n", encoding="utf-8")
            run_stage("merge-vendor-config", ["bash", str(kernel / "scripts/kconfig/merge_config.sh"), "-m", "-r", "-O", str(vendor_out),
                str(common / ".config"), str(fragment), str(extra)], output, evidence, env, runner)
            base_make = make_args(kernel, vendor_out, common, release, max(1, getattr(args, "jobs", None) or os.cpu_count() or 1))
            run_stage("vendor-olddefconfig", base_make + ["olddefconfig"], output, evidence, env, runner)
            run_stage("vendor-modules-prepare", base_make + ["modules_prepare"], output, evidence, env, runner)
            validate_prepared_config(common, vendor_out, release)
            evidence["prepared_vendor_cpu_config_matches_common"] = True
            if current_cfg.get("CONFIG_DEBUG_INFO_BTF_MODULES") == "y":
                require_file(common / "vmlinux")
            # No M= and no Image target: native mixed build generates dependency
            # closure and Module.symvers from GKI vmlinux.symvers + modules-only.
            # This stage exists only to produce a real vendor Module.symvers; the
            # GPU modules themselves are built separately below with M=.
            #
            # This stage is strict. Tolerating its exit code cannot work: -k lets
            # make continue past a broken target, but the tree-wide modpost that
            # writes Module.symvers never runs, so a "tolerated" failure only
            # resurfaces later as an absent Module.symvers and hides the compile
            # error that caused it. Anything that genuinely does not belong to the
            # GPU build is excluded in the config fragment above instead. -k and
            # MODPOST_WARN remain so one broken target still reports every other
            # error in a single run rather than one per CI attempt.
            run_stage("vendor-native-modules", base_make + ["-k", "KBUILD_MODPOST_WARN=1", "modules"],
                      output, evidence, env, runner)
            validate_vendor_symbols(common, vendor_out)
            shutil.copyfile(vendor_out / ".config", output / "vendor-config.txt")
            vendor_headers = {str(p): strict.sha256(p) for p in
                (vendor_out / ".config", vendor_out / "include/generated/autoconf.h", vendor_out / "include/generated/utsrelease.h")}
            run_stage("synx-modules", base_make + [f"M={synx}", f"SYNX_ROOT={synx}", "CONFIG_ARCH_PINEAPPLE=y", "modules"], output, evidence, env, runner)
            synx_symvers = require_file(synx / "Module.symvers")
            run_stage("hw-fence-modules", base_make + [f"M={fence}", f"MSM_HW_FENCE_ROOT={mm}/", "CONFIG_ARCH_PINEAPPLE=y",
                f"KBUILD_EXTRA_SYMBOLS={synx_symvers}", "modules"], output, evidence, env, runner)
            fence_symvers = require_file(fence / "Module.symvers")
            manifest = {"sdk_format_version": 1, "kernel_version": args.kernel_version,
                "common_out_sha256": {n: strict.sha256(common / n) for n in strict.SDK_BIND_FILES},
                "common_source_sha256": {n: strict.sha256(common_source / n) for n in strict.CPU_HEADERS},
                "vendor_kernel": str(kernel.relative_to(work)).replace("\\", "/"),
                "vendor_out": str(vendor_out.relative_to(work)).replace("\\", "/"),
                "gpu_source": str(gpu.relative_to(work)).replace("\\", "/"),
                "dependency_symvers": [str(p.relative_to(work)).replace("\\", "/") for p in
                    (vendor_out / "Module.symvers", synx_symvers, fence_symvers)]}
            (work / "gpu-va48-sdk.json").write_text(json.dumps(manifest), encoding="utf-8")
            _, _, _, merged, cfg, release, available, dep_hashes = strict.bind_sdk(work, common, common_source,
                args.kernel_version, {n: strict.sha256(common / n) for n in strict.COMMON_FILES}, manifest["common_source_sha256"], work)
            originals, changed = patcher.build_changes(gpu)
            patcher.apply_changes(gpu, originals, changed)
            strict.clean_gpu_objects(gpu)
            run_stage("kgsl-va48-module", base_make + [f"M={gpu}", f"KERNEL_SRC={kernel}", f"KGSL_PATH={gpu}",
                "CONFIG_ARCH_PINEAPPLE=y", "CONFIG_QCOM_KGSL_GPU_VA48=y", f"KBUILD_EXTRA_SYMBOLS={merged}", "modules"], output, evidence, env, runner)
            if any(strict.sha256(Path(p)) != old for p, old in vendor_headers.items()):
                raise strict.BuildError("vendor generated configuration/headers changed during external module builds")
            module, vermagic, imports = strict.validate_module(gpu, release, cfg, available, runner, env)
            modules = export_dependency_modules(output, vendor_out, synx, fence, gpu, release, vermagic, available, runner, env)
            shutil.copyfile(module, output / "msm_kgsl.ko")
            shutil.copyfile(vendor_out / "Module.symvers", output / "vendor-Module.symvers")
            shutil.copyfile(synx_symvers, output / "synx-Module.symvers")
            shutil.copyfile(fence_symvers, output / "hw-fence-Module.symvers")
            evidence.update(status="success", compiled=True, vermagic=vermagic, module_sha256=strict.sha256(module),
                            module_imported_crcs=imports, dependency_symvers_sha256=dep_hashes, deployed_module_candidates=modules)
    except Exception as exc:
        evidence["error"] = str(exc)
        evidence["status"] = "failed"
        (output / "msm_kgsl.ko").unlink(missing_ok=True)
        if (output / "dependencies").is_dir():
            shutil.rmtree(output / "dependencies")
        (output / "module-manifest.json").unlink(missing_ok=True)
        raise
    finally:
        changed = [p for p, old in protected.items() if not Path(p).is_file() or strict.sha256(Path(p)) != old]
        evidence["common_unchanged"] = not changed
        evidence["common_protected_sha256_after"] = {p: strict.sha256(Path(p)) if Path(p).is_file() else None for p in protected}
        if changed:
            evidence.update(status="failed", error="protected current CPU source/output changed")
            (output / "msm_kgsl.ko").unlink(missing_ok=True)
            if (output / "dependencies").is_dir():
                shutil.rmtree(output / "dependencies")
            (output / "module-manifest.json").unlink(missing_ok=True)
        (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if changed:
            raise strict.BuildError(evidence["error"])
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-source", required=True, type=Path)
    parser.add_argument("--common-out", required=True, type=Path)
    parser.add_argument("--kernel-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--cpu-patch", type=Path)
    args = parser.parse_args(argv)
    try:
        build(args)
        print(f"GPU VA48 module built from public sources: {args.output / 'msm_kgsl.ko'}")
        return 0
    except Exception as exc:
        print(f"Public-source GPU build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
