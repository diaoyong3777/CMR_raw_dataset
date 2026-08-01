# CMR_raw_dataset

CMR_raw_dataset 是 [CMR_Bench](https://github.com/diaoyong3777/CMR_Bench) 的原始图片与标注分发仓库。仓库本身只保存下载、校验、安装和发布工具；大体积数据通过固定的 `datasets` GitHub Release 发布，不进入 Git 历史。

CMR_Bench 已跟踪统一实验需要的 `pkl_dataset/*.pkl`，因此这里的发布包不重复包含 PKL。

## 数据集状态

| 数据集 | 用途 | 当前发布状态 |
|---|---|---|
| `coco2017_mini` | CMR_Bench 端到端快速验证 | 允许发布 |
| `mirflickr25k` | 正式实验 | 待上游再分发许可复核 |
| `nuswide` | 正式实验 | 待上游再分发许可复核 |
| `coco2017` | 正式实验 | 待上游再分发许可复核 |

数据集仍受各自上游条款约束。本仓库不会改变原始许可证，也不会把“能够下载”解释为“可以任意再分发”。正式数据集在许可复核前由工具阻止上传。

## 快速使用

要求 Python 3.10 或更高版本，只使用 Python 标准库。直接运行可打开中文交互菜单：

```bash
python dataset.py
```

也可以使用子命令。例如把 mini 安装到 Windows 的 `D:\datasets\coco2017_mini`：

```bash
python dataset.py install coco2017_mini --output D:\datasets
python dataset.py verify coco2017_mini --root D:\datasets --deep
```

Linux 路径同样由使用者指定：

```bash
python dataset.py install coco2017_mini --output /data
```

脚本不会写死 `D:\`、`/hy-tmp` 或任何云平台路径。安装完成后，将 CMR_Bench 的 `datasets.<name>.image_root` 指向实际的 `<output>/<dataset>` 即可。

## 发布包设计

每个小数据集生成一个普通 ZIP：

```text
CMR_raw_dataset-coco2017_mini.zip
CMR_raw_dataset-coco2017_mini.manifest.json
CMR_raw_dataset-coco2017_mini.sha256
```

大数据集按约 1900 MiB 拆成多个**相互独立的 ZIP**：

```text
CMR_raw_dataset-coco2017.part001.zip
CMR_raw_dataset-coco2017.part002.zip
...
CMR_raw_dataset-coco2017.manifest.json
CMR_raw_dataset-coco2017.sha256
```

每个 ZIP 都能在 Windows 中直接打开，不使用只能依赖特定解压软件的传统 `.z01/.z02` 分卷。所有 ZIP 均包含同一个顶层数据集目录，脚本会按 manifest 顺序解压到同一位置，并拒绝重复路径、目录穿越、符号链接、未登记文件和哈希不匹配。

manifest 记录：

- 原始文件数量和总大小；
- 每个文件的相对路径、大小和 SHA-256；
- 每个 ZIP 的文件名、大小和 SHA-256；
- 整套分包的 bundle SHA-256。

## 发布者流程

先只读检查原始目录：

```bash
python dataset.py inspect coco2017_mini --source <原始目录>
```

确认后打包：

```bash
python dataset.py pack coco2017_mini --source <原始目录>
```

产物写入被 Git 忽略的 `release_assets/`。脚本不会修改原始目录，也不会在磁盘上额外生成一个完整的大 ZIP；大数据集会直接写成多个独立 ZIP。

发布前可从本地资产执行一次完整安装回归：

```bash
python dataset.py install coco2017_mini --local-assets release_assets --output <测试安装根目录>
python dataset.py verify coco2017_mini --root <测试安装根目录> --deep
```

安装并登录 GitHub CLI 后上传：

```bash
gh auth login
python dataset.py upload coco2017_mini --yes
```

默认不会覆盖本地或远程同名内容。明确确认替换时分别使用 `pack --force`、`upload --replace` 或 `install --force`；安装替换会先把旧目录移动为同盘备份。

## 本地目录

```text
CMR_raw_dataset/
├── dataset.py
├── datasets.json
├── README.md
├── raw_dataset/       # 可选的本地原始数据，Git 忽略
├── release_assets/    # 本地发布产物，Git 忽略
└── .dataset_cache/    # 下载缓存，Git 忽略
```

原始数据可以位于任意磁盘或云主机，不必复制到 `raw_dataset/`；通过 `--source` 传入真实目录即可。

## 关于 GitHub 的 Source code

GitHub Release 页面底部会自动附加 `Source code (zip)` 和 `Source code (tar.gz)`，无法隐藏。它们只是本仓库脚本在 Tag 对应提交处的快照，**不是数据集**。数据集资产均以 `CMR_raw_dataset-` 开头，并配有 `.manifest.json` 和 `.sha256`。
