#!/usr/bin/env python3
"""Offline pipeline tests. Pass --source with an unpatched supported GPU tree."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("gpu_builder", Path(__file__).with_name("build_gpu_va48.py"))
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)
SOURCE = None
CONFIG = """CONFIG_ARM64=y
CONFIG_64BIT=y
CONFIG_ARM64_4K_PAGES=y
CONFIG_ARM64_VA_BITS=39
CONFIG_ARM64_VA_BITS_39=y
CONFIG_PGTABLE_LEVELS=3
CONFIG_PAGE_SHIFT=12
CONFIG_MODULES=y
CONFIG_MODVERSIONS=y
CONFIG_MODULE_UNLOAD=y
CONFIG_SMP=y
CONFIG_LTO_NONE=y
"""
RELEASE = '6.1.118-test'
CORE = '0x11111111\tmodule_layout\tvmlinux\tEXPORT_SYMBOL\n0x22222222\t_printk\tvmlinux\tEXPORT_SYMBOL\n'
VENDOR = ('0x33333333\tqcom_alloc_io_pgtable_ops\tqcom_io_pgtable\tEXPORT_SYMBOL_GPL\n'
          '0x44444444\tqcom_free_io_pgtable_ops\tqcom_io_pgtable\tEXPORT_SYMBOL_GPL\n')
EXTRA = '0x55555555\tqti_hw_fence_register\tmsm_hw_fence\tEXPORT_SYMBOL\n'


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding='utf-8', newline='\n')


def autoconf(config):
    result = ''
    for line in config.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            result += f'#define {key} {"1" if value == "y" else value}\n'
    return result


def tree_hashes(path):
    return {str(p.relative_to(path)): b.sha256(p) for p in path.rglob('*') if p.is_file()}


class BuildPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.common = self.base / 'common-out'
        self.common_source = self.base / 'common-source'
        self.sdk = self.base / 'original-sdk'
        self.cpu = self.base / 'cpu-patch.py'
        put(self.cpu, '# existing CPU patch; do not change\n')
        put(self.common / '.config', CONFIG)
        put(self.common / 'arch/arm64/boot/Image', b'CPU Image remains unchanged')
        put(self.common / 'vmlinux.symvers', CORE)
        put(self.common / 'System.map', 'kernel symbols\n')
        put(self.common / 'include/generated/utsrelease.h', f'#define UTS_RELEASE "{RELEASE}"\n')
        put(self.common / 'include/generated/autoconf.h', autoconf(CONFIG))
        put(self.sdk / 'vendor-kernel/Makefile', '# prepared test kernel KBUILD_MIXED_TREE support\n')
        for name in b.CPU_HEADERS:
            put(self.common_source / name, '#define TEST_CPU_LAYOUT 39\n')
            put(self.sdk / 'vendor-kernel' / name, '#define TEST_CPU_LAYOUT 39\n')
        vendor_cfg = CONFIG + 'CONFIG_ARM_SMMU=y\nCONFIG_ARCH_PINEAPPLE=y\n'
        put(self.sdk / 'vendor-out/.config', vendor_cfg)
        put(self.sdk / 'vendor-out/include/generated/autoconf.h', autoconf(vendor_cfg))
        put(self.sdk / 'vendor-out/include/generated/utsrelease.h', f'#define UTS_RELEASE "{RELEASE}"\n')
        put(self.sdk / 'vendor-out/Module.symvers', CORE + VENDOR)
        put(self.sdk / 'deps/Module.symvers', EXTRA)
        shutil.copytree(SOURCE, self.sdk / 'graphics-kernel')
        put(self.sdk / 'graphics-kernel/msm_kgsl.ko', b'stale module must be removed')
        put(self.sdk / 'graphics-kernel/kgsl_iommu.o', b'stale object must be removed')
        put(self.sdk / 'graphics-kernel/.kgsl_iommu.o.cmd', 'stale command\n')
        self.manifest = {
            'sdk_format_version': 1, 'kernel_version': '6.1.118',
            'common_out_sha256': {p: b.sha256(self.common / p) for p in b.SDK_BIND_FILES},
            'common_source_sha256': {p: b.sha256(self.common_source / p) for p in b.CPU_HEADERS},
            'vendor_kernel': 'vendor-kernel', 'vendor_out': 'vendor-out',
            'gpu_source': 'graphics-kernel', 'dependency_symvers': ['vendor-out/Module.symvers', 'deps/Module.symvers'],
        }
        self.save_manifest()
        self.calls = []
        self.counter = 0
        self.original_common = tree_hashes(self.common)
        self.original_cpu = b.sha256(self.cpu)

    def save_manifest(self):
        put(self.sdk / 'gpu-va48-sdk.json', json.dumps(self.manifest))

    def args(self):
        self.counter += 1
        return SimpleNamespace(common_out=self.common, common_source=self.common_source, output=self.base / f'output-{self.counter}',
            sdk_dir=self.sdk, sdk_url=None, sdk_sha256=None, kernel_version='6.1.118', cpu_patch=self.cpu)

    def runner(self, mode='success'):
        def run(command, **kwargs):
            self.calls.append(command)
            if command[0] == 'make':
                for variable in b.INJECTED_ENV:
                    self.assertNotIn(variable, kwargs['env'])
                self.assertIn('KBUILD_MODPOST_WARN=', command)
                self.assertIn('KBUILD_MODPOST_NOFINAL=', command)
                self.assertNotIn('KBUILD_MODPOST_WARN=0', command)
                self.assertIn('CONFIG_QCOM_KGSL_GPU_VA48=y', command)
                gpu = Path(next(p[2:] for p in command if p.startswith('M=')))
                vo = Path(next(p[2:] for p in command if p.startswith('O=')))
                self.assertFalse((gpu / 'msm_kgsl.ko').exists())
                self.assertFalse((gpu / 'kgsl_iommu.o').exists())
                self.assertIn('module_layout', (vo / 'Module.symvers').read_text())
                self.assertIn('qcom_alloc_io_pgtable_ops', (vo / 'Module.symvers').read_text())
                self.assertIn('pt->info.cfg.ias = 48', (gpu / 'kgsl_iommu.c').read_text())
                if mode == 'make_fails':
                    kwargs['stdout'].write('ERROR: unresolved vendor symbol\n')
                    return subprocess.CompletedProcess(command, 2)
                header = bytearray(64)
                header[:7] = b'\x7fELF\x02\x01\x01'
                struct.pack_into('<HHI', header, 16, 1, 183 if mode != 'bad_elf' else 62, 1)
                implementation = b'\0GPU VA48: TTBR0 IAS=\0GPU VA48: powered SMMU\0'
                put(gpu / 'msm_kgsl.ko', bytes(header) + (implementation if mode != 'missing_feature' else b''))
                put(gpu / 'kgsl_iommu.o', b'new object')
                put(gpu / '.kgsl_iommu.o.cmd', 'clang -DCONFIG_QCOM_KGSL_GPU_VA48=1 -c kgsl_iommu.c\n')
                if mode == 'vendor_drift':
                    put(vo / '.config', CONFIG.replace('VA_BITS=39', 'VA_BITS=48'))
                if mode == 'cpu_drift':
                    put(self.common / 'arch/arm64/boot/Image', b'changed CPU artifact')
                if mode == 'warning':
                    kwargs['stdout'].write('WARNING: modpost: "unknown_symbol" [msm_kgsl.ko] undefined!\n')
                return subprocess.CompletedProcess(command, 0)
            if command[0] == 'modinfo':
                release = RELEASE if mode != 'bad_vermagic' else '6.1.999-wrong'
                return subprocess.CompletedProcess(command, 0, f'{release} SMP mod_unload modversions aarch64\n')
            if command[0] == 'modprobe':
                result = '0x11111111 module_layout\n0x33333333 qcom_alloc_io_pgtable_ops\n0x44444444 qcom_free_io_pgtable_ops\n'
                if mode != 'missing_crc':
                    result += '0x55555555 qti_hw_fence_register\n'
                if mode == 'bad_crc':
                    result = result.replace('0x11111111', '0xdeadbeef')
                return subprocess.CompletedProcess(command, 0, result)
            if command[0] == 'llvm-nm':
                return subprocess.CompletedProcess(command, 0, 'qcom_alloc_io_pgtable_ops U\nqcom_free_io_pgtable_ops U\nqti_hw_fence_register U\noptional_weak w\n')
            self.fail(f'unexpected command: {command}')
        return run

    def assert_failure(self, args, pattern, mode='success'):
        with self.assertRaisesRegex(Exception, pattern):
            b.build(args, runner=self.runner(mode))
        evidence = json.loads((args.output / 'evidence.json').read_text())
        self.assertEqual(evidence['status'], 'failed')
        self.assertFalse((args.output / 'msm_kgsl.ko').exists())
        self.assertTrue((args.output / 'build.log').exists())

    def test_complete_pipeline_and_read_only_cpu_and_original_sdk(self):
        sdk_before = tree_hashes(self.sdk)
        with mock.patch.dict(os.environ, {'MAKEFLAGS': '-i', 'GNUMAKEFLAGS': '-i', 'KBUILD_MODPOST_WARN': '1'}):
            args = self.args()
            evidence = b.build(args, runner=self.runner())
        self.assertEqual(evidence['status'], 'success')
        self.assertTrue(evidence['compiled'])
        self.assertEqual([c[0] for c in self.calls], ['make', 'modinfo', 'modprobe', 'llvm-nm'])
        self.assertEqual(tree_hashes(self.sdk), sdk_before)
        self.assertEqual(tree_hashes(self.common), self.original_common)
        self.assertEqual(b.sha256(self.cpu), self.original_cpu)
        self.assertTrue((args.output / 'msm_kgsl.ko').is_file())

    def test_wrong_binding_never_runs_make(self):
        self.manifest['common_out_sha256']['.config'] = '0' * 64
        self.save_manifest()
        self.assert_failure(self.args(), 'not bound')
        self.assertEqual(self.calls, [])

    def test_prior_image_difference_allows_sdk_reuse_but_image_stays_protected(self):
        image = self.common / 'arch/arm64/boot/Image'
        old_image_hash = b.sha256(image)
        put(image, b'new CPU Image with changed timestamp or KPM payload')
        current_image_hash = b.sha256(image)
        self.assertNotEqual(old_image_hash, current_image_hash)
        self.assertNotIn('arch/arm64/boot/Image', self.manifest['common_out_sha256'])
        args = self.args()
        result = b.build(args, runner=self.runner())
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['sdk_bind_sha256'], self.manifest['common_out_sha256'])
        self.assertEqual(result['common_out_sha256_before']['arch/arm64/boot/Image'], current_image_hash)
        self.assertEqual(result['common_out_sha256_after']['arch/arm64/boot/Image'], current_image_hash)
        self.assertEqual(b.sha256(image), current_image_hash)

    def test_enforced_module_signing_refused_without_trusted_key(self):
        config = CONFIG + 'CONFIG_MODULE_SIG_FORCE=y\n'
        put(self.common / '.config', config)
        put(self.common / 'include/generated/autoconf.h', autoconf(config))
        self.manifest['common_out_sha256']['.config'] = b.sha256(self.common / '.config')
        self.save_manifest()
        self.assert_failure(self.args(), 'enforced module signing is unsupported')
        self.assertEqual(self.calls, [])

    def test_https_zip_hash_verified_extraction_and_full_pipeline(self):
        archive = self.base / 'prepared-sdk.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for path in self.sdk.rglob('*'):
                if path.is_file():
                    z.write(path, str(path.relative_to(self.sdk)).replace('\\', '/'))
        data = archive.read_bytes()
        class Response(io.BytesIO):
            def geturl(self):
                return 'https://example.com/sdk.zip'
        opener = SimpleNamespace(open=lambda *a, **kw: Response(data))
        args = self.args()
        args.sdk_dir = None
        args.sdk_url = 'https://example.com/sdk.zip'
        args.sdk_sha256 = hashlib.sha256(data).hexdigest()
        with mock.patch.object(b.urllib.request, 'build_opener', return_value=opener):
            result = b.build(args, runner=self.runner())
        self.assertEqual(result['sdk_mode'], 'https_sha256_archive')
        self.assertEqual(result['sdk_sha256'], args.sdk_sha256)
        self.assertTrue((args.output / 'msm_kgsl.ko').is_file())

    def test_missing_vendor_symvers_never_runs_make(self):
        (self.sdk / 'vendor-out/Module.symvers').unlink()
        self.assert_failure(self.args(), 'missing/empty SDK file')
        self.assertEqual(self.calls, [])

    def test_dependency_crc_export_type_and_namespace_conflicts_refused(self):
        for line in ('0xdeadbeef\tmodule_layout\tvendor\tEXPORT_SYMBOL\n',
                     '0x11111111\tmodule_layout\tvendor\tEXPORT_SYMBOL_GPL\n',
                     '0x11111111\tmodule_layout\tvendor\tEXPORT_SYMBOL\tOTHER_NAMESPACE\n'):
            with self.subTest(line=line):
                put(self.sdk / 'deps/Module.symvers', EXTRA + line)
                self.assert_failure(self.args(), 'conflicts')
        self.assertEqual(self.calls, [])

    def test_cpu_configuration_or_stale_generated_header_refused(self):
        cfg = self.sdk / 'vendor-out/.config'
        original = cfg.read_text()
        put(cfg, original.replace('CONFIG_PGTABLE_LEVELS=3', 'CONFIG_PGTABLE_LEVELS=4'))
        self.assert_failure(self.args(), 'configuration mismatch')
        put(cfg, original)
        put(self.sdk / 'vendor-out/include/generated/autoconf.h', autoconf(original).replace('CONFIG_ARM64_VA_BITS 39', 'CONFIG_ARM64_VA_BITS 48'))
        self.assert_failure(self.args(), 'stale vendor generated')
        self.assertEqual(self.calls, [])

    def test_conflicting_duplicate_inside_one_symbol_table_refused(self):
        put(self.sdk / 'deps/Module.symvers', EXTRA + EXTRA.rstrip() + '\tOTHER_NAMESPACE\n')
        self.assert_failure(self.args(), 'conflicting CRC/export/namespace inside')
        self.assertEqual(self.calls, [])

    def test_missing_dependency_and_unknown_gpu_source_hard_fail(self):
        dep = self.sdk / 'deps/Module.symvers'
        dep.unlink()
        self.assert_failure(self.args(), 'missing/empty SDK file')
        put(dep, EXTRA)
        c = self.sdk / 'graphics-kernel/kgsl_iommu.c'
        put(c, c.read_text() + '\n/* unaudited GPU drift */\n')
        self.assert_failure(self.args(), 'unknown source fingerprint')
        self.assertEqual(self.calls, [])

    def test_same_config_but_different_cpu_layout_header_refused(self):
        put(self.sdk / 'vendor-kernel' / b.CPU_HEADERS[0], '#define TEST_CPU_LAYOUT 48\n')
        self.assert_failure(self.args(), 'CPU layout header differs')
        self.assertEqual(self.calls, [])

    def test_make_error_or_modpost_warning_is_a_hard_failure(self):
        self.assert_failure(self.args(), 'failed with exit code', 'make_fails')
        self.assert_failure(self.args(), 'missing/undefined', 'warning')

    def test_module_arch_feature_vermagic_and_crc_validation(self):
        for mode, error in (('bad_elf', 'not an AArch64'), ('missing_feature', 'enabled GPU VA48'),
                            ('bad_vermagic', 'vermagic'), ('bad_crc', 'imported CRC'),
                            ('missing_crc', 'strong module import')):
            with self.subTest(mode=mode):
                self.assert_failure(self.args(), error, mode)

    def test_vendor_configuration_drift_after_make_refused(self):
        self.assert_failure(self.args(), 'configuration/headers changed', 'vendor_drift')

    def test_cpu_change_detected_and_success_artifact_removed(self):
        self.assert_failure(self.args(), 'protected CPU artifacts changed', 'cpu_drift')

    def test_missing_common_artifact_writes_failure_evidence(self):
        (self.common / 'System.map').unlink()
        self.assert_failure(self.args(), 'missing common-out')
        self.assertEqual(self.calls, [])

    def test_unsafe_manifest_path_refused(self):
        self.manifest['vendor_kernel'] = '../outside'
        self.save_manifest()
        self.assert_failure(self.args(), 'unsafe SDK path')
        self.assertEqual(self.calls, [])


class ArchiveSafetyTests(unittest.TestCase):
    def test_tar_path_traversal_absolute_symlink_and_hardlink_rejected_before_write(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / 'sdk.tar'
                with tarfile.open(archive, 'w') as t:
                    good = tarfile.TarInfo('good-first')
                    good.size = 1
                    t.addfile(good, io.BytesIO(b'x'))
                    bad = tarfile.TarInfo(name)
                    bad.type = kind
                    bad.linkname = '../escape'
                    t.addfile(bad)
                destination = root / 'sdk'
                with self.assertRaises(b.BuildError):
                    b.extract_sdk(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])

    def test_zip_symlink_traversal_and_case_collision_rejected(self):
        for names in (('../escape',), ('C:/drive',), ('link',), ('same', 'SAME')):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / 'sdk.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    for name in names:
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        info.external_attr = ((stat.S_IFLNK | 0o777) if name == 'link' else (stat.S_IFREG | 0o644)) << 16
                        z.writestr(info, 'data')
                with self.assertRaises(b.BuildError):
                    b.extract_sdk(archive, root / 'sdk')

    def test_https_and_full_hash_required(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'sdk'
            for url, digest in (('http://example.com/sdk.tar', '0' * 64),
                                ('https://user:password@example.com/sdk.tar', '0' * 64),
                                ('https://example.com/sdk.tar', 'short')):
                with self.subTest(url=url), self.assertRaises(b.BuildError):
                    b.download_sdk(url, digest, target)

    def test_https_download_hash_mismatch_refused(self):
        class Response(io.BytesIO):
            def geturl(self):
                return 'https://example.com/sdk.tar'
        opener = SimpleNamespace(open=lambda *a, **kw: Response(b'archive bytes'))
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(b.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(b.BuildError, 'SHA256 mismatch'):
                b.download_sdk('https://example.com/sdk.tar', '0' * 64, Path(temp) / 'sdk')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args, remaining = parser.parse_known_args()
    SOURCE = args.source.resolve()
    unittest.main(argv=[sys.argv[0], '-v', *remaining])
