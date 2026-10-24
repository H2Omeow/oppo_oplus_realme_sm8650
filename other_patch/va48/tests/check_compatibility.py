#!/usr/bin/env python3
"""以固定真实源码验证七个 6.1.x 入口的四种补丁组合与失败保护。

示例：
  python3 other_patch/va48/tests/check_compatibility.py --sources /path/to/compat-sources
  python3 other_patch/va48/tests/check_compatibility.py --cache /tmp/va48-baselines

未指定 --sources 时，只从 GitHub 固定提交下载清单中的相关源文件，不克隆
整个内核。所有试验均在临时副本执行。配置检查使用明确的合成配置，检查
拒绝条件，不冒充 Kbuild 生成的配置、完整内核编译或实机验证。
"""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request


HERE = Path(__file__).resolve().parent.parent
VA_FILES = {
    'arch/arm64/include/asm/memory.h', 'arch/arm64/include/asm/assembler.h',
    'arch/arm64/mm/init.c', 'arch/arm64/mm/mmu.c', 'arch/arm64/Kconfig',
    'mm/util.c', 'kernel/module/version.c',
}
LOG_FILES = {'arch/arm64/kernel/setup.c', 'init/main.c', 'kernel/reboot.c', 'lib/fonts/Kconfig'}
DEFCONFIG = 'arch/arm64/configs/gki_defconfig'
CHECKS = []


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    CHECKS.append(label)
    print('通过：' + label, flush=True)


def snapshot(tree):
    return {p.relative_to(tree).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tree.rglob('*') if p.is_file()}


def command(script, *args, success=True):
    result = subprocess.run([sys.executable, str(HERE / script), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    if (result.returncode == 0) != success:
        raise AssertionError(f'{script} 返回 {result.returncode}，预期成功={success}\n{result.stdout}')
    return result.stdout


def fetch(source, version, info, rel, expected):
    dest = source / version / rel
    if not dest.exists():
        url = f'https://raw.githubusercontent.com/{info["repository"]}/{info["sha"]}/{rel}'
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'va48-compatibility-test'})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError(f'{version}/{rel} 下载指纹不符')
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)
    if hashlib.sha256(dest.read_bytes()).hexdigest() != expected:
        raise ValueError(f'{version}/{rel} 基线指纹不符；请使用未修改的固定提交源码')


def good_config(va48=False, boot_log=False):
    values = {
        'ARM64': 'y', 'ARM64_4K_PAGES': 'y', 'ARM64_16K_PAGES': 'n',
        'ARM64_64K_PAGES': 'n', 'ARM64_VA_BITS': '48' if va48 else '39',
        'ARM64_VA_BITS_48': 'y' if va48 else 'n',
        'ARM64_VA_BITS_39': 'n' if va48 else 'y', 'ARM64_VA_BITS_52': 'n',
        'PGTABLE_LEVELS': '4' if va48 else '3', 'COMPAT': 'y', 'MODVERSIONS': 'y',
        'ARCH_MMAP_RND_BITS': '24', 'ARCH_MMAP_RND_BITS_MAX': '24',
        'OF': 'y', 'OF_ADDRESS': 'y', 'PRINTK': 'y', 'KALLSYMS': 'y', 'FONT_SUPPORT': 'y',
        'FONTS': 'y', 'FONT_8x16': 'y',
    }
    return ''.join(f'CONFIG_{name}={value}\n' if value != 'n' else f'# CONFIG_{name} is not set\n'
                   for name, value in values.items())


def shared(action, tree, version, evidence, va48=False, boot_log=False, success=True):
    return command('configure_features.py', action, tree, '--expected-version', version,
                   '--va48', str(va48).lower(), '--boot-log', str(boot_log).lower(),
                   '--profile', 'auto', '--evidence', evidence, success=success)


def rejected_without_writes(script, tree, label, *args):
    before = snapshot(tree)
    command(script, tree, *args, success=False)
    check(snapshot(tree) == before, label)


