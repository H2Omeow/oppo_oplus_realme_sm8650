# GPU VA48：仅扩展 KGSL TTBR0（实验性）

此补丁仅针对 SM8650 / Adreno Gen7.2.0 的 GPU 进程地址空间。**不修改 CPU 用户/内核地址空间，不修改 `arch/arm64`，不调用原有 CPU VA48 补丁器，不改变内核虚拟地址布局。** 默认关闭，尚未完成内核模块编译或实机验证。

## 为什么需要独立补丁

审查的构建器版本为 `H2Omeow/oppo_oplus_realme_sm8650@f631392a5378f7a824dd2dfb024ada81345d2639`。原有 VA48 脚本处理的是 CPU 地址空间，不会修改独立构建的 Qualcomm KGSL 驱动。

GPU 源码来自 `OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650`，目录为 `vendor/qcom/opensource/graphics-kernel`。补丁器识别 U14、V15、B16 三套完整四文件 SHA256 基线（分支名与指纹见 `patch_gpu_va48.py` 的 `SOURCE_BASELINES`），拒绝未知版本、混拼源码及重复应用。原 `kgsl_iommu.h:72-76` 将 SVM 设在 64–256 GiB，将非 SVM 设在 256–512 GiB。`gpu-only-va48.patch` 是 B16 的参考 diff；其他基线使用补丁器。

设备树 `kernel_platform/qcom/proprietary/devicetree/qcom/msm-arm-smmu-pineapple.dtsi:10` 的 `qcom,use-3-lvl-tables` 还会使厂商 SMMU 的初始页表输入宽度收窄到 39 位。KGSL 原本直接继承这个宽度，因此只放大地址常量无效。

本补丁保留该设备树属性以及原有 GPU 全局 TTBR1 页表，只单独扩展 KGSL 自己创建的 TTBR0。厂商 `drivers/iommu/arm/arm-smmu/arm-smmu-qcom.c` 的 `set_ttbr0_cfg` 分别设置 T0SZ/T1SZ，允许这种分离。这里的 GPU SMMU 寄存器与 CPU 的 `TCR_EL1` 不是同一套寄存器。

## 修改边界

补丁器仅允许修改 GPU 源码目录内的四个文件：

- `Kconfig`：新增默认关闭的 `QCOM_KGSL_GPU_VA48`。
- `Kbuild`：让外置模块构建可以显式传入该开关。
- `kgsl_iommu.h`：GPU 私有状态和地址常量。
- `kgsl_iommu.c`：硬件能力检查、GPU 私有 TTBR0 页表宽度及分配范围。

不修改设备树、通用 SMMU 驱动、CPU 页表、CPU VA_BITS、mmap/ASLR、模块 CRC/vermagic 检查，或原仓库的 CPU VA48 脚本。32 位兼容区、安全区和全局 GPU 映射地址保持原有定义。

只有显式开启选项、64 位/4 KiB 配置、目标 GPU、独立进程页表模式和 SMMU ID2 能力检查均通过，才进入实验路径。硬件检查先启用所需电源与时钟并检查返回值；不支持时保留原 GPU 地址范围。

## 地址分区

下列区间均为左闭右开，表示虚拟地址范围，不是物理内存容量。

| GPU 用途 | 原范围 | 实验路径 |
|---|---|---|
| CPU/GPU 同址 SVM | `[64 GiB, 256 GiB)` | `[64 GiB, min(128 TiB, 当前任务 TASK_SIZE))` |
| 独立 GPU VA | `[256 GiB, 512 GiB)` | `[128 TiB, 256 TiB)` |
| GPU 全局 TTBR1 | 原有高地址映射 | 保持原样 |

CPU 仍是 VA39 时，SVM 搜索不会越过当前 CPU 的 `TASK_SIZE`；独立 GPU 分配器使用高地址区，实际 GPU 命令执行能力须实机验证。CPU 已使用用户 VA48、内核布局保持原位置的补丁时，SVM 可使用更大的下半区。**本分区并不承诺 CPU 全部 48 位地址都能直接作为同址 GPU 指针**，上半区被独立 GPU 分配器使用。

## 应用与构建

在匹配的厂商 GPU 源码上先检查，再应用：

```sh
python3 patch_gpu_va48.py /path/to/vendor/qcom/opensource/graphics-kernel --check
python3 patch_gpu_va48.py /path/to/vendor/qcom/opensource/graphics-kernel
```

在原本可以成功构建该设备 `msm_kgsl.ko` 的厂商模块构建流程中，显式加入：

