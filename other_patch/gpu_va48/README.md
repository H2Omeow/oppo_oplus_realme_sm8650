# GPU VA48：仅扩展 KGSL TTBR0（实验性）

此补丁仅针对 SM8650 / Adreno Gen7.2.0 的 GPU 进程地址空间。**不修改 CPU 用户/内核地址空间，不修改 `arch/arm64`，不调用原有 CPU VA48 补丁器，不改变内核虚拟地址布局。** 默认关闭，尚未完成内核模块编译或实机验证。

## 为什么需要独立补丁

审查的构建器版本为 `H2Omeow/oppo_oplus_realme_sm8650@f631392a5378f7a824dd2dfb024ada81345d2639`。原有 VA48 脚本处理的是 CPU 地址空间，不会修改独立构建的 Qualcomm KGSL 驱动。

GPU 源码来自 `OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650`，目录为 `vendor/qcom/opensource/graphics-kernel`。补丁器识别 U14、Ace3Pro U14.1、V15、B16 四套完整五文件 SHA256 基线（分支名与指纹见 `patch_gpu_va48.py` 的 `SOURCE_BASELINES`），拒绝未知版本、混拼源码及重复应用。原 `kgsl_iommu.h:72-76` 将 SVM 设在 64–256 GiB，将非 SVM 设在 256–512 GiB。`gpu-only-va48.patch` 是 B16 的参考 diff；其他基线使用补丁器。

设备树 `kernel_platform/qcom/proprietary/devicetree/qcom/msm-arm-smmu-pineapple.dtsi:10` 的 `qcom,use-3-lvl-tables` 还会使厂商 SMMU 的初始页表输入宽度收窄到 39 位。KGSL 原本直接继承这个宽度，因此只放大地址常量无效。

本补丁保留该设备树属性以及原有 GPU 全局 TTBR1 页表，只单独扩展 KGSL 自己创建的 TTBR0。厂商 `drivers/iommu/arm/arm-smmu/arm-smmu-qcom.c` 的 `set_ttbr0_cfg` 分别设置 T0SZ/T1SZ，允许这种分离。这里的 GPU SMMU 寄存器与 CPU 的 `TCR_EL1` 不是同一套寄存器。

## 修改边界

补丁器仅允许修改 GPU 源码目录内的五个文件：

- `Kconfig`：新增默认关闭的 `QCOM_KGSL_GPU_VA48`。
- `Kbuild`：让外置模块构建可以显式传入该开关。
- `kgsl_iommu.h`：GPU 私有状态和地址常量。
- `kgsl_iommu.c`：硬件能力检查、GPU 私有 TTBR0 页表宽度、分配范围及高址重叠查找。
- `kgsl.c`：GPU VA48 生效后返回实际普通 GPU 地址容量，旧模式保留原值。

不修改设备树、通用 SMMU 驱动、CPU 页表、CPU VA_BITS、mmap/ASLR、模块 CRC/vermagic 检查，或原仓库的 CPU VA48 脚本。32 位兼容区、安全区和全局 GPU 映射地址保持原有定义。

只有显式开启选项、64 位/4 KiB 配置、目标 GPU、独立进程页表模式和 SMMU ID2 能力检查均通过，才进入实验路径。硬件检查先启用所需电源与时钟并检查返回值；不支持时保留原 GPU 地址范围。

## 地址分区

下列区间均为左闭右开，表示虚拟地址范围，不是物理内存容量。

| GPU 用途 | 原范围 | 实验路径 |
|---|---|---|
| CPU/GPU 同址 SVM | `[64 GiB, 256 GiB)` | `[64 GiB, min(256 TiB, 当前任务 TASK_SIZE))` |
| 独立 GPU VA | `[256 GiB, 512 GiB)` | `[128 TiB, 256 TiB)` |
| GPU 全局 TTBR1 | 原有高地址映射 | 保持原样 |

CPU 仍是 VA39 时，SVM 搜索不会越过当前 CPU 的 `TASK_SIZE`；独立 GPU 分配器使用高地址区，实际 GPU 命令执行能力须实机验证。CPU 已使用用户 VA48、内核布局保持原位置的补丁时，SVM 可在完整 CPU 用户地址上限内查找。SVM 与普通 GPU 地址区间在高半区重叠，GPU VA48 模式禁用旧的 va_hint 直接分配快路径，双方使用同一锁保护的红黑树检查冲突。已有 GPU 映射占用某个地址时，同址导入仍会失败，不能覆盖原对象。

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

## 工作流选项与自动编译

四个 SM8650 普通入口 `fastbuild_6.1.57.yml`、`fastbuild_6.1.75.yml`、`fastbuild_6.1.118.yml`、`fastbuild_6.1.141.yml` 的有效 GPU 开关为：

```text
FEATURE_GPU_VA48 = va48_enable || gpu_va48_enable
```

即开启 CPU VA48 时自动构建配套 GPU VA48；也可在 CPU VA48 关闭时单独勾选 `gpu_va48_enable`。原本始终开启 CPU VA48 的 `va48_boot.yml` 也始终构建 GPU，无需重复选项。GPU 构建或校验失败会阻断后续打包，不会忽略错误继续产出一个缺少配套模块的成功结果。默认不启用 CPU/GPU 实验功能。天玑/Mali 的 6.1.115、6.1.128、6.1.134 工作流原样保留。