def check_version(version, source, temporary, patches_only):
    base = source / version
    makefile = (base / 'Makefile').read_text()
    actual = '.'.join(re.search(rf'^{key}\s*=\s*(\d+)\s*$', makefile, re.M)[1]
                      for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'))
    check(actual == version, f'{version} 固定源码实际版本匹配入口')
    trial = temporary / version
    trial.mkdir()

    # 只读预检必须保持全部源文件的字节不变。
    preflight = trial / 'preflight'
    shutil.copytree(base, preflight)
    before = snapshot(preflight)
    va = command('patch_va48.py', preflight, '--check')
    log = command('patch_boot_log.py', preflight, '--check', '--profile', 'auto')
    check('checked=14 failed=0' in va and 'checked=8 expected=8 failed=0' in log and
          snapshot(preflight) == before, f'{version} 两种补丁全量预检且零写入')

    for va48 in (False, True):
        for boot_log in (False, True):
            name = f'va48-{int(va48)}-log-{int(boot_log)}'
            tree = trial / name
            shutil.copytree(base, tree)
            before = snapshot(tree)
            evidence = trial / (name + '-evidence')
            if patches_only:
                if va48:
                    output = command('patch_va48.py', tree)
                    check('applied=14 failed=0' in output, f'{version}/{name} VA48 共14处')
                if boot_log:
                    output = command('patch_boot_log.py', tree, '--profile', 'auto')
                    check('applied=8 expected=8 failed=0' in output, f'{version}/{name} 日志共8处')
            else:
                shared('apply', tree, version, evidence, va48, boot_log)
            after = snapshot(tree)
            changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            expected = (VA_FILES if va48 else set()) | (LOG_FILES if boot_log else set())
            if not patches_only and (va48 or boot_log):
                expected |= {DEFCONFIG}
            check(changed == expected, f'{version}/{name} 四种开关组合只修改所选文件')
            if va48:
                check('if (mm->mmap_base >= STACK_TOP)' in (tree / 'mm/util.c').read_text() and
                      'default 24 if (ARM64_VA_BITS=48' in (tree / 'arch/arm64/Kconfig').read_text(),
                      f'{version}/{name} 保留 VA39 分配窗口及24位随机化上限')
            if boot_log:
                helpers = (tree / 'arch/arm64/kernel/setup.c').read_text()
                check('bootlog_' in helpers and 'bootlog_read_simplefb' in helpers and
                      '0xd5100000' not in helpers,
                      f'{version}/{name} 自动屏幕配置未硬编码旧一加12地址')
            if not patches_only:
                config = tree / 'out/.config'
                config.parent.mkdir()
                config.write_text(good_config(va48, boot_log))
                before_verify = snapshot(tree)
                shared('verify', tree, version, evidence, va48, boot_log)
                check(snapshot(tree) == before_verify, f'{version}/{name} 合成最终配置检查只读通过')
                if va48 or boot_log:
                    config.write_text(config.read_text().replace('CONFIG_ARM64_4K_PAGES=y',
                                      '# CONFIG_ARM64_4K_PAGES is not set\nCONFIG_ARM64_16K_PAGES=y'))
                    before_verify = snapshot(tree)
                    shared('verify', tree, version, evidence, va48, boot_log, success=False)
                    check(snapshot(tree) == before_verify, f'{version}/{name} 最终16K配置拒绝且零写入')
                if va48:
                    for kasan in ('KASAN_GENERIC', 'KASAN_SW_TAGS'):
                        config.write_text(good_config(va48, boot_log) + f'CONFIG_{kasan}=y\n')
                        before_verify = snapshot(tree)
                        shared('verify', tree, version, evidence, va48, boot_log, success=False)
                        check(snapshot(tree) == before_verify,
                              f'{version}/{name} 最终{kasan}配置拒绝且零写入')

    # 故意破坏最后修改点，确认不会遗留较早文件上的半份补丁。
    for script, rel, old, new in (
        ('patch_va48.py', 'kernel/module/version.c', '\tstruct modversion_info *versions;', '\tstruct modversion_info *unknown_versions;'),
        ('patch_boot_log.py', 'kernel/reboot.c', 'void kernel_restart(char *cmd)', 'void kernel_restart(char *unknown_cmd)'),
    ):
        tree = trial / ('bad-anchor-' + script)
        shutil.copytree(base, tree)
        path = tree / rel
        text = path.read_text()
        check(text.count(old) == 1, f'{version}/{script} 测试破坏点唯一')
        path.write_text(text.replace(old, new, 1))
        rejected_without_writes(script, tree, f'{version}/{script} 坏锚点拒绝且零写入')
        if not patches_only:
            before = snapshot(tree)
            shared('apply', tree, version, trial / ('bad-anchor-evidence-' + script), True, True, success=False)
            check(snapshot(tree) == before, f'{version}/{script} 共享入口跨补丁预检失败且零写入')

    tree = trial / 'wrong-version'
    shutil.copytree(base, tree)
    path = tree / 'Makefile'
    path.write_text(re.sub(r'^PATCHLEVEL\s*=\s*1$', 'PATCHLEVEL = 6', path.read_text(), flags=re.M))
    for script in ('patch_va48.py', 'patch_boot_log.py'):
        rejected_without_writes(script, tree, f'{version}/{script} 非6.1版本拒绝且零写入')
    if not patches_only:
        before = snapshot(tree)
        shared('apply', tree, version, trial / 'wrong-version-evidence', True, True, success=False)
        check(snapshot(tree) == before, f'{version} 共享入口版本不符拒绝且零写入')

    tree = trial / 'invalid-config'
    shutil.copytree(base, tree)
    config = tree / 'synthetic.config'
    config.write_text(good_config().replace('CONFIG_ARM64_4K_PAGES=y',
                      '# CONFIG_ARM64_4K_PAGES is not set\nCONFIG_ARM64_16K_PAGES=y'))
    rejected_without_writes('patch_va48.py', tree, f'{version} VA48 生成16K配置拒绝且零写入', '--config', config)
    if not patches_only:
        (tree / DEFCONFIG).write_text((tree / DEFCONFIG).read_text() + '\nCONFIG_ARM64_16K_PAGES=y\n')
        before = snapshot(tree)
        shared('apply', tree, version, trial / 'invalid-defconfig-evidence', True, True, success=False)
        check(snapshot(tree) == before, f'{version} 共享入口显式16K defconfig拒绝且零写入')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sources', type=Path, help='已有未修改的 VERSION/源码相对路径目录')
    parser.add_argument('--cache', type=Path, help='从固定提交下载相关文件的缓存目录')
    parser.add_argument('--report', type=Path, help='可选 JSON 结果路径')
    parser.add_argument('--patches-only', action='store_true', help='仅诊断独立补丁；正常完整验证不要加此项')
    args = parser.parse_args()
    baseline = json.loads((HERE / 'baseline-sources.json').read_text())['versions']
    with tempfile.TemporaryDirectory(prefix='va48-compatibility-') as temp:
        temporary = Path(temp)
        source = args.sources or args.cache or temporary / 'downloaded-sources'
        tasks = [(source, version, info, rel, data['sha256']) for version, info in baseline.items()
                 for rel, data in info['files'].items()]
        if args.sources:
            missing = [str(root / version / rel) for root, version, info, rel, expected in tasks
                       if not (root / version / rel).exists()]
            if missing:
                raise FileNotFoundError('本地基线缺少文件：' + ', '.join(missing))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda task: fetch(*task), tasks))
        check(True, f'{len(baseline)} 个固定提交及 {len(tasks)} 个文件的 SHA256 全部匹配')
        for version in baseline:
            check_version(version, source, temporary, args.patches_only)
    report = {'通过': True, '检查数量': len(CHECKS), '检查': CHECKS,
              '局限': ['仅相关真实源码的锚点与功能开关检查，未执行完整内核编译。',
                       '最终配置检查使用合成配置，不能代替 Kbuild 生成配置核对。',
                       '基础 r16 仅一加 ACE5 / 6.1.118 已知实机通过，其余未实机验证。']}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'全部 {len(CHECKS)} 项兼容检查通过。')


if __name__ == '__main__':
    main()
