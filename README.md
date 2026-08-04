# CMR_raw_dataset

CMR_raw_dataset 为跨模态检索实验提供原始图片、文本和标注数据，以及一套可直接使用的下载、校验、安装、转换和发布工具。

Git 仓库只保存脚本、配置和说明；大体积数据统一存放在 [`datasets` Release](https://github.com/diaoyong3777/CMR_raw_dataset/releases/tag/datasets)，不会写入 Git 历史。本仓库只管理上游原始图片与标注，不包含特定项目生成的特征、PKL、数据划分或实验结果。

## 快速开始

需要 Python 3.10 或更高版本。下载和安装只使用 Python 标准库，不需要额外安装依赖。

```bash
git clone https://github.com/diaoyong3777/CMR_raw_dataset.git
cd CMR_raw_dataset
python dataset.py
```

第一次使用选择：

```text
1. 下载并安装数据集（首次使用）
```

然后选择数据集和安装根目录。脚本会自动完成：

```text
下载（支持断点续传） → SHA-256 校验 → ZIP 安全检查 → 解压安装
```

也可以不进入菜单，直接运行：

```bash
python dataset.py install coco2017_mini --output ./datasets
```

安装结果位于 `./datasets/coco2017_mini/`。其他数据集只需替换命令中的名称。

## 已发布数据集

下表来自当前正式 Release；“下载大小”是所有 ZIP 的合计大小，“解压大小”是 manifest 记录的原始文件总大小。

| 数据集 | 内容 | 文件数 | 下载大小 | 解压大小 | ZIP 数量 |
|---|---|---:|---:|---:|---:|
| `coco2017_mini` | COCO 2017 小型图片-文本子集 | 34 | 4.6 MiB | 4.6 MiB | 1 |
| `mirflickr25k` | MIRFLICKR-25K 图片与标注 | 150,042 | 2.9 GiB | 2.9 GiB | 2 |
| `nuswide` | NUS-WIDE 图片与标注 | 269,909 | 6.9 GiB | 8.2 GiB | 5 |
| `coco2017` | COCO 2017 train/val 图片与标注 | 123,293 | 18.9 GiB | 19.6 GiB | 11 |

请预留大于“解压大小”的可用空间。下载过程中还会保留缓存，安装完成后可按需清理 `.dataset_cache/`。

安装后的主要内容如下，文件数量与上表一致：

```text
coco2017_mini/
├── annotations/                    # 2 个 JSON 标注文件
└── mini2017/                       # 32 张 JPG 图片

mirflickr25k/
├── mirflickr/                      # 图片、文本标签与原始说明
└── mirflickr25k_annotations_v080/ # 类别标注

nuswide/
├── ConceptsList/                   # 81 个类别名称
├── Groundtruth/                    # 多标签真值
├── ImageList/                      # 全量、训练与测试图片清单
├── NUS_WID_Tags/                  # 文本标签
├── images/                         # 269,648 张 JPG 图片
├── NUS-WIDE-urls.rar
└── NUS_WID_Low_Level_Features.rar

coco2017/
├── annotations/                    # train/val captions、instances、person keypoints
├── train2017/                      # 118,287 张 JPG 图片
└── val2017/                        # 5,000 张 JPG 图片
```

## 交互菜单

直接运行 `python dataset.py` 会显示按使用目标组织的菜单：

```text
CMR 原始数据集工具
请选择你想完成的事情：
  1. 下载并安装数据集（首次使用）
  2. 查看状态或验证已安装数据
  3. 转换文件夹或 ZIP（高级功能）
  4. 制作或上传数据包（发布者）
  0. 退出
```

- 普通使用者通常只需要功能 1 和 2；
- 已有文件夹或 ZIP 需要互转时使用功能 3；
- 只有维护 Release 时才使用功能 4。

主菜单输入 `0` 退出，子菜单输入 `0` 返回。直接回车不会意外退出；输入无效编号时会继续提示。

## 下载、更新与验证

### 下载并安装

```bash
python dataset.py install nuswide --output ./datasets
```

下载缓存保存在 `.dataset_cache/`。网络中断后重新执行同一命令，脚本会从已有字节继续下载，不需要从头开始。

如果目标数据集目录已经存在，默认停止，避免覆盖本地文件。确认需要更新时使用：

```bash
python dataset.py install nuswide --output ./datasets --force
```

`--force` 不会直接删除原目录，而是先在同一磁盘保留备份，再安装新版本。

### 检查安装结果

快速检查目录结构和文件大小：

```bash
python dataset.py verify nuswide --root ./datasets
```

逐文件重新计算 SHA-256：

```bash
python dataset.py verify nuswide --root ./datasets --deep
```

`--root` 必须与安装时的 `--output` 指向同一个安装根目录，而不是直接指向 `nuswide/` 子目录。深度校验最完整，但大型数据集需要更长时间。

### 查看当前状态

```bash
python dataset.py status --remote
```

该命令同时显示：

- 仓库附近是否存在同名原始文件夹或 ZIP；
- `release_assets/` 中是否有本地发布包；
- GitHub Release 中是否已经发布完整数据包；
- `datasets.json` 是否允许发布者上传该数据集。

## 手动下载 Release 资产

推荐使用 `dataset.py install`，因为它会自动选择全部分包并完成校验。如果必须在浏览器中手动下载，请把同一数据集的以下文件放在同一个目录：

```text
CMR_raw_dataset-<DATASET>.zip                  # 小数据集
CMR_raw_dataset-<DATASET>.part001.zip          # 大数据集分包
CMR_raw_dataset-<DATASET>.part002.zip
...
CMR_raw_dataset-<DATASET>.manifest.json
CMR_raw_dataset-<DATASET>.sha256
```

然后从该目录安装：

```bash
python dataset.py install nuswide --local-assets ./downloaded-assets --output ./datasets
```

多个 `.partNNN.zip` 是相互独立、可直接打开的普通 ZIP，不是必须依赖 `.z01/.z02` 工具的传统分卷。安装脚本会根据 manifest 把它们合并到同一个数据集目录。

Release 页面底部的 `Source code (zip)` 和 `Source code (tar.gz)` 由 GitHub 自动生成，只是本仓库的脚本源码，不是数据集。真正的数据资产均以 `CMR_raw_dataset-` 开头。

## 文件夹与 ZIP 互转

脚本支持三种等价形式：

```text
原始文件夹 ↔ 单个完整 ZIP ↔ 多个独立小 ZIP
      └──────────────────────↗
```

交互菜单选择功能 3 即可按提示转换。命令行中的目标格式如下：

| `--to` | 输出 | 适用场景 |
|---|---|---|
| `directory` | `<输出根目录>/<DATASET>/` | 直接浏览或交给其他项目使用 |
| `single-zip` | 一个完整 ZIP | 本地保存或传输 |
| `split-zip` | 多个 ZIP、manifest、SHA-256 | GitHub Release 发布 |

示例：

```bash
# 原目录 → 单个完整 ZIP
python dataset.py convert coco2017 --source ./raw_dataset/coco2017 --to single-zip --output ./converted/coco2017.zip

# 单个完整 ZIP → 多个可发布的小 ZIP
python dataset.py convert coco2017 --source ./coco2017.zip --to split-zip --output ./converted/coco2017-parts

# 多个小 ZIP → 原目录；来源也可以填写 manifest 或任意 part ZIP
python dataset.py convert coco2017 --source ./converted/coco2017-parts --to directory --output ./datasets
```

读取单 ZIP 时会直接流式处理，不会先完整解压一份副本。单个完整 ZIP 使用 ZIP64，可以大于 2 GiB，但 GitHub Release 发布仍必须使用多个小 ZIP。

目标已存在时默认停止；明确需要替换时添加 `--force`。替换过程会先保留同盘备份，新结果全部完成后才切换。

## 发布者流程

普通使用者不需要本节。发布者可以直接运行交互菜单的功能 4；它会把检查、打包、测试安装和上传组织为连续步骤。

### 1. 检查原始来源

来源可以是同名原始文件夹，也可以是顶层目录正确的原始 ZIP：

```bash
python dataset.py inspect coco2017 --source ./raw_dataset/coco2017
python dataset.py inspect coco2017 --source ./coco2017.zip
```

检查只读取文件，不生成数据包。省略 `--source` 时，脚本会在仓库的 `raw_dataset/`、仓库目录及其附近自动查找同名文件夹或 ZIP，不写死盘符或云平台路径。

### 2. 制作发布包

```bash
python dataset.py pack coco2017 --source ./coco2017.zip
```

产物写入被 Git 忽略的 `release_assets/`。脚本会根据数据大小自动选择一个 ZIP 或多个小 ZIP，并生成 manifest 与 SHA-256 文件；不需要手动判断“大包”还是“小包”。

GitHub Release 的单个资产必须小于 2 GiB，因此正式发布默认使用 1900 MiB 上限，为 ZIP 元数据和边界误差预留空间。只有测试或特殊场景才需要调整 `--part-size-mib`。

重新制作已有数据包时使用 `--force`。旧资产会先备份，只有整套新包生成成功后才安全替换，失败时自动恢复。

### 3. 本地安装回归

```bash
python dataset.py install coco2017_mini --local-assets ./release_assets --output ./test-install
python dataset.py verify coco2017_mini --root ./test-install --deep
```

### 4. 上传或继续上传

上传需要安装并登录 [GitHub CLI](https://cli.github.com/)：

```bash
gh auth login
python dataset.py upload coco2017_mini --yes
```

脚本会逐个上传资产，并在每个文件完成后核对 GitHub 返回的大小和 SHA-256。重新运行同一命令会跳过远端完全一致的资产，只上传缺失文件。

如果远端同名资产内容不同，默认停止。确认需要替换旧版本时使用：

```bash
python dataset.py upload coco2017_mini --replace --yes
```

## 添加新的数据集

扩展数据集不需要修改打包、安装或上传主流程：

1. 在 `datasets.json` 的 `datasets` 中添加数据集名称、说明、顶层目录名和上游来源；
2. 准备同名原始文件夹或 ZIP；
3. 依次运行 `inspect`、`pack` 和本地安装回归；
4. 完成上游许可与再分发条件复核后，将 `publish_enabled` 设为 `true`；
5. 运行 `upload` 发布。

`publish_enabled` 只控制本工具是否允许上传，不限制本机检查、转换、打包或使用数据集。Release 中是否真实存在完整资产，应以 `python dataset.py status --remote` 为准。

对于历史 ZIP 文件名编码不规范的数据集，可以配置 `source_zip_encoding`。例如旧版 `nuswide.zip` 中未标记 UTF-8 的文件名可按 GBK 读取；新生成的数据包统一写成跨平台可识别的 UTF-8 文件名。

## 发布包的完整性与安全性

manifest 记录来源类型、文件数量、总大小，以及每个原始文件、每个 ZIP 和整套发布资产的 SHA-256。工具在安装前后执行以下检查：

- Release 资产名称、大小和 SHA-256；
- ZIP 中是否包含目录穿越、绝对路径或符号链接；
- 是否存在重复路径、未登记文件或缺失文件；
- 每个文件的大小，深度模式下还会重新计算 SHA-256；
- 目标目录原子替换与失败恢复。

打包、转换、安装和深度校验只按约 10% 输出总体进度，不会为几十万个文件逐行刷屏。Windows、Linux 和 macOS 使用相同的数据包结构；Windows 下也兼容超过传统 260 字符限制的历史长路径。

## 仓库结构

```text
CMR_raw_dataset/
├── dataset.py          # 交互菜单与命令行工具
├── datasets.json       # 数据集、Release 与发布权限配置
├── README.md
├── tests/              # 维护工具时使用的自动回归测试
├── raw_dataset/        # 可选原始来源，Git 忽略
├── release_assets/     # 本地发布资产，Git 忽略
└── .dataset_cache/     # 可续传下载缓存，Git 忽略
```

维护脚本后运行：

```bash
python -m unittest discover -s tests -v
```

测试目录不参与普通下载或安装，但用于防止格式转换、断点续传、摘要校验、路径安全和交互菜单等功能回退，因此需要保留在 Git 仓库中。

## 数据来源与许可

各数据集仍受其上游来源的许可、使用条件和引用要求约束。本仓库提供统一的整理与传输方式，不改变数据所有权，也不替代使用者对上游条款的确认。发布或再次分发数据前，请自行核对相应数据集的最新要求。
