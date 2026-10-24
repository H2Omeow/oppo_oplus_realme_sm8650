#!/usr/bin/env python3
"""为仓库现有 6.1.x 构建统一预检、应用补丁并核对最终配置。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


HERE = Path(__file__).resolve().parent
SUPPORTED = {"6.1." + n for n in ("57", "75", "115", "118", "128", "134", "141")}
VA_FILES = (
    "arch/arm64/include/asm/memory.h", "arch/arm64/include/asm/assembler.h",
    "arch/arm64/mm/init.c", "arch/arm64/mm/mmu.c", "arch/arm64/Kconfig",
    "mm/util.c", "kernel/module/version.c",
)
LOG_FILES = ("arch/arm64/kernel/setup.c", "init/main.c", "kernel/reboot.c", "lib/fonts/Kconfig")
DEFCONFIG = "arch/arm64/configs/gki_defconfig"


def boolean(value):
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("开关必须是 true 或 false")
    return value == "true"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_values(path):
    values = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"CONFIG_(\w+)=(.*)", line)
        disabled = re.fullmatch(r"# CONFIG_(\w+) is not set", line)
        if match:
            values[match[1]] = match[2]
        elif disabled:
            values[disabled[1]] = "n"
    return values


def write_config(path, settings):
    lines = path.read_text().splitlines()
    lines = [line for line in lines if not any(
        line.startswith(f"CONFIG_{name}=") or line == f"# CONFIG_{name} is not set"
        for name in settings)]
    lines.extend(f"CONFIG_{name}={value}" if value != "n" else f"# CONFIG_{name} is not set"
                 for name, value in settings.items())
    path.write_text("\n".join(lines) + "\n")


def run(script, args, log):
    result = subprocess.run([sys.executable, str(HERE / script), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log.write_text(result.stdout)
    print(result.stdout, end="", flush=True)
    if result.returncode:
        raise ValueError(f"{script} 失败，详细输出：{log}")


def check_hashes():
    manifest = json.loads((HERE / "script-sha256.json").read_text())
    for name, expected in manifest.items():
        if digest(HERE / name) != expected:
            raise ValueError(f"补丁指纹不符：{name}")
    if (HERE / "patch_va48.py").read_bytes() != (HERE.parent.parent / "scripts/patch_va48.py").read_bytes():
        raise ValueError("两个 VA48 脚本副本不一致")


def apply(args):
    check_hashes()
    baseline = config_values(args.tree / DEFCONFIG)
    if args.va48:
        for name in ("ARM64_16K_PAGES", "ARM64_64K_PAGES", "KASAN_GENERIC", "KASAN_SW_TAGS"):
            if baseline.get(name) == "y":
                raise ValueError(f"defconfig 显式启用了不兼容项 CONFIG_{name}=y")
    selected = []
    if args.va48:
        selected.append(("patch_va48.py", [args.tree], "va48"))
        run("verify_layout.py", [], args.evidence / "va48-layout.log")
    if args.boot_log:
        log_args = [args.tree, "--profile", args.profile]
        if args.framebuffer:
            log_args += ["--framebuffer", args.framebuffer]
        selected.append(("patch_boot_log.py", log_args, "boot-log"))
    # 两个补丁均完成预检后才写入；失败会恢复本入口涉及的全部源码和 defconfig。
    for script, options, name in selected:
        run(script, [*options, "--check"], args.evidence / f"{name}-preflight.log")
    files = ([*VA_FILES] if args.va48 else []) + ([*LOG_FILES] if args.boot_log else []) + [DEFCONFIG]
    originals = {name: (args.tree / name).read_bytes() for name in files}
    try:
        for script, options, name in selected:
            run(script, options, args.evidence / f"{name}-patch.log")
        settings = {}
        if args.va48:
            settings.update(ARM64_4K_PAGES="y", ARM64_16K_PAGES="n", ARM64_64K_PAGES="n",
                            ARM64_VA_BITS_39="n", ARM64_VA_BITS_48="y", ARM64_VA_BITS_52="n",
                            COMPAT="y", MODVERSIONS="y", ARCH_MMAP_RND_BITS="24")
        if args.boot_log:
            settings.update(PRINTK="y", KALLSYMS="y", FONT_SUPPORT="y", FONTS="y", FONT_8x16="y")
        if settings:
            write_config(args.tree / DEFCONFIG, settings)
    except BaseException:
        for name, original in originals.items():
            (args.tree / name).write_bytes(original)
        raise
    print("所选补丁和 defconfig 已完成；编译前还会核对生成的 .config。")


def verify(args):
    config = args.tree / args.config
    values = config_values(config)
    required = {}
    if args.va48 or args.boot_log:
        required.update(ARM64="y", ARM64_4K_PAGES="y")
    if args.va48:
        required.update(ARM64_VA_BITS="48", ARM64_VA_BITS_48="y", PGTABLE_LEVELS="4",
                        COMPAT="y", MODVERSIONS="y")
    if args.boot_log:
        required.update(OF="y", OF_ADDRESS="y", PRINTK="y", KALLSYMS="y", FONT_SUPPORT="y", FONT_8x16="y")
    failures = [f"CONFIG_{key} 应为 {expected}，实际 {values.get(key, '<未设置>')}"
                for key, expected in required.items() if values.get(key) != expected]
    if args.va48 or args.boot_log:
        for name in ("ARM64_16K_PAGES", "ARM64_64K_PAGES"):
            if values.get(name) == "y":
                failures.append(f"不支持 CONFIG_{name}=y")
    if args.va48:
        for name in ("KASAN_GENERIC", "KASAN_SW_TAGS"):
            if values.get(name) == "y":
                failures.append(f"VA39 固定内核布局未适配 CONFIG_{name}=y")
        for name in ("ARCH_MMAP_RND_BITS", "ARCH_MMAP_RND_BITS_MAX"):
            value = values.get(name, "")
            if not value.isdigit() or not 0 < int(value) <= 24:
                failures.append(f"CONFIG_{name} 必须处于 1..24，实际 {value or '<未设置>'}")
        for name in ("ARM64_VA_BITS_39", "ARM64_VA_BITS_52"):
            if values.get(name) == "y":
                failures.append(f"VA48 选择冲突：CONFIG_{name}=y")
    (args.evidence / "kernel.config").write_bytes(config.read_bytes())
    report = {"通过": not failures, "错误": failures, "已核对配置": required,
              "mmap随机化": {k: values.get(k) for k in ("ARCH_MMAP_RND_BITS", "ARCH_MMAP_RND_BITS_MAX")},
              "配置SHA256": digest(config)}
    (args.evidence / "config-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if failures:
        raise ValueError("；".join(failures))
    print("生成的内核配置检查通过。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "verify"))
    parser.add_argument("tree", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--va48", type=boolean, default=False)
    parser.add_argument("--boot-log", type=boolean, default=False)
    parser.add_argument("--profile", choices=("auto", "oneplus12-legacy", "custom"), default="auto")
    parser.add_argument("--framebuffer", default="")
    parser.add_argument("--config", type=Path, default=Path("out/.config"))
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.tree = args.tree.resolve()
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    try:
        source = (args.tree / "Makefile").read_text()
        numbers = [re.search(rf"^{key}\s*=\s*(\d+)\s*$", source, re.M)
                   for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL")]
        version = ".".join(match[1] for match in numbers) if all(numbers) else "未知"
        if args.action == "apply":
            metadata = {"内核版本": version, "预期版本": args.expected_version,
                        "构建仓库提交": os.environ.get("GITHUB_SHA", "本地验证"),
                        "VA48": args.va48, "屏幕日志": args.boot_log,
                        "屏幕配置": args.profile, "自定义布局": args.framebuffer,
                        "实机验证范围": "基础 r16：一加 ACE5 / 6.1.118；其余版本、设备及新版日志未实机验证"}
            (args.evidence / "features.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        if version not in SUPPORTED or version != args.expected_version:
            raise ValueError(f"源码版本 {version} 与已支持入口 {args.expected_version} 不匹配")
        (apply if args.action == "apply" else verify)(args)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        message = f"配置失败：{exc}"
        print(message, file=sys.stderr)
        (args.evidence / f"{args.action}-error.log").write_text(message + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