```text
CONFIG_QCOM_KGSL_GPU_VA48=y
```

该选项只属于 GPU 模块。不要为开启它改 CPU 的 `CONFIG_ARM64_VA_BITS`，也不要删除设备树里的三级页表属性。若外置构建系统不读取此目录的 Kconfig，需要将上述参数传入实际的 KGSL Kbuild 调用；确认编译命令确实包含相应定义。

**只把此补丁放进 common Image 的构建步骤不会生效。** 本次增加独立的 GPU 模块构建入口；模块必须与目标设备、固件和本次 GKI 匹配。本目录不包含自动刷写脚本或声称可直接刷入的二进制。

## 工作流选项

四个 SM8650 普通入口 `fastbuild_6.1.57.yml`、`fastbuild_6.1.75.yml`、`fastbuild_6.1.118.yml`、`fastbuild_6.1.141.yml`，以及已有 `va48_boot.yml` 增加：

- `gpu_va48_enable`：默认关闭的 GPU 独立开关。普通入口中它与 `va48_enable` 没有联动；只开 GPU 不会开启 CPU VA48。专用 `va48_boot.yml` 原本始终开启 CPU VA48，此行为没有改变。
- `gpu_va48_sdk_url`：启用时必填，已准备且匹配当前 GKI/目标固件的 vendor SDK HTTPS 压缩包地址。
- `gpu_va48_sdk_sha256`：启用时必填，压缩包的 64 位 SHA256。

MT6989/MT6897 的 6.1.115、6.1.128、6.1.134 工作流不作修改，也不接入 Mali。工作流的 `ANDROID_VERSION=android14` 是内核 Android 基线，不能据此选择所有固件的 GPU 源码。

| 普通入口 | 已检查的 GPU 源码基线 | 适配状态 |
|---|---|---|
| 6.1.57 | U14 | 源码补丁预检通过；需要匹配 SDK |
| 6.1.75 | V15 | 源码补丁预检通过；需要匹配 SDK |
| 6.1.118 | B16 | 源码补丁预检通过；需要匹配 SDK |
| 6.1.141 | 可识别的 U14/V15/B16 源码之一 | 未找到官方 `_6.1.141` GPU 分支；必须另外提供精确匹配 SDK，不能假定 B16 可直接加载 |

这表示所有 SM8650 构建入口均提供选项，不表示全部设备/固件已经完成模块编译或实机适配。设备 SDK 的 GPU 四文件不在已知基线内时，补丁器直接拒绝。

开启后，在 common `Image` 构建及可选 KPM 修补完成、AnyKernel 打包之前执行 `build_gpu_va48.py`，通过已准备的 vendor kernel 环境混合构建真实 `msm_kgsl.ko`。evidence 记录最终待打包 Image 的前后 SHA256；SDK 的预先匹配使用配置、符号表及布局头，不依赖会因时间戳或 KPM 改变的 Image 摘要。独立工件 `gpu-va48-6.1.*` 保存模块、构建日志及 evidence；失败时保存已有诊断，工作流失败。**AnyKernel ZIP 仍只安装 Image，不会安装 GPU 模块。只刷 ZIP 不会启用此 GPU 功能。** 模块部署需按设备原有方式另行处理。

### 准备 SDK

本仓库没有预制的通用 vendor SDK，helper 不会从零准备 vendor kernel。SDK 必须来自可信、原本可以成功构建目标设备 KGSL 的环境，包含完整 vendor kernel 源码、已配置的独立 vendor out、GPU 源码及 hw_fence/synx 等依赖符号表。归档会执行其中的内核构建脚本；URL 应指向自己核验过的公开归档。支持 ZIP 和 Python tarfile 可读取的 tar（建议 `.tar.gz`）；不允许链接、特殊文件、越界路径或重复路径。

归档根目录必须直接包含 `gpu-va48-sdk.json`，格式见 [`sdk-manifest.example.json`](sdk-manifest.example.json)：

- `sdk_format_version` 为 `1`；`kernel_version` 与所选入口完全相同。
- `common_out_sha256` 绑定本次 common out 的 `.config`、`vmlinux.symvers`、`System.map`。应从完全相同源码、选项和工具链的构建取得这些文件；不是任意同版本号内核。占位字符串必须换成真实 SHA256。`Image` 不作预先身份匹配，但必须存在且在 GPU 构建前后逐字不变。
- `common_source_sha256` 绑定当前 common 的 `arch/arm64/include/asm/{memory.h,page.h,processor.h,pgtable-hwdef.h}` 四个布局头。示例文件打包时应重命名为根目录的 `gpu-va48-sdk.json`。
- `vendor_kernel`、`vendor_out`、`gpu_source` 为 SDK 内相对路径，禁止指向 SDK 外。
- vendor out 必须已经有 `.config`、generated 头和非空 `Module.symvers`；所有 `dependency_symvers` 也必须真实存在。
- vendor 与当前 common 的 CPU 配置、关键 ARM64 地址布局头必须匹配。仅把 vendor `.config` 改成 VA48 不能匹配 r16 的布局常量；不匹配直接失败，helper 不覆盖头文件。

