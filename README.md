# CMR_raw_dataset

CMR_raw_dataset 是可独立使用的原始数据集整理、转换、发布和安装工具，也为 [CMR_Bench](https://github.com/diaoyong3777/CMR_Bench) 等跨模态检索项目提供数据来源。Git 仓库只保存配置、脚本和说明；大体积数据放在固定的 `datasets` GitHub Release 中，不进入 Git 历史。

本仓库只处理上游原始数据，不包含任何特定项目生成的预处理特征或 PKL。与 CMR_Bench 配合使用时，统一实验所需的 `pkl_dataset/*.pkl` 由 CMR_Bench 仓库管理。

## 一键入口

要求 Python 3.10 或更高版本，只使用 Python 标准库。在项目根目录运行：

```bash
python dataset.py
```

菜单按使用目标组织，不需要理解内部的检查、manifest 或分包步骤：

```text
1. 下载并安装数据集（普通使用者）
2. 制作或上传数据包（发布者）
3. 转换文件夹或 ZIP
4. 数据集状态与完整性检查
0. 退出
```

其中“制作或上传数据包”会自动完成来源检查、单包/多包判断、ZIP、manifest 和 SHA-256 生成，因此不再拆成多个菜单功能。
“数据集状态与完整性检查”下面分为两个明确操作：查看原始文件、本机数据包和 GitHub 发布状态；验证已经安装的数据目录是否缺失或损坏。
主菜单只有输入 `0` 才退出，直接回车会继续等待选择；所有子菜单均可输入 `0` 返回主菜单。子功能完成、取消或报错后也会自动回到主菜单，不需要重新运行脚本。

内部流程如下：

```text
原始文件夹 / 原始 ZIP
        ↓ 只读检查
小数据 → 1 个普通 ZIP
大数据 → 多个相互独立的 ZIP
        ↓
上传/续传 GitHub Release
        ↓
下载/续下 → 校验 → 安全安装 → 验证

原目录 ↔ 单个大 ZIP ↔ 多个独立小 ZIP
             ↖──────────────↗
```

“大”或“小”无需手动选择。GitHub 要求每个 Release 资产严格小于 2 GiB，因此脚本使用 1900 MiB 作为约 2 GB 的安全值，为 ZIP 元数据和边界误差预留约 148 MiB。普通菜单不会询问这个参数；只有高级命令行转换时才可通过 `--part-size-mib` 调整。参见 [GitHub Release 官方限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)。

## 当前数据集

| 数据集 | 用途 | 上传状态 |
|---|---|---|
| `coco2017_mini` | 小型图片-文本数据集 | 已开放，已发布 |
| `mirflickr25k` | 完整数据集 | 已开放 |
| `nuswide` | 完整数据集 | 已开放 |
| `coco2017` | 完整数据集 | 已开放 |

这里的“上传状态”只表示本工具是否允许把数据包上传到本仓库的 GitHub Release，不限制数据集在本机制作、转换和使用。该状态由 `datasets.json` 中的 `publish_enabled` 配置，不是实时检测结果。当前四个数据集均已开放上传；发布者仍需自行确认并遵守各数据集的上游许可和再分发条款。

## 原始输入

脚本接受三种等价输入：

- 已解压的同名文件夹，例如 `<任意位置>/coco2017/`；
- 顶层目录正确的原始 ZIP，例如 `<任意位置>/coco2017.zip`。
- 带 manifest 和 SHA-256 的单 ZIP 或多 ZIP 发布包。

使用 ZIP 时直接流式读取、重新整理，不会先完整解压，因此不用再为解压副本预留空间。脚本会自动寻找仓库附近的同名 ZIP 或文件夹；也可以显式传入任意磁盘或 Linux 路径：

```bash
python dataset.py inspect coco2017 --source <原始 ZIP 的实际路径>
python dataset.py inspect coco2017 --source /data/coco2017.zip
```

`nuswide.zip` 中未标记 UTF-8 的历史中文文件名按配置使用 GBK 解码，输出发布包统一写成跨平台可识别的 UTF-8 文件名。

## 三种格式互转

交互菜单选择“格式转换”，即可在以下三种形式间直接转换：

```text
原目录 ↔ 单个大 ZIP ↔ 多个独立小 ZIP
原目录 ↔ 多个独立小 ZIP
```

普通使用者直接运行菜单即可。下列命令只用于批处理或自定义路径，尖括号中的内容是需要替换的路径占位符：

```bash
# 原目录 → 单个大 ZIP
python dataset.py convert coco2017 --source <原目录> --to single-zip --output <输出目录>/coco2017.zip

# 单个大 ZIP → 多个独立小 ZIP
python dataset.py convert coco2017 --source <原始 ZIP> --to split-zip --output <多 ZIP 输出目录>

# 多个小 ZIP → 原目录；来源可写发布包目录、manifest 或任意一个 part ZIP
python dataset.py convert coco2017 --source <多 ZIP 目录>/CMR_raw_dataset-coco2017.part001.zip --to directory --output <安装根目录>

# 多个小 ZIP → 单个大 ZIP
python dataset.py convert coco2017 --source <多 ZIP 目录>/CMR_raw_dataset-coco2017.manifest.json --to single-zip --output <输出目录>/coco2017-rebuilt.zip
```

`--to directory` 的 `--output` 是安装根目录，最终得到 `<output>/coco2017/`；`single-zip` 的输出是具体 `.zip` 文件；`split-zip` 的输出是保存 ZIP、manifest 和 SHA-256 的资产目录。目标已存在时默认停止，确认后使用 `--force` 安全替换。

单个大 ZIP 使用 ZIP64，不受脚本的 2 GiB 发布分包限制，但仅作为本地保存或交换格式；GitHub Release 仍使用自动生成的小 ZIP 发布包。

## 发布包结构

小数据集生成一套单包资产：

```text
CMR_raw_dataset-coco2017_mini.zip
CMR_raw_dataset-coco2017_mini.manifest.json
CMR_raw_dataset-coco2017_mini.sha256
```

大数据集直接生成多套独立 ZIP，不额外生成一个完整总包：

```text
CMR_raw_dataset-coco2017.part001.zip
CMR_raw_dataset-coco2017.part002.zip
...
CMR_raw_dataset-coco2017.manifest.json
CMR_raw_dataset-coco2017.sha256
```

这些不是 `.z01/.z02` 式传统分卷。每个 `.partNNN.zip` 都能独立打开，并包含相同的顶层数据集目录；安装脚本按 manifest 将它们合并解压到一个目录。

manifest 保存原始来源类型、文件总数和总大小，以及每个原始文件、每个 ZIP 和整套发布资产的 SHA-256。安装时会拒绝目录穿越、符号链接、重复路径、未登记文件、大小或哈希不匹配。

打包、转换、安装和深度校验按约 10% 的间隔显示总体进度，不逐个打印文件名；无论数据集包含多少文件，进度信息通常不超过 11 行。上传阶段按实际 ZIP 数量显示，例如 5 个分包只显示 5 项上传进度。

## 发布者流程

以下命令等价于交互菜单中的对应功能。

### 1. 查看状态和检查原始数据

```bash
python dataset.py status --remote
python dataset.py inspect coco2017
```

脚本会在仓库的 `raw_dataset/`、仓库同级目录和仓库附近查找同名 ZIP 或文件夹；没有找到时使用 `--source` 指定任意实际路径。

### 2. 自动整理大/小发布包

```bash
python dataset.py pack coco2017 --source <原始 ZIP 的实际路径>
```

产物写入被 Git 忽略的 `release_assets/`。默认不覆盖现有资产；确认重新生成时使用 `--force`。替换采用同盘临时备份，只有整套新资产全部就位后才删除旧资产，失败时自动恢复。

打包会新生成一套发布资产，磁盘上会暂时同时存在原始 ZIP 和新 ZIP。开始正式数据集打包前，应为 `release_assets/` 预留足够空间。

### 3. 发布前本地回归

```bash
python dataset.py install coco2017_mini --local-assets release_assets --output <测试安装根目录>
python dataset.py verify coco2017_mini --root <测试安装根目录> --deep
```

安装替换默认禁止；显式使用 `install --force` 时，会先把已有目录重命名为同盘备份，不会直接删除。

### 4. 上传或续传 Release

先安装并登录 GitHub CLI：

```bash
gh auth login
python dataset.py upload coco2017_mini --yes
```

上传是文件级续传：远程大小和 SHA-256 相同的资产自动跳过；网络中断后重新运行同一命令即可继续尚未完成的文件。每个文件上传后立即核对远程大小和摘要。远程同名内容不同或遗留旧分包时默认停止，明确确认后使用 `--replace`。

### 5. 下载、安装和验证

```bash
python dataset.py install coco2017_mini --output <安装根目录>
python dataset.py verify coco2017_mini --root <安装根目录> --deep
```

下载缓存位于 `.dataset_cache/`。连接中断会保留 `.download` 临时文件，下次按 HTTP Range 从已有字节继续；完成后依次执行资产 SHA-256、ZIP 内容和逐文件 SHA-256 校验，再原子安装到 `<output>/<dataset>`。

Linux 只需替换输出路径：

```bash
python dataset.py install coco2017_mini --output /data
```

若用于 CMR_Bench，安装完成后将其 `datasets.<name>.image_root` 指向实际的 `<output>/<dataset>`；其他项目直接使用对应的数据集目录即可。

## 本地目录

```text
CMR_raw_dataset/
├── dataset.py
├── datasets.json
├── README.md
├── tests/
├── raw_dataset/       # 可选原始数据，Git 忽略
├── release_assets/    # 本地发布资产，Git 忽略
└── .dataset_cache/    # 可续传下载缓存，Git 忽略
```

脚本不写死任何盘符、用户目录或云平台路径。交互菜单给出的默认位置根据仓库实际位置动态计算，使用者可以在每次提示时直接输入其他路径。

## GitHub 页面上的 Source code

Release 页面底部的 `Source code (zip)` 和 `Source code (tar.gz)` 由 GitHub 根据 Tag 自动生成，无法隐藏。它们只是本仓库脚本的源码快照，不是数据集。真正的数据资产均以 `CMR_raw_dataset-` 开头，并配有 `.manifest.json` 和 `.sha256`。
