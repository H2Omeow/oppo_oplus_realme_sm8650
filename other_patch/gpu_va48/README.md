# GPU VA48：仅扩展 KGSL TTBR0（实验性）

此补丁仅针对 SM8650 / Adreno Gen7.2.0 的 GPU 进程地址空间。**不修改 CPU 用户/内核地址空间，不修改 `arch/arm64`，不调用原有 CPU VA48 补丁器，不改变内核虚拟地址布局。** 默认关闭，尚未完成内核模块编译或实机验证。

## 为什么需要独立补丁

审查的构建器版本为 `H2Omeow/oppo_oplus_realme_sm8650@f631392a5378f7a824dd2dfb024ada81345d2639`。原有 VA48 脚本处理的是 CPU 地址空间，不会修改独立构建的 Qualcomm KGSL 驱动。

GPU 源码基于 `OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650` 的 `oneplus/sm8650_b_16.0.0_oneplus12` 分支，目录为 `vendor/qcom/opensource/graphics-kernel`。它的 `kgsl_iommu.h:72-76` 仍将 SVM 设在 64–256 GiB，将非 SVM 设在 256–512 GiB。

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

CPU 仍是 VA39 时，SVM 搜索不会越过当前 CPU 的 `TASK_SIZE`；GPU 独立地址仍可在高地址区工作。CPU 已使用用户 VA48、内核布局保持原位置的补丁时，SVM 可使用更大的下半区。**本分区并不承诺 CPU 全部 48 位地址都能直接作为同址 GPU 指针**，上半区被独立 GPU 分配器使用。

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

现有构建器主要打包 common `Image`，没有完整的设备匹配 GPU 模块构建和替换流程。**只把此补丁放进 common Image 的构建步骤不会生效。** 应使用与现有内核/固件匹配的厂商源码、配置和模块部署方式，替换 GPU 模块，并保留原模块用于回退。本目录不包含刷写脚本或声称可直接刷入的二进制。

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