本地调用与工作流相同：

```sh
python3 other_patch/gpu_va48/build_gpu_va48.py \
  --common-source /path/to/common \
  --common-out /path/to/common/out \
  --sdk-url https://your-host.example/prepared-sdk.tar.gz \
  --sdk-sha256 ACTUAL_64_HEX_SHA256 \
  --kernel-version 6.1.118 \
  --output ./gpu-va48-output
```

helper 在 SDK 副本内打补丁并重编译 GPU；不把 vendor 头复制进 common，不禁用 hw_fence/synx，不忽略 modpost 错误，不放宽 CRC/vermagic。它检查实际 GPU object 编译定义、AArch64 模块、导入符号与 CRC/vermagic，并保护 common 输出及 CPU 布局头的前后 SHA256。工作流额外安装 `kmod`；本地还需 `make`、匹配 Clang 工具链、`llvm-nm`、`modinfo`、`modprobe`。helper 不自动签名；`CONFIG_MODULE_SIG_FORCE=y` 会在构建前拒绝，需要另行准备受信任签名流程。CFI/KMI、固件和用户态驱动兼容性仍需目标环境与实机确认。

### 本地回归

```sh
python3 test_patch_gpu_va48.py \
  --source /path/to/u14/graphics-kernel \
  --source /path/to/v15/graphics-kernel \
  --source /path/to/b16/graphics-kernel \
  --cpu-patch /path/to/builder/other_patch/va48/patch_va48.py
python3 test_build_gpu_va48.py --source /path/to/b16/graphics-kernel
```

补丁器三基线回归通过 30 项；构建 helper 通过 21 项离线测试，使用临时 SDK 与模拟工具链，检查构建编排和失败处理，不是实际 KGSL C 编译。五个工作流的 YAML、条件、步骤顺序和 CPU 行为保持检查也已通过。记录见 `test-results.txt`、`build-test-results.txt`、`workflow-test-results.txt`、`verification.json`。

## 验证与限制

补丁器的源码锚点与单元测试只能验证补丁适用性、修改范围及地址计算，不能代替内核模块编译和硬件验证。实机至少应确认：

1. 实际加载的是新 GPU 模块，选项生效；初始化日志显示 GPU TTBR0 为 48 位且 TTBR1 保持原配置。
2. 高于 512 GiB 的 GPU 地址能完成实际 GPU 写入、读回和命令提交，而不只是 ioctl 返回成功。
3. SVM 在 CPU 允许范围内工作；32 位应用、FORCE_32BIT、安全内容和 GPU 全局映射正常。
4. 多进程切换、LPAC、休眠恢复和 GPU 故障恢复没有新增页表错误。
5. CPU `Image`、原 CPU VA48 补丁及 ARM64 配置与测试前相同。

当前版本为源码实验方案，尚无 Adreno 750 全部高地址命令路径、固件行为或性能收益的实机结论。

## 源码依据

- [构建器固定版本](https://github.com/H2Omeow/oppo_oplus_realme_sm8650/tree/f631392a5378f7a824dd2dfb024ada81345d2639)
- [KGSL 地址分区](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650/blob/oneplus/sm8650_b_16.0.0_oneplus12/vendor/qcom/opensource/graphics-kernel/kgsl_iommu.h#L72-L76)
- [KGSL 私有页表配置](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650/blob/oneplus/sm8650_b_16.0.0_oneplus12/vendor/qcom/opensource/graphics-kernel/kgsl_iommu.c#L1384-L1406)
- [厂商 SMMU 的 TTBR0 独立配置](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8650/blob/oneplus/sm8650_b_16.0.0_oneplus12/drivers/iommu/arm/arm-smmu/arm-smmu-qcom.c#L429-L468)
- [GPU 设备树原配置](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650/blob/oneplus/sm8650_b_16.0.0_oneplus12/kernel_platform/qcom/proprietary/devicetree/qcom/msm-arm-smmu-pineapple.dtsi#L4-L10)

审查日期：2026-10-26（Asia/Shanghai）。
