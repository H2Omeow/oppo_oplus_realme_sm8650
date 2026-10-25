# 欧加真 SM8650/MT6989/MT6897 系列通用6.1内核自动化编译脚本
[![STAR](https://img.shields.io/github/stars/cctv18/oppo_oplus_realme_sm8650?style=flat&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyByb2xlPSJpbWciIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48dGl0bGU%2BR2l0SHViPC90aXRsZT48cGF0aCBkPSJNMTIgLjI5N2MtNi42MyAwLTEyIDUuMzczLTEyIDEyIDAgNS4zMDMgMy40MzggOS44IDguMjA1IDExLjM4NS42LjExMy44Mi0uMjU4LjgyLS41NzcgMC0uMjg1LS4wMS0xLjA0LS4wMTUtMi4wNC0zLjMzOC43MjQtNC4wNDItMS42MS00LjA0Mi0xLjYxQzQuNDIyIDE4LjA3IDMuNjMzIDE3LjcgMy42MzMgMTcuN2MtMS4wODctLjc0NC4wODQtLjcyOS4wODQtLjcyOSAxLjIwNS4wODQgMS44MzggMS4yMzYgMS44MzggMS4yMzYgMS4wNyAxLjgzNSAyLjgwOSAxLjMwNSAzLjQ5NS45OTguMTA4LS43NzYuNDE3LTEuMzA1Ljc2LTEuNjA1LTIuNjY1LS4zLTUuNDY2LTEuMzMyLTUuNDY2LTUuOTMgMC0xLjMxLjQ2NS0yLjM4IDEuMjM1LTMuMjItLjEzNS0uMzAzLS41NC0xLjUyMy4xMDUtMy4xNzYgMCAwIDEuMDA1LS4zMjIgMy4zIDEuMjMuOTYtLjI2NyAxLjk4LS4zOTkgMy0uNDA1IDEuMDIuMDA2IDIuMDQuMTM4IDMgLjQwNSAyLjI4LTEuNTUyIDMuMjg1LTEuMjMgMy4yODUtMS4yMy42NDUgMS42NTMuMjQgMi44NzMuMTIgMy4xNzYuNzY1Ljg0IDEuMjMgMS45MSAxLjIzIDMuMjIgMCA0LjYxLTIuODA1IDUuNjI1LTUuNDc1IDUuOTIuNDIuMzYuODEgMS4wOTYuODEgMi4yMiAwIDEuNjA2LS4wMTUgMi44OTYtLjAxNSAzLjI4NiAwIC4zMTUuMjEuNjkuODI1LjU3QzIwLjU2NSAyMi4wOTIgMjQgMTcuNTkyIDI0IDEyLjI5N2MwLTYuNjI3LTUuMzczLTEyLTEyLTEyIiBmaWxsPSIjZmZmZmZmIj48L3BhdGg%2BPC9zdmc%2B)](https://github.com/cctv18/oppo_oplus_realme_sm8650/stargazers)
[![FORK](https://img.shields.io/github/forks/cctv18/oppo_oplus_realme_sm8650?style=flat&logo=greasyfork&color=%2394E61A)](https://github.com/cctv18/oppo_oplus_realme_sm8650/forks)
[![COOLAPK](https://img.shields.io/badge/cctv18_2-cctv18_2?style=flat&logo=android&logoColor=FF4500&label=%E9%85%B7%E5%AE%89&color=FF4500)](http://www.coolapk.com/u/22650293)
[![DISCUSSION](https://img.shields.io/badge/%E8%AE%A8%E8%AE%BA%E5%8C%BA-discussions?logo=livechat&logoColor=FFBBFF&color=3399ff)](https://github.com/cctv18/oppo_oplus_realme_sm8650/discussions)

<img alt="Endpoint Badge" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fcctv18%2Fkernel-workshop%2Frefs%2Fheads%2Fhotfix%2Fnotice.json">

##### 
一个更方便、快捷的自动化OPPO/一加/真我系列骁龙8Gen3(SM8650)/天玑9400e(MT6989)/天玑8350(MT6897)机型的通用内核编译脚本。
##### 
这个项目的初衷是解决以下问题：
- 绿厂官方摆烂，代码开源开一半，导致部分内核代码无法通过已有的配置xml正常编译，甚至没有编译配置xml；
- 官方使用的 Bazel 编译器过于不稳定且低效，容易出现各种各样莫名其妙的错误，且全网几乎找不到任何有效解决方法，对于新手极不友好；
- 由于绿厂魔改内核f2fs代码，导致欧加真机型刷入GKI内核后不清空data分区就无法正常开机。
## 本项目的主要内容(及计划)
- 提供 OKI（官方源码）/ GKI（谷歌通用内核源码）双编译模式，OKI保留官方驱动/调度，GKI兼容性更强（无需相同内核小版本即可刷入）；
- 为 GKI 移植官方内核的f2fs源码，使 GKI 内核可以和官方 OKI 内核一样，刷入后可保留数据正常开机，不需要清空data ~~（新建文件夹）~~；
- 改用 LLVM/Clang 20 进行编译，并排除了官方源码中不必要的 vendor 源码参与，大幅优化编译流程，对比原 bazel 编译器缩短了近2/3的编译时间（原版官方编译器每次约需要超过1h才能完成编译），提高了编译过程的稳定性，输出日志更便于维护调试；
- 修复官方代码部分bug/未及时更新的补丁，并计划引入风驰内核支持；
- 提供 Github Action 在线编译/shell本地编译双版本脚本。
## 已实现：
- [x] 欧加真 SM8650 通用OKI内核（基于一加12 6.1.57/6.1.75/6.1.118 官方内核源码，其他同内核版本非SM8650机型可自行测试，部分机型可完全兼容）
- [x] 欧加真 MT6989 通用OKI内核（基于一加Ace5竞速版 6.1.115 官方内核源码，其他同内核版本非MT6989机型可自行测试，部分机型可完全兼容）
- [x] 欧加真 MT6897 通用OKI内核（基于一加平板 6.1.128 官方内核源码，其他同内核版本非MT6897机型可自行测试，部分机型可完全兼容）
- [x] ReSukiSU/SukiSU Ultra/KernelSU Next/原版KernelSU多版本KSU可选
- [x] 引入独家设计的 [ccache-ECS](https://github.com/cctv18/ccache-ECS) 缓存及大量编译流程优化，编译时间可稳定在约6min *(首次编译时会拉取公共预置ccache，从第二次开始没有大量配置改动的情况下，单次编译时间约6min；距离上一次调用两周未调用后缓存会被自动清除，此时编译会自动重建缓存)*
- [x] 引入O2编译优化，改善内核运行性能
- [x] ~~可选manual/kprobes/syscall钩子模式(kprobes钩子模式下支持切换至sus su模式)~~ 由于最新版KSU已更新inline hook，故旧版manual/syscall钩子已作废
- [x] lz4 1.10.0 & zstd 1.5.7 算法更新&优化补丁(来自[@ferstar](https://github.com/ferstar), 移植by [@Xiaomichael](https://github.com/Xiaomichael))
- [x] 可选加入 BBR/Brutal 及一系列 tcp 拥塞控制算法
- [x] 三星SSG IO调度器移植（目前已知仅在一加12上会导致无法正常启动，原因尚不明确，待进一步研究修复）
- [x] 加入一些网络功能拓展配置选项（用于为ipset及需要iptables等高级网络功能内核支持的程序提供支持）
- [x] Droidspaces 容器化支持（比传统 Docker/LXC 更轻量化，便于移植的完整 Linux 环境容器实现）
- [x] 添加了对[Mountify](https://github.com/backslashxx/mountify)模块的支持
- [x] 加入Re:Kernel支持，与Freezer，NoActive等软件配合降低功耗
- [x] 加入内核防格基带保护(By [@showdo](https://github.com/showdo))，有效防止恶意格机脚本/程序对系统分区数据的破坏
## VA48 r16 与可选故障日志（现有 6.1.x 入口）

**实机验证范围：VA48 基础 r16 补丁仅由用户在一加 ACE5、6.1.118 内核上测试正常开机。其他版本、设备及本次新版屏幕日志均未实机验证。**

仓库全部 7 个 `fastbuild_6.1.*.yml` 入口现已接入独立开关，默认关闭。源码预检通过表示已知代码结构匹配，不能代替内核编译、厂商模块和实机启动验证。

| 内核入口 | 源码平台 / 基础机型 | 本次源码兼容检查 | 实机启动 |
| --- | --- | --- | --- |
| 6.1.57、6.1.75 | SM8650 / 一加12 | 两个补丁锚点通过 | 未测试 |
| 6.1.115、6.1.134 | MT6989 / 一加 ACE5 竞速版 | 两个补丁锚点通过 | 未测试 |
| 6.1.118 | SM8650 / 一加12源码 | 两个补丁锚点通过；旧实现 Actions #38 编译成功 | 基础 r16 仅一加 ACE5 已开机 |
| 6.1.128 | MT6897 / 一加平板 | 两个补丁锚点通过 | 未测试 |
| 6.1.141 | SM8650 / 一加12 | 两个补丁锚点通过 | 未测试 |

- `va48_enable`：保留成功 r16 的 14 项修改，启用用户态 VA48、内核态 VA39、4 KiB 页面、四级页表、24 位 mmap ASLR 上限及 mmap_base 越界保护。仍保留原 r16 的模块符号 CRC 放宽和 vermagic 检查；这不能保证任意厂商模块 ABI 兼容，模块必须与设备和内核版本匹配。
- `boot_log_enable`：独立启用 panic/oops 内核日志尾部、可用的 PC/LR/调用栈、重启来源和最后 initcall 提示。可用于 VA39 或 VA48。关闭时不注入日志代码、不改 panic 超时。
- `boot_log_profile`：默认 `auto`，只读取设备树 `/chosen` 中唯一、参数完整且格式支持的 `simple-framebuffer`。未发现有效缓冲区时仅输出内核日志提示，关闭屏幕绘字并保持原 panic 超时；不会自动套用一加12的物理地址。
- `oneplus12-legacy`：显式使用旧布局 `0xd5100000 / 1264×2780 / stride=5056`。名称只代表历史参数，不代表所有一加12/ACE5固件兼容，需先核对目标设备。
- `custom`：在 `boot_log_framebuffer` 填写 `base=0x...,width=...,height=...,stride=...,format=x8r8g8b8`。也支持 `a8r8g8b8`、`x8b8g8r8`、`a8b8g8r8`，只绘黑白文字。地址、尺寸、步长、溢出和总长度均有检查。参数直接作为数据传入，不作为 shell 执行。

无论选择哪种屏幕配置，运行时都要求整个缓冲区位于保留内存中。设备树展开后核对参数与保留范围，`core_initcall` 再建立持久映射；不使用任意物理地址的 `phys_to_virt`。屏幕可用后 panic 才请求停留在日志画面，厂商/硬件看门狗仍可能重启。`core_initcall` 之前、断电、显示被驱动接管或完全锁死时无法保证屏幕输出。最后 initcall 只是定位线索，不是故障根因结论。

日志补丁会让隐藏的 `FONT_SUPPORT` 默认内置，避免仅写 defconfig 后被 Kconfig 丢弃；生成配置仍核对 `FONT_SUPPORT=y` 和 `FONT_8x16=y`。屏幕使用 ASCII 字库，因此屏幕提示和原始内核日志保留英文；说明和新增解释性注释为中文。屏幕空间只容纳日志尾部，完整日志应从设备已有的 pstore/ramoops（若支持）或串口获取。

### 预检与复现

补丁器支持 `--check` 全量预检且不写源码。`configure_features.py` 统一完成所选补丁的预检、应用和 defconfig 设置，失败时恢复本入口涉及的全部源码。生成 `.config` 后检查实际 VA_BITS、页表级数、页面大小、ASLR 和日志依赖；不兼容的 KASAN、16/64 KiB 页面、重复补丁或未知源码结构会被拒绝。5.15、6.6 等其他内核系列尚未适配。

```bash
python3 other_patch/va48/patch_va48.py /path/to/common --check
python3 other_patch/va48/patch_boot_log.py /path/to/common --profile auto --check
python3 other_patch/va48/configure_features.py apply /path/to/common \
  --expected-version 6.1.118 --va48 true --boot-log true \
  --profile auto --evidence ./feature-evidence
# 在 common 内生成 out/.config 后运行：
python3 other_patch/va48/configure_features.py verify /path/to/common \
  --expected-version 6.1.118 --va48 true --boot-log true --evidence ./feature-evidence
```

构建始终使用 checkout 的同一提交中的脚本，校验指纹和脚本副本，并上传 `feature-evidence-*`（补丁预检/应用日志、实际配置、配置检查结果和输入选项）。状态与屏幕配置写入发布说明和 ZIP 注释。`va48_boot.yml` 保留为始终启用 VA48 的 6.1.118 专用入口。

`VA48 与日志兼容检查` Actions 提供固定源码提交的 7 版本回归检查，记录在 `other_patch/va48/baseline-sources.json`。本地可运行：

```bash
python3 other_patch/va48/tests/check_compatibility.py
python3 -m unittest discover -s other_patch/va48/tests -p 'test_*.py'
```

## 实验性 Adreno GPU VA48（仅 SM8650）

四个骁龙入口 6.1.57 / 6.1.75 / 6.1.118 / 6.1.141 增加 `gpu_va48_enable`。开启原有 `va48_enable` 时自动联动构建 GPU VA48，不必再勾选 GPU；只勾选 GPU 则保持原 CPU 配置。专用 `va48_boot.yml` 始终开启 CPU VA48，因此也始终构建配套 GPU。GPU 构建失败会中止打包，保存诊断。天玑/Mali 工作流没有改动。

CI 自动下载固定 commit 的公开 vendor kernel、KGSL 和依赖源码，准备独立构建目录并编译，无需填写 SDK URL、无需从手机提取镜像。版本、配置、CPU 布局头、符号 CRC 和 vermagic 都进行检查；不能把分支名直接当内核版本。6.1.75 使用同 SoC 的 Ace3Pro 公开源码，早期依赖有固定版本补充来源，均为尚未完成实机适配的实验候选。

GPU 补丁只扩展 KGSL 自有 TTBR0，不修改 CPU 内核布局、设备树或原 CPU VA48 补丁。GPU 构建过程中，当前 common 源码的 CPU 布局头与最终 Image 前后校验不变。原有 r16 仍为用户态 VA48、内核布局 VA39。

**联动构建不等于已在设备启用。** GPU 模块、依赖、日志及校验结果保存在 `gpu-va48-6.1.*` Actions 工件；AnyKernel ZIP 只安装 Image，设备仍需部署与固件匹配的 GPU 模块。只刷 ZIP、继续加载原厂 KGSL 不会改变 GPU 地址限制。本方案尚未完成真实 KGSL 构建或实机验证，也不承诺全部 48 位 CPU 指针均能同址共享。参见 [GPU VA48 文档](other_patch/gpu_va48/README.md)。

## 待实现：
- [ ] zram内置化，无需外置zram.ko挂载 ~~（有了新版 lz4&zstd 补丁真的还有必要吗）~~
- [ ] Nethunter 网卡监听模式支持
- [ ] kexec 内核热切换支持
- [ ] 欧加真 6.1 通用 GKI内核（移植一加f2fs源码，实现免清data刷入）
- 更多优化与特性移植……
##### 
##### 
##### 
## 鸣谢
- ReSukiSU：[ReSukiSU/ReSukiSU](https://github.com/ReSukiSU/ReSukiSU)
- Sukisu Ultra：[SukiSU-Ultra/SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra)
- susfs4ksu：[ShirkNeko/susfs4ksu](https://github.com/ShirkNeko/susfs4ksu)
- SukiSU内核补丁：[SukiSU-Ultra/SukiSU_patch](https://github.com/SukiSU-Ultra/SukiSU_patch)
- pershoot维护的KernelSU Next分支：[pershoot/KernelSU-Next](https://github.com/pershoot/KernelSU-Next)
- 手动钩子等补丁：[WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)
- 原版KernelSU: [tiann/KernelSU](https://github.com/tiann/KernelSU)
- 内核防格基带保护模块：[vc-teahouse/Baseband-guard](https://github.com/vc-teahouse/Baseband-guard)
- GKI 内核构建脚本：(待定)
- ~~本地化内核构建脚本（已失效）：[Suxiaoqinx/kernel_manifest_OnePlus_Sukisu_Ultra](https://github.com/Suxiaoqinx/kernel_manifest_OnePlus_Sukisu_Ultra)~~

<!-- 这是一个访客统计，用来看看我的项目主页有多少人访问过 -->
<div align="center">
  <img width="0" height="0" src="https://count.getloli.com/get/@:cctv18" />
</div>

