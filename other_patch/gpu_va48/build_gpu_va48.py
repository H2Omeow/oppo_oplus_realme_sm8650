#!/usr/bin/env python3
"""Build the experimental KGSL module from a hash-bound prepared vendor SDK.

This does not prepare a vendor SDK, build a CPU kernel, or modify common-out.
An SDK must contain gpu-va48-sdk.json in its root. See sdk-manifest.example.json.
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
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

sys.dont_write_bytecode = True
COMMON_FILES = (".config", "arch/arm64/boot/Image", "vmlinux.symvers", "System.map")
# SDK compatibility is tied to ABI/config inputs, not a previously built Image.
# The actual current Image remains mandatory and hash-protected during this run.
SDK_BIND_FILES = (".config", "vmlinux.symvers", "System.map")
CPU_HEADERS = tuple("arch/arm64/include/asm/" + p for p in
                    ("memory.h", "page.h", "processor.h", "pgtable-hwdef.h"))
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_UNPACKED_BYTES = 24 * 1024**3
MAX_MEMBERS = 300000
CPU_KEYS = {
    "CONFIG_ARM64", "CONFIG_64BIT", "CONFIG_ARM64_VA_BITS", "CONFIG_PGTABLE_LEVELS",
    "CONFIG_PAGE_SHIFT", "CONFIG_PAGE_SIZE", "CONFIG_ARM64_4K_PAGES",
    "CONFIG_ARM64_16K_PAGES", "CONFIG_ARM64_64K_PAGES", "CONFIG_MODULES",
    "CONFIG_MODVERSIONS", "CONFIG_MODULE_UNLOAD", "CONFIG_SMP", "CONFIG_PREEMPT",
    "CONFIG_PREEMPT_BUILD", "CONFIG_PREEMPT_DYNAMIC", "CONFIG_PREEMPT_RT",
    "CONFIG_CFI_CLANG", "CONFIG_CFI_PERMISSIVE", "CONFIG_LTO_CLANG",
    "CONFIG_LTO_CLANG_THIN", "CONFIG_LTO_CLANG_FULL", "CONFIG_LTO_NONE",
    "CONFIG_SHADOW_CALL_STACK",
    "CONFIG_ARM64_PA_BITS", "CONFIG_ARM64_TAGGED_ADDR_ABI", "CONFIG_ARM64_MTE", "CONFIG_ANDROID_KABI_RESERVE",
}
INJECTED_ENV = (
    "MAKEFLAGS", "GNUMAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "KBUILD_NSDEPS", "KBUILD_EXTMOD",
    "KBUILD_MODPOST_WARN", "KBUILD_MODPOST_NOFINAL",
    "KBUILD_EXTRA_SYMBOLS", "KCFLAGS", "KCPPFLAGS", "CFLAGS_MODULE", "LDFLAGS_MODULE",
    "KBUILD_USERCFLAGS", "KBUILD_USERLDFLAGS", "CONFIG_QCOM_KGSL_GPU_VA48",
)


class BuildError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def relative_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise BuildError(f"unsafe SDK path: {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise BuildError(f"unsafe SDK path: {name!r}")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    if any(part.rstrip(" .") != part or part.split(".")[0].lower() in reserved for part in p.parts):
        raise BuildError(f"unsafe SDK path component: {name!r}")
    return p


def inside(root: Path, name: str, *, directory: bool = False) -> Path:
    rel = relative_path(name)
    candidate = root.joinpath(*rel.parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise BuildError(f"SDK path escapes root: {name}")
    for part in (candidate, *candidate.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise BuildError(f"SDK symlinks are not allowed: {name}")
    if directory and not candidate.is_dir():
        raise BuildError(f"missing SDK directory: {name}")
    if not directory and (not candidate.is_file() or candidate.stat().st_size == 0):
        raise BuildError(f"missing/empty SDK file: {name}")
    return candidate


def check_archive_members(entries):
    seen = set()
    total = 0
    for name, size, regular, directory in entries:
        if name in (".", "./") and directory:
            continue
        path = relative_path(name)
        key = str(path).casefold()
        if key in seen:
            raise BuildError(f"duplicate/case-colliding archive path: {name}")
        seen.add(key)
        if not regular and not directory:
            raise BuildError(f"archive links/special files are not allowed: {name}")
        total += size
        if size < 0 or total > MAX_UNPACKED_BYTES or len(seen) > MAX_MEMBERS:
            raise BuildError("SDK archive exceeds extraction limits")


def extract_sdk(archive: Path, destination: Path):
    """Validate every member first; never use extractall or follow archive links."""
    destination.mkdir(parents=True, exist_ok=False)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            members = z.infolist()
            entries = []
            for info in members:
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                directory = info.is_dir()
                regular = not directory and kind in (0, stat.S_IFREG)
                if info.flag_bits & 1:
                    raise BuildError("encrypted SDK ZIP entries are not supported")
                entries.append((info.filename, info.file_size, regular, directory and kind in (0, stat.S_IFDIR)))
            check_archive_members(entries)
            for info in members:
                if info.filename in (".", "./"):
                    continue
                path = destination.joinpath(*relative_path(info.filename).parts)
                if info.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.chmod(path, (info.external_attr >> 16) & 0o777 or 0o644)
    else:
        try:
            with tarfile.open(archive, "r:*") as t:
                members = t.getmembers()
                check_archive_members((m.name, m.size, m.isfile(), m.isdir()) for m in members)
                for info in members:
                    if info.name in (".", "./"):
                        continue
                    path = destination.joinpath(*relative_path(info.name).parts)
                    if info.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with t.extractfile(info) as src, path.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.chmod(path, info.mode & 0o777)
        except tarfile.TarError as exc:
            raise BuildError(f"not a supported tar/ZIP SDK: {exc}") from exc


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise BuildError("SDK redirect must remain HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_sdk(url: str, expected_sha: str, destination: Path):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise BuildError("SDK URL must be HTTPS without embedded credentials or fragment")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha or ""):
        raise BuildError("a complete SDK SHA256 is required")
    opener = urllib.request.build_opener(HttpsOnlyRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "KGSL-VA48-CI/1"})
    count = 0
    with opener.open(request, timeout=90) as src, destination.open("xb") as dst:
        if urllib.parse.urlsplit(src.geturl()).scheme != "https":
            raise BuildError("SDK response URL is not HTTPS")
        for block in iter(lambda: src.read(1024 * 1024), b""):
            count += len(block)
            if count > MAX_ARCHIVE_BYTES:
                raise BuildError("SDK download exceeds archive size limit")
            dst.write(block)
    if sha256(destination) != expected_sha.lower():
        raise BuildError("SDK SHA256 mismatch")


def read_config(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        if m:
            result[m[1]] = m[2]
        m = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if m:
            result[m[1]] = "n"
    return result


def read_autoconf(path: Path) -> dict[str, str]:
    result = {}
    for name, value in re.findall(r"^#define (CONFIG_[A-Z0-9_]+) (.+)$", path.read_text(encoding="utf-8"), re.M):
        if name.endswith("_MODULE"):
            result[name[:-7]] = "m"
        else:
            result[name] = "y" if value == "1" and name not in ("CONFIG_PAGE_SHIFT", "CONFIG_PGTABLE_LEVELS", "CONFIG_ARM64_VA_BITS", "CONFIG_ARM64_PA_BITS") else value
    return result


def read_release(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'^#define UTS_RELEASE "([^"\n]+)"$', text, re.M)
    if not m:
        raise BuildError(f"invalid kernel UTS_RELEASE header: {path}")
    return m[1]


def symbols(path: Path) -> dict[str, tuple[int, str]]:
    result = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) not in (4, 5) or not re.fullmatch(r"0x[0-9a-fA-F]{8,16}", fields[0]):
            raise BuildError(f"invalid Module.symvers line {path.name}:{number}")
        name, crc = fields[1], int(fields[0], 16)
        if name in result and not same_export(result[name], (crc, line)):
            raise BuildError(f"conflicting CRC/export/namespace inside {path.name}: {name}")
        result[name] = (crc, line)
    if not result:
        raise BuildError(f"empty symbol table: {path}")
    return result


def same_export(left, right):
    def signature(entry):
        fields = entry[1].split()
        return entry[0], fields[3], fields[4] if len(fields) == 5 else ""
    return signature(left) == signature(right)


def bind_sdk(root: Path, common: Path, common_source: Path, expected_version: str,
             hashes: dict[str, str], header_hashes: dict[str, str], work: Path):
    manifest = json.loads(inside(root, "gpu-va48-sdk.json").read_text(encoding="utf-8"))
    if manifest.get("sdk_format_version") != 1 or manifest.get("kernel_version") != expected_version:
        raise BuildError("SDK format/kernel_version does not match this workflow")
    expected_bindings = {name: hashes[name] for name in SDK_BIND_FILES}
    if manifest.get("common_out_sha256") != expected_bindings:
        raise BuildError("SDK manifest is not bound to the exact current common-out ABI/config artifacts")
    if manifest.get("common_source_sha256") != header_hashes:
        raise BuildError("SDK manifest is not bound to the exact current CPU layout headers")
    vk = inside(root, manifest.get("vendor_kernel"), directory=True)
    vo = inside(root, manifest.get("vendor_out"), directory=True)
    gpu = inside(root, manifest.get("gpu_source"), directory=True)
    if any(p.is_relative_to(common) or common.is_relative_to(p) for p in (vk, vo, gpu)):
        raise BuildError("SDK build paths must be separate from common-out")
    makefile = inside(root, str((vk / "Makefile").relative_to(root)).replace("\\", "/"))
    if "KBUILD_MIXED_TREE" not in makefile.read_text(encoding="utf-8"):
        raise BuildError("vendor kernel lacks the required mixed-GKI build support")
    for name in CPU_HEADERS:
        vendor_header = inside(root, str((vk / name).relative_to(root)).replace("\\", "/"))
        if sha256(vendor_header) != header_hashes[name]:
            raise BuildError(f"vendor/common CPU layout header differs: {name}; no headers will be overwritten")
    cfg = read_config(common / ".config")
    vendor_cfg = read_config(inside(root, str((vo / ".config").relative_to(root)).replace("\\", "/")))
    for key in ("CONFIG_ARM64", "CONFIG_64BIT", "CONFIG_ARM64_4K_PAGES", "CONFIG_MODULES", "CONFIG_MODVERSIONS"):
        if cfg.get(key) != "y":
            raise BuildError(f"common kernel requires {key}=y")
    if cfg.get("CONFIG_MODULE_SIG_FORCE") == "y":
        raise BuildError("enforced module signing is unsupported: this workflow does not manage a trusted signing key")
    if vendor_cfg.get("CONFIG_ARM_SMMU") not in ("y", "m") or vendor_cfg.get("CONFIG_ARCH_PINEAPPLE") != "y":
        raise BuildError("vendor SDK requires ARM_SMMU and ARCH_PINEAPPLE configuration")
    critical = CPU_KEYS | {k for k in cfg.keys() | vendor_cfg.keys()
                          if k.startswith(("CONFIG_ARM64_VA_BITS_", "CONFIG_ARM64_PA_BITS_", "CONFIG_KASAN"))}
    auto = read_autoconf(inside(root, str((vo / "include/generated/autoconf.h").relative_to(root)).replace("\\", "/")))
    common_auto = read_autoconf(common / "include/generated/autoconf.h")
    for key in critical:
        if cfg.get(key, "n") != vendor_cfg.get(key, "n"):
            raise BuildError(f"CPU/header configuration mismatch: {key}")
        if auto.get(key, "n") != vendor_cfg.get(key, "n"):
            raise BuildError(f"stale vendor generated autoconf.h: {key}")
        if common_auto.get(key, "n") != cfg.get(key, "n"):
            raise BuildError(f"stale common generated autoconf.h: {key}")
    release = read_release(common / "include/generated/utsrelease.h")
    vendor_release = read_release(inside(root, str((vo / "include/generated/utsrelease.h").relative_to(root)).replace("\\", "/")))
    if release != vendor_release or not (release == expected_version or release.startswith(expected_version + "-")):
        raise BuildError("vendor/common kernel release does not match workflow version")
    all_symbols = symbols(common / "vmlinux.symvers")
    # For M= builds Makefile.modpost reads vendor_out/Module.symvers, not the
    # mixed tree table. If absent it can add -w automatically. Require it, then
    # replace only this copied SDK table with the checked GKI+vendor union.
    vendor_symvers = inside(root, str((vo / "Module.symvers").relative_to(root)).replace("\\", "/"))
    dependencies = {str(vendor_symvers.relative_to(root)).replace("\\", "/"): sha256(vendor_symvers)}
    vendor_exports = symbols(vendor_symvers)
    for symbol, entry in vendor_exports.items():
        if symbol in all_symbols and not same_export(all_symbols[symbol], entry):
            raise BuildError(f"vendor symbol CRC/namespace conflicts with GKI: {symbol}")
        all_symbols.setdefault(symbol, entry)
    vendor_union = dict(all_symbols)
    dependency_paths = manifest.get("dependency_symvers")
    if not isinstance(dependency_paths, list) or not dependency_paths:
        raise BuildError("SDK must provide dependency_symvers for vendor/hw_fence/synx dependencies")
    merged_lines = []
    for name in dependency_paths:
        path = inside(root, name)
        dependencies[name] = sha256(path)
        if path == vendor_symvers:
            continue
        for symbol, entry in symbols(path).items():
            if symbol in all_symbols:
                if not same_export(all_symbols[symbol], entry):
                    raise BuildError(f"dependency symbol CRC/namespace conflicts with GKI/another dependency: {symbol}")
            else:
                all_symbols[symbol] = entry
                merged_lines.append(entry[1])
    for required in ("module_layout", "qcom_alloc_io_pgtable_ops", "qcom_free_io_pgtable_ops"):
        if required not in all_symbols:
            raise BuildError(f"required core/vendor symbol is absent: {required}")
    if len(all_symbols) == len(symbols(common / "vmlinux.symvers")):
        raise BuildError("dependency symbol tables provide no vendor exports")
    vendor_symvers.write_text("\n".join(e[1] for e in vendor_union.values()) + "\n", encoding="utf-8", newline="\n")
    merged = work / "dependency.symvers"
    merged.write_text("\n".join(merged_lines) + "\n", encoding="utf-8", newline="\n")
    return vk, vo, gpu, merged, cfg, release, all_symbols, dependencies


def clean_gpu_objects(gpu: Path):
    for path in sorted(gpu.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file() and (path.suffix in (".o", ".ko", ".cmd", ".mod", ".d") or
                path.name.endswith(".mod.c") or path.name in ("Module.symvers", "modules.order")):
            path.unlink()


def validate_module(gpu: Path, release: str, cfg, available_symbols, runner, env):
    module = gpu / "msm_kgsl.ko"
    if not module.is_file():
        raise BuildError("make did not produce msm_kgsl.ko")
    content = module.read_bytes()
    if len(content) < 64 or content[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", content, 16) != (1, 183):
        raise BuildError("msm_kgsl.ko is not an AArch64 ELF64 relocatable module")
    if not (gpu / "kgsl_iommu.o").is_file() or not (gpu / ".kgsl_iommu.o.cmd").is_file():
        raise BuildError("KGSL IOMMU source was not compiled")
    command = (gpu / ".kgsl_iommu.o.cmd").read_text(encoding="utf-8")
    if not re.search(r"(?:^|\s)-DCONFIG_QCOM_KGSL_GPU_VA48=1(?:\s|$)", command):
        raise BuildError("IOMMU compile command lacks the GPU VA48 define")
    for marker in (b"GPU VA48: TTBR0 IAS=", b"GPU VA48: powered SMMU"):
        if marker not in content:
            raise BuildError("module does not contain the enabled GPU VA48 implementation")
    vermagic = runner(["modinfo", "-F", "vermagic", str(module)], capture_output=True, text=True,
                      env=env, check=True).stdout.strip()
    tokens = vermagic.split()
    if not tokens or tokens[0] != release or "aarch64" not in tokens:
        raise BuildError("module vermagic release/architecture mismatch")
    for key, token in (("CONFIG_MODVERSIONS", "modversions"), ("CONFIG_MODULE_UNLOAD", "mod_unload"), ("CONFIG_SMP", "SMP")):
        if (token in tokens) != (cfg.get(key) == "y"):
            raise BuildError(f"module vermagic configuration mismatch: {key}")
    result = runner(["modprobe", "--dump-modversions", str(module)], capture_output=True, text=True,
                    env=env, check=True).stdout
    imported = {}
    for line in result.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"0x[0-9a-fA-F]{8,16}", fields[0]):
            raise BuildError("cannot parse module imported CRCs")
        crc, name = int(fields[0], 16), fields[1]
        if name not in available_symbols or available_symbols[name][0] != crc:
            raise BuildError(f"module imported CRC differs from GKI/vendor symbols: {name}")
        imported[name] = f"0x{crc:08x}"
    if "module_layout" not in imported:
        raise BuildError("module has no verifiable module_layout CRC")
    nm_output = runner(["llvm-nm", "--undefined-only", "--format=posix", str(module)],
                      capture_output=True, text=True, env=env, check=True).stdout
    for line in nm_output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] not in ("U", "w", "v"):
            raise BuildError("cannot parse module undefined symbol table")
        if fields[1] == "U" and (fields[0] not in available_symbols or fields[0] not in imported):
            raise BuildError(f"strong module import has no checked symbol/CRC: {fields[0]}")
    return module, vermagic, imported


def build(args, *, runner=subprocess.run):
    common = args.common_out.resolve()
    common_source = args.common_source.resolve()
    output = args.output.resolve()
    if output.is_relative_to(common_source) or common_source.is_relative_to(output) or output.is_relative_to(common) or common.is_relative_to(output):
        raise BuildError("output and common source/out must be separate")
    if output.exists() and any(output.iterdir()):
        raise BuildError("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"status": "failed", "experimental": True, "hardware_tested": False,
                "kernel_version": args.kernel_version, "common_out_sha256_before": {}, "common_source_sha256_before": {}}
    protected = {}
    cpu_patch = getattr(args, "cpu_patch", None)
    if cpu_patch is None:
        candidate = Path(__file__).resolve().parent.parent / "va48/patch_va48.py"
        cpu_patch = candidate if candidate.is_file() else None
    log_path = output / "build.log"
    copied_module = None
    try:
        for name in (*COMMON_FILES, "include/generated/utsrelease.h", "include/generated/autoconf.h"):
            path = common / name
            if not path.is_file() or path.stat().st_size == 0:
                raise BuildError(f"missing common-out build artifact: {name}")
            protected[str(path)] = sha256(path)
            if name in COMMON_FILES:
                evidence["common_out_sha256_before"][name] = protected[str(path)]
        for name in CPU_HEADERS:
            path = common_source / name
            if not path.is_file() or path.stat().st_size == 0:
                raise BuildError(f"missing current common CPU layout header: {name}")
            protected[str(path)] = sha256(path)
            evidence["common_source_sha256_before"][name] = protected[str(path)]
        if cpu_patch is not None:
            cpu_patch = cpu_patch.resolve()
            if not cpu_patch.is_file():
                raise BuildError("specified CPU patch script is missing")
            protected[str(cpu_patch)] = sha256(cpu_patch)
            evidence["cpu_patch_sha256_before"] = protected[str(cpu_patch)]
        with tempfile.TemporaryDirectory(prefix="kgsl-va48-sdk-") as temp:
            work = Path(temp)
            sdk = work / "sdk"
            if args.sdk_dir:
                if args.sdk_dir.is_symlink():
                    raise BuildError("local SDK root must not be a symlink")
                local = args.sdk_dir.resolve()
                if output.is_relative_to(local) or local.is_relative_to(output):
                    raise BuildError("output and local SDK must be separate")
                for path in local.rglob("*"):
                    if path.is_symlink():
                        raise BuildError("local SDK must not contain symlinks")
                if not local.is_dir() or local.is_symlink():
                    raise BuildError("invalid local SDK directory")
                shutil.copytree(local, sdk)
                evidence["sdk_mode"] = "local_directory_for_testing"
            else:
                archive = work / "sdk.archive"
                download_sdk(args.sdk_url, args.sdk_sha256, archive)
                extract_sdk(archive, sdk)
                evidence["sdk_mode"] = "https_sha256_archive"
                evidence["sdk_sha256"] = args.sdk_sha256.lower()
            vk, vo, gpu, merged, cfg, release, available, dep_hashes = bind_sdk(
                sdk, common, common_source, args.kernel_version, evidence["common_out_sha256_before"],
                evidence["common_source_sha256_before"], work)
            evidence["dependency_symvers_sha256"] = dep_hashes
            evidence["sdk_bind_sha256"] = {name: evidence["common_out_sha256_before"][name] for name in SDK_BIND_FILES}
            evidence["sdk_manifest_sha256"] = sha256(sdk / "gpu-va48-sdk.json")
            vendor_protected = {str(p): sha256(p) for p in
                (vo / ".config", vo / "include/generated/autoconf.h", vo / "include/generated/utsrelease.h")}
            spec = importlib.util.spec_from_file_location("kgsl_gpu_va48_patcher", Path(__file__).with_name("patch_gpu_va48.py"))
            patcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(patcher)
            originals, changed = patcher.build_changes(gpu)
            patcher.apply_changes(gpu, originals, changed)
            evidence["gpu_original_sha256"] = {n: hashlib.sha256(t.encode()).hexdigest() for n, t in originals.items()}
            evidence["gpu_patched_sha256"] = {n: hashlib.sha256(t.encode()).hexdigest() for n, t in changed.items()}
            clean_gpu_objects(gpu)
            env = dict(os.environ)
            for name in INJECTED_ENV:
                env.pop(name, None)
            command = ["make", "-C", str(vk), f"O={vo}", f"KBUILD_MIXED_TREE={common}",
                       f"KERNEL_SRC={vk}", f"KGSL_PATH={gpu}", f"M={gpu}", "ARCH=arm64", "LLVM=1",
                       "CONFIG_ARCH_PINEAPPLE=y", "CONFIG_QCOM_KGSL_GPU_VA48=y",
                       f"KBUILD_EXTRA_SYMBOLS={merged}", "KBUILD_MODPOST_WARN=", "KBUILD_MODPOST_NOFINAL=",
                       f"-j{max(1, os.cpu_count() or 1)}", "modules"]
            evidence["make_command"] = command
            with log_path.open("w", encoding="utf-8") as log:
                log.write("Building GPU module only; common-out is read-only input.\n")
                log.flush()
                result = runner(command, stdout=log, stderr=subprocess.STDOUT, text=True, env=env, check=False)
            if result.returncode:
                raise BuildError(f"GPU make/modpost failed with exit code {result.returncode}; see build.log")
            if any(sha256(Path(p)) != old for p, old in vendor_protected.items()):
                raise BuildError("vendor prepared configuration/headers changed during make")
            if re.search(r"(?:warning:.*(?:undefined|missing.*(?:symvers|symbol))|undefined!)",
                         log_path.read_text(encoding="utf-8", errors="replace"), re.I):
                raise BuildError("modpost reported missing/undefined symbols; refusing module")
            module, vermagic, imports = validate_module(gpu, release, cfg, available, runner, env)
            evidence.update({"status": "success", "compiled": True, "vermagic": vermagic,
                             "module_imported_crcs": imports, "module_sha256": sha256(module)})
            copied_module = output / "msm_kgsl.ko"
            shutil.copyfile(module, copied_module)
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        if copied_module:
            copied_module.unlink(missing_ok=True)
        raise
    finally:
        changed_protected = [p for p, old in protected.items() if not Path(p).is_file() or sha256(Path(p)) != old]
        evidence["common_out_unchanged"] = not changed_protected
        evidence["common_out_sha256_after"] = {name: sha256(common / name) if (common / name).is_file() else None for name in COMMON_FILES}
        evidence["common_source_sha256_after"] = {name: sha256(common_source / name) if (common_source / name).is_file() else None for name in CPU_HEADERS}
        if cpu_patch is not None:
            evidence["cpu_patch_sha256_after"] = sha256(cpu_patch) if cpu_patch.is_file() else None
        if changed_protected:
            evidence["status"] = "failed"
            evidence["error"] = "protected CPU artifacts changed during GPU build"
            (output / "msm_kgsl.ko").unlink(missing_ok=True)
        if not log_path.exists():
            log_path.write_text("GPU preflight failed before make.\n" + evidence.get("error", "") + "\n", encoding="utf-8")
        (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if changed_protected:
            raise BuildError(evidence["error"])
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-out", required=True, type=Path)
    parser.add_argument("--common-source", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sdk-url")
    source.add_argument("--sdk-dir", type=Path, help="local prepared SDK for testing; copied before use")
    parser.add_argument("--sdk-sha256")
    parser.add_argument("--kernel-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cpu-patch", type=Path, help="optional explicit CPU patch script to hash-protect")
    args = parser.parse_args(argv)
    try:
        result = build(args)
        print(f"GPU-only module built and validated: {args.output / 'msm_kgsl.ko'}")
        print(f"CPU artifacts unchanged: {result['common_out_unchanged']}")
        return 0
    except Exception as exc:
        print(f"GPU VA48 build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