### 无需用户提供 SDK

`build_gpu_va48_ci.py` 自动完成源码和依赖准备，工作流只需布尔开关；已移除 `gpu_va48_sdk_url` / `gpu_va48_sdk_sha256` 输入。不需要从设备提取镜像，也没有要求用户寻找一个不存在的官方 SDK。

源码固定信息见 [`source-refs.json`](source-refs.json)：

| 入口 | vendor kernel 基版 | GPU/模块来源 | 当前状态 |
|---|---|---|---|
| 6.1.57 | OnePlus12 U14，实际 6.1.57 | 配套 U14；缺失的 hw_fence/synx 使用固定 B16 公开依赖源码 | 实验候选 |
| 6.1.75 | SM8650 Ace3Pro U14.1，实际 6.1.75 | 配套 Ace3Pro；缺失的 hw_fence 使用固定 B16 公开依赖源码 | 同 SoC 候选，不能据此保证所有机型 ABI |
| 6.1.118 | B16 初始发布，实际 6.1.118 | 已审查 B16 GPU/模块源码 | 实验候选 |
| 6.1.141 | B16 当前固定提交，实际 6.1.141 | 配套 B16 GPU/模块源码 | 实验候选 |

当前 V15 分支的 vendor kernel 已升至 6.1.118，当前 B16 分支已升至 6.1.141；CI 检查固定提交中的 Makefile，避免按分支名误选基版。依赖补充仅复制缺失目录，不能覆盖原有 GPU。匹配公开 API 头只用于支持尝试构建，不是跨固件 ABI 兼容证明。全部机型/固件并未实机验证。

自动流程如下：

1. 检查本次 common `.config`、Image、符号表和 CPU 布局头，记录 SHA256。
2. 下载并核对固定 vendor/modules commit，在私有 vendor 工作副本和独立 `vendor-out` 准备配置。以本次 common 配置为基础合并厂商 fragment，对齐 CPU/CFI/LTO 等关键设置及完整 release。
3. 仅在临时 vendor 副本中对齐当前 common 的四个 CPU 布局头，供模块使用同一 CPU 布局。绝不将 vendor 头反向复制到 common；不构建第二个 CPU Image。
4. 原生 `KBUILD_MIXED_TREE=common/out make modules` 生成真实 vendor 符号表，再按顺序编译 synx/ipclite、hw_fence 与打补丁的 KGSL。
5. 严格检查符号冲突、强未定义导入的 CRC、架构、vermagic、实际编译开关和 Image/CPU 头前后摘要。任何失败均保存已有日志并使工作流失败。

此过程不修改 CPU VA48 补丁、common 的配置或 Image，不禁用 hw_fence/synx、不跳过 modpost、不伪造 CRC、不修改 vermagic。底层 `build_gpu_va48.py` 中的 manifest/校验函数用于内部构建验证，旧 `sdk-manifest.example.json` 不是普通用户需要准备的下载项。签名强制、私有结构、CFI/KMI、固件和用户态驱动仍可能阻止构建或加载，必须通过实际编译和设备测试确认。

本地 Linux 构建入口与 CI 相同（需匹配 Clang、make、kmod 等工具）：

```sh
python3 other_patch/gpu_va48/build_gpu_va48_ci.py \
  --common-source /path/to/common \
  --common-out /path/to/common/out \
  --kernel-version 6.1.118 \
  --output ./gpu-va48-output
```

### 模块部署与联动生效

common Image 与可选 KPM 修补完成后、AnyKernel 打包前构建 GPU。独立 `gpu-va48-6.1.*` 工件包含模块、依赖、固定源码信息、日志及验证记录。**AnyKernel ZIP 目前只安装 Image，不会替换设备实际加载的 `msm_kgsl.ko` 或 vendor_dlkm 中的依赖。** 仅勾选、编译或只刷 Image 均不意味着 GPU 地址限制已经改变。

必须按目标固件的模块位置、加载时机、依赖和签名机制部署新模块；不能在所有设备上盲写同一个分区或路径。内核模块不能靠应用启动后的普通文件覆盖来替换已加载 KGSL。设备布局未输入、模块未验证时，本仓库不提供宣称通刷的自动刷写脚本。

SVM 上限为 `min(256 TiB, TASK_SIZE)`，支持在 CPU 允许范围内尝试高位同址导入。若该地址已被普通 GPU 映射占用，同址导入会返回失败；用户态库也可能有自己的地址校验。编译通过不等于全部应用和固件的 48 位 SVM 路径已实机验证。GPU 硬件探测失败时仍走原 GPU 路径，必须看实际初始化日志确认，不能只看 CI 开关。

### 回归验证

补丁器对 U14、Ace3Pro U14.1、V15、B16 四套真实源码执行回归；构建编排测试使用模拟工具链，验证失败处理与保护边界。Python 测试和 YAML 检查均不代表实际 KGSL C 编译。测试记录和验证范围见 `test-results.txt`、`build-test-results.txt`、`workflow-test-results.txt`、`verification.json`。

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
