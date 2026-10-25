#!/usr/bin/env python3
"""Offline public-source CI tests; real native compilation is a separate CI run."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ci = load('public_gpu_ci_tests', HERE / 'build_gpu_va48_ci.py')
fixtures = load('public_gpu_fixtures', HERE / 'test_build_gpu_va48.py')
SOURCE = None


class PublicSourceBuildTests(unittest.TestCase):
    def setUp(self):
        fixtures.SOURCE = SOURCE
        self.fixture = fixtures.BuildPipelineTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base
        self.refs = {'6.1.118': {
            'kernel': {'repository': 'OnePlusOSS/android_kernel_oneplus_sm8650', 'commit': 'a' * 40},
            'modules': {'repository': 'OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650', 'commit': 'b' * 40},
            'api_header_sha256': {'include/linux/soc/qcom/msm_hw_fence.h': fixtures.hashlib.sha256(b'public API\n').hexdigest()}}}
        self.calls = []
        self.fetches = []
        self.mode = 'success'

    def args(self):
        args = self.fixture.args()
        args.jobs = 2
        return args

    def fetcher(self, spec, target, *unused):
        self.fetches.append(spec)
        if 'modules_and' in spec['repository']:
            shutil.copytree(self.fixture.sdk / 'graphics-kernel', target / 'vendor/qcom/opensource/graphics-kernel')
            for p in ('vendor/qcom/opensource/synx-kernel/Kbuild', 'vendor/qcom/opensource/mm-drivers/hw_fence/Kbuild',
                      'vendor/qcom/opensource/mm-drivers/config/kalamammdrivers.conf',
                      'vendor/qcom/opensource/mm-drivers/config/kalamammdriversconf.h'):
                fixtures.put(target / p, '# public source\n')
        else:
            shutil.copytree(self.fixture.sdk / 'vendor-kernel', target)
            version = '75' if self.mode == 'wrong_base' else '118'
            fixtures.put(target / 'Makefile', f'VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = {version}\n# KBUILD_MIXED_TREE\n')
            fixtures.put(target / 'scripts/kconfig/merge_config.sh', '# source merge script\n')
            fixtures.put(target / 'arch/arm64/configs/vendor/pineapple_GKI.config', 'CONFIG_ARM_SMMU=m\n')
            fixtures.put(target / 'include/linux/soc/qcom/msm_hw_fence.h', 'changed API\n' if self.mode == 'api_drift' else 'public API\n')

    def module(self, path, gpu=False):
        binary = bytearray(64)
        binary[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HHI', binary, 16, 1, 183, 1)
        if gpu:
            binary += b'\0GPU VA48: TTBR0 IAS=\0GPU VA48: powered SMMU\0'
        fixtures.put(path, bytes(binary))

    def runner(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(kwargs['env'].get('GIT_TERMINAL_PROMPT'), '0')
        if command[0] == 'bash':
            out = Path(command[command.index('-O') + 1])
            fixtures.put(out / '.config', fixtures.CONFIG + 'CONFIG_ARM_SMMU=m\nCONFIG_ARCH_PINEAPPLE=y\n')
            return subprocess.CompletedProcess(command, 0)
        if command[0] == 'make':
            self.assertIn('KBUILD_MODPOST_WARN=', command)
            self.assertIn('KBUILD_MODPOST_NOFINAL=', command)
            self.assertFalse(any(v == 'Image' for v in command))
            vo = Path(next(v[2:] for v in command if v.startswith('O=')))
            if command[-1] == 'olddefconfig':
                if self.mode == 'config_drift':
                    fixtures.put(vo / '.config', (vo / '.config').read_text().replace('CONFIG_ARM64_VA_BITS=39', 'CONFIG_ARM64_VA_BITS=48'))
                return subprocess.CompletedProcess(command, 0)
            if command[-1] == 'modules_prepare':
                fixtures.put(vo / 'include/generated/autoconf.h', fixtures.autoconf((vo / '.config').read_text()).replace('CONFIG_ARM_SMMU m', 'CONFIG_ARM_SMMU_MODULE 1'))
                if self.mode == 'autoconf_drift':
                    fixtures.put(vo / 'include/generated/autoconf.h', (vo / 'include/generated/autoconf.h').read_text().replace('CONFIG_ARM64_VA_BITS 39', 'CONFIG_ARM64_VA_BITS 48'))
                fixtures.put(vo / 'include/generated/utsrelease.h', f'#define UTS_RELEASE "{fixtures.RELEASE}"\n')
                return subprocess.CompletedProcess(command, 0)
            module_arg = next((v[2:] for v in command if v.startswith('M=')), None)
            if module_arg is None:
                if self.mode == 'native_failure':
                    return subprocess.CompletedProcess(command, 2)
                fixtures.put(vo / 'Module.symvers', fixtures.CORE + fixtures.VENDOR)
                self.module(vo / 'drivers/iommu/arm-smmu.ko')
                self.module(vo / 'drivers/iommu/qcom_iommu_util.ko')
            else:
                m = Path(module_arg)
                if m.name == 'synx-kernel':
                    fixtures.put(m / 'Module.symvers', '0x66666666\tsynx_register\tsynx-driver\tEXPORT_SYMBOL\n')
                    self.module(m / 'msm/synx-driver.ko')
                    if self.mode != 'missing_ipclite':
                        self.module(m / 'msm/synx/ipclite.ko')
                elif m.name == 'hw_fence':
                    fixtures.put(m / 'Module.symvers', fixtures.EXTRA)
                    self.module(m / 'msm_hw_fence.ko')
                else:
                    self.assertIn('CONFIG_QCOM_KGSL_GPU_VA48=y', command)
                    self.module(m / 'msm_kgsl.ko', gpu=True)
                    fixtures.put(m / 'kgsl_iommu.o', b'new object')
                    fixtures.put(m / '.kgsl_iommu.o.cmd', 'clang -DCONFIG_QCOM_KGSL_GPU_VA48=1 -c kgsl_iommu.c\n')
                    if self.mode == 'cpu_changed':
                        fixtures.put(self.fixture.common / 'arch/arm64/boot/Image', b'changed CPU image')
            return subprocess.CompletedProcess(command, 0)
        if command[0] == 'modinfo':
            if command[2] == 'depends':
                return subprocess.CompletedProcess(command, 0, 'arm_smmu,qcom_iommu_util,msm_hw_fence,synx_driver,ipclite\n' if Path(command[-1]).name == 'msm_kgsl.ko' else '\n')
            if self.mode == 'dependency_magic_drift' and Path(command[-1]).name == 'ipclite.ko':
                return subprocess.CompletedProcess(command, 0, f'{fixtures.RELEASE} modversions aarch64\n')
            return subprocess.CompletedProcess(command, 0, f'{fixtures.RELEASE} SMP mod_unload modversions aarch64\n')
        if command[0] == 'modprobe':
            return subprocess.CompletedProcess(command, 0, '0x11111111 module_layout\n0x33333333 qcom_alloc_io_pgtable_ops\n')
        if command[0] == 'llvm-nm':
            return subprocess.CompletedProcess(command, 0, 'qcom_alloc_io_pgtable_ops U\n')
        self.fail(f'unexpected command: {command}')

    def test_full_public_source_pipeline_and_dependency_artifacts(self):
        args = self.args()
        before = fixtures.tree_hashes(self.fixture.common)
        source_before = fixtures.tree_hashes(self.fixture.common_source)
        result = ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertEqual(result['status'], 'success')
        self.assertFalse(result['user_sdk_required'])
        self.assertEqual(fixtures.tree_hashes(self.fixture.common), before)
        self.assertEqual(fixtures.tree_hashes(self.fixture.common_source), source_before)
        self.assertEqual([s['name'] for s in result['stages']], ['merge-vendor-config', 'vendor-olddefconfig',
            'vendor-modules-prepare', 'vendor-native-modules', 'synx-modules', 'hw-fence-modules', 'kgsl-va48-module'])
        inventory = json.loads((args.output / 'module-manifest.json').read_text())
        self.assertEqual(len(inventory['modules']), 6)
        for entry in inventory['modules'].values():
            self.assertTrue((args.output / entry['path']).is_file())

    def test_real_native_stage_failure_stops_before_gpu_and_preserves_logs(self):
        self.mode = 'native_failure'
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'vendor-native-modules failed'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        evidence = json.loads((args.output / 'evidence.json').read_text())
        self.assertEqual(evidence['status'], 'failed')
        self.assertTrue((args.output / evidence['stages'][-1]['log']).is_file())
        self.assertFalse(any(v.startswith('M=') for cmd in self.calls for v in cmd))

    def test_wrong_vendor_base_cannot_be_hidden_by_kernelrelease(self):
        self.mode = 'wrong_base'
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'base version differs'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertEqual(self.calls, [])

    def test_missing_mapping_rejected_before_download(self):
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'no verified fixed'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs={})
        self.assertEqual(self.fetches, [])

    def test_vendor_dependency_api_is_bound_before_compilation(self):
        self.mode = 'api_drift'
        with self.assertRaisesRegex(ci.strict.BuildError, 'dependency API header differs'):
            ci.build(self.args(), runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertEqual(self.calls, [])

    def test_vendor_config_drift_stops_before_native_build(self):
        self.mode = 'config_drift'
        with self.assertRaisesRegex(ci.strict.BuildError, 'prepared vendor CPU configuration mismatch'):
            ci.build(self.args(), runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertFalse(any(cmd[-1] == 'modules' for cmd in self.calls))

    def test_vendor_generated_header_drift_stops_before_native_build(self):
        self.mode = 'autoconf_drift'
        with self.assertRaisesRegex(ci.strict.BuildError, 'stale vendor generated autoconf'):
            ci.build(self.args(), runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertFalse(any(cmd[-1] == 'modules' for cmd in self.calls))

    def test_dependency_output_missing_is_not_success(self):
        self.mode = 'missing_ipclite'
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'required GPU dependency modules'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertFalse((args.output / 'msm_kgsl.ko').exists())

    def test_dependency_vermagic_mismatch_is_not_success(self):
        self.mode = 'dependency_magic_drift'
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'vermagic differs from validated KGSL'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertFalse((args.output / 'msm_kgsl.ko').exists())
        self.assertFalse((args.output / 'dependencies').exists())

    def test_cpu_source_and_image_are_protected(self):
        self.mode = 'cpu_changed'
        args = self.args()
        with self.assertRaisesRegex(ci.strict.BuildError, 'protected current CPU'):
            ci.build(args, runner=self.runner, fetcher=self.fetcher, refs=self.refs)
        self.assertFalse((args.output / 'msm_kgsl.ko').exists())
        self.assertFalse((args.output / 'dependencies').exists())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args, remaining = parser.parse_known_args()
    SOURCE = args.source.resolve()
    unittest.main(argv=[sys.argv[0], '-v', *remaining])
