# EFD — 《明日方舟：终末地》省空间下载器

把官方更新包当作**一个连续的 ZIP 流**边下边解，**压缩包从不落盘**。
代价是零依赖，收益是峰值磁盘从 **110.97 GiB 降到 59.46 GiB**。

```
官方方式峰值  = 压缩包 53.01 GiB + 解压产物 57.96 GiB = 110.97 GiB
本方案峰值    = 解压产物 57.96 GiB + 单文件缓冲 1.50 GiB = 59.46 GiB
省下                                         51.51 GiB
```

这不是估算。厂商接口自己吐出的 `total_size` 是 `119,390,457,746` B（111.19 GiB），
与上面的 110.97 GiB 只差 0.23 GiB —— **`total_size` 描述的是「两份副本同时存在」的峰值，
而不是安装后的体积**。它和我们算的是同一件事。

> 为什么在乎：目标盘 `D:` 只有 91.2 GB 可用。官方路径**根本装不下**。

---

## 快速开始

需要 **Python 3.10+**（自带 tkinter 即可，无需 `pip install` 任何东西）。

```powershell
# 图形界面（或直接双击 run-gui.cmd）
python -m efd gui

# 命令行：先看计划，不写任何文件
python -m efd plan --target "D:\Apps\Hypergryph Launcher\games\Arknights Endfield"

# 确认无误后开装（不加 --apply 则永远只是预览）
python -m efd install --target "D:\...\Arknights Endfield" --apply
```

跑到一半断了？**直接重跑**。已装好的文件按「存在且尺寸相同」跳过，
不产生任何网络请求，续传是零成本的。

---

## 命令

| 命令 | 作用 |
|---|---|
| `efd plan` | 只做规划：要下多少、峰值磁盘多少。**永不写盘** |
| `efd install` | 执行安装。**不加 `--apply` 仍是 dry-run** |
| `efd probe` | 诊断：验证 CDN 的 Range 能力，并量化「精确抠出一个文件」的真实下载量 |
| `efd gui` | 图形界面 |

常用参数：

| 参数 | 说明 |
|---|---|
| `--verify-crc` | 对已存在的同尺寸文件算 CRC32 再决定跳过（**读满全盘，很慢**） |
| `--exclude-ace` | 跳过反作弊与 SDK |
| `--exclude-streaming` | 跳过 StreamingAssets（占 96% 体积，用于试探安装） |
| `--limit N` | 只处理前 N 个文件 |
| `--json FILE` | 把**完整**计划写成 JSON |
| `--prune` | 装完后删除清单里已不存在的本地多余文件 |
| `--keep-going` | 单个文件失败时继续，而不是中断 |

默认**不排除任何东西** —— 「安装游戏」的默认含义就是把归档里的东西都装上。

---

## 打包成单文件 exe

目标机器**不需要装 Python**。

```powershell
build.cmd                              # 双击也行；首次会自动建 .venv-build/ 并装 PyInstaller
# 等价于：
python tools/build_exe.py --setup       # 一次性：建构建 venv
python tools/build_exe.py --clean --verify
```

产物 **`dist/EFD.exe`，12.9 MiB**。三种用法共用同一个文件：

| 怎么用 | 结果 |
|---|---|
| 双击 | 直接进图形界面 |
| `EFD.exe plan --target "…"` | 正常命令行输出 |
| `EFD.exe gui` | 图形界面，并自动摘掉控制台窗口 |

之所以不用 PyInstaller 的 `--noconsole`：那会把 CLI 的输出一起干掉。控制台模式 +
`FreeConsole()` 能同时满足两种用法，代价是**只需要一个文件**。

`--verify` 会真的把 exe 跑起来做三项冒烟测试：`--version`、`--help` 里四个子命令齐全、
无参数启动后窗口**真的出现**（用 `EnumWindows` 查，不是看进程活着）且关闭后无残留窗口。

两个刻意的设计：

* **不碰你的全局 Python。** 优先用仓库内的 `.venv-build/`，没有就提示 `--setup`。
* **把 PyInstaller 的缓存挪出 C 盘。** 它默认写 `%LOCALAPPDATA%`；构建脚本改到 `build/` 下。

### 单文件模式的代价

onefile 每次启动都会先把自己解压到 `%TEMP%\`（约 13 MB），首次启动慢 1–2 秒。
如果目标机器 C 盘很紧，把 spec 里的 `EXE(...)` 换成分目录模式（PyInstaller 的
`--onedir`）可以避免这个解压。

---

## 工作原理

启动器的更新包是 **54 个分卷**，前 53 卷每卷恰好 1 GiB，末卷 6,706,365 B。
逻辑上等价于把 54 卷首尾相接得到的一个 53 GiB 单体 ZIP。

标准库 `zipfile` 只接受一个 seekable 流，于是这里做了一个跨卷的 `RawIOBase`
（[`efd/volumes.py`](efd/volumes.py) 的 `ConcatReader`），把 54 个 HTTP Range
阅读器伪装成一个连续文件。

```mermaid
flowchart LR
    A["54 个分卷<br/>HTTP Range"] --> B["ConcatReader<br/>伪造连续流"]
    B --> C["zipfile<br/>zip64 / inflate / CRC32"]
    C --> D["流式写 .part"]
    D -->|"os.replace"| E["目标文件"]
```

这样 **zip64 / deflate / CRC32 校验 / 中央目录解析全部由标准库负责**，
我们一行都不用写——这也是整个方案只有几百行的原因。

三个关键事实（都有实测支撑，见 [docs/REPORT.md](docs/REPORT.md)）：

1. **一次 Range 请求就能拿到全量清单。** 中央目录 259,944 B，完整落在末卷内，
   合计下载 254.11 KB。
2. **顺序读比随机读快一倍**（5.6 MB/s vs 2.7 MB/s，带宽受限）。
   所以安装按 `header_offset` 排序，线性扫描归档，**零回退读**。
   8 路并发反而只有 3.3 MB/s，所以并发是没意义的。
3. **`.chk` 从 CDN 下来与清单逐字节一致**，不需要解密或二次解包。

### 峰值公式是怎么来的

| | 压缩包 | 解压产物 | 单文件缓冲 | 峰值 |
|---|---|---|---|---|
| 官方 | 53.01 GiB | 57.96 GiB | — | **110.97 GiB** |
| 本方案 | 不落盘 | 57.96 GiB | 1.50 GiB | **59.46 GiB** |

「单文件缓冲」那一项是最大单文件的大小。安装是**流式**的：
读一块、写一块、校验、原子替换，内存占用恒定，不随文件大小增长。

---

## 项目结构

```
efd/
├── config.py      常量与接口参数（改接口先看这里）
├── util.py        格式化、CRC32、路径越界防护、输出编码
├── seed.py        Seed 接口客户端
├── volumes.py     分卷 → 连续流（核心）
├── archive.py     ZIP 归档视图
├── planner.py     差集与续传判定（只用一份）
├── installer.py   安装循环（只用一份）
├── cli.py         命令行
└── gui.py         图形界面（Tkinter）

packaging/         PyInstaller 入口与 spec
tools/             build_exe.py（构建 exe）、export_manifest.py（重建清单）
build.cmd          双击构建 exe
run-gui.cmd        双击从源码启动图形界面

tests/             标准库 unittest，116 个用例，大部分不需要网络
data/              派生物与离线夹具
docs/              调查报告与取证样本

build/ dist/       构建中间产物与 EFD.exe（已被 .gitignore 忽略）
.venv-build/       构建用 venv，同样不入库
```

**`planner.py` 和 `installer.py` 是 CLI 与 GUI 的唯一实现。** 两者都只负责
「显示」与「转发」，不重复任何业务逻辑。

---

## 数据与夹具

| 文件 | 是什么 |
|---|---|
| `data/package_manifest.csv` | 1692 条中央目录清单，**派生物**，由下一条重建 |
| `data/fixtures/vol054.bin` | 末卷（6.7 MB），中央目录完整落在其中 |
| `data/fixtures/pack_sizes.json` | 54 卷的真实尺寸，由 Seed 接口导出 |
| `data/hotfix_index_*.json` | 资源热更新清单（通道 B，见「已知限制」） |

只有末卷也能解析出**完整清单**，因为 `MissingVolume` 会提供正确的尺寸占位——
偏移量全部成立。这既是测试夹具，也是断网时重建清单的手段。

```powershell
python -m unittest discover -s tests -t .   # 跑测试
python tools/export_manifest.py             # 重建清单 CSV
```

---

## 已知限制与风险

- **接口不是公开 API。** Seed 地址与 appcode 都是从启动器自身流量里取的，
  服务端随时可能改。改动时先看 [`efd/config.py`](efd/config.py)。
- **只实现了通道 A**（启动器全量客户端包）。通道 B（资源热更新，
  `index_main.json` / `index_initial.json` 的逐字节循环减法解密）已经**摸清但没有实现**。
- **反作弊（ACE）不在本工具职责内。** 它装到 `C:\Program Files\AntiCheatExpert`，
  且在游戏启动时会被自动重装；解压它的文件并不等于完成安装。如果 C 盘紧张，
  这一点值得注意。
- **未验证 `--prune` 在真实残缺安装上的表现。** 它会删掉清单里没有的本地文件，
  请先用 `efd plan --stale` 看清楚会删什么。
- **仅 Windows 验证过。** 代码本身是跨平台的，但 GUI 用了 `windll.shcore` 做高 DPI
  （已 try/except），其余部分是纯标准库。

### 关于绕过官方分发

本工具直接向官方 CDN 取包，绕过了启动器的下载流程。这处于**灰色地带**：
它没有破解、没有修改游戏内容、没有绕过任何付费或授权校验，但确实不是官方支持的安装方式。
请自行判断是否接受，并自行承担风险。

仓库目前**没有 LICENSE 文件** —— 这是有意留白的，需要作者明确选择。

---

## 开发

```powershell
python -m unittest discover -s tests -t .   # 测试（零依赖）
python -m efd plan --target . --limit 5     # 快速干跑
```

设计约束，改代码时请一并保持：

1. **零运行时依赖。** 只用标准库。测试也用 `unittest`，不用 pytest。
2. **默认不写盘。** `plan` 永不写盘；`install` 不加 `--apply` 只预览。
3. **续传判定只有一处**（`planner._looks_installed`），CLI 与 GUI 共用。
4. **安装是流式的。** 不要退化成 `zf.read()` —— 最大单文件 1.50 GB，
   那会让内存峰值等于文件大小。
5. **路径校验是显式拒绝，不是静默改写**（`util.safe_join`）。
6. **管道输出统一 UTF-8**（`util.setup_output_encoding`）。
   控制台跟随控制台代码页；管道/重定向写 UTF-8。
   不这么做的话，PowerShell 7 / CI 读到的会是 cp936 字节，整段变成 `U+FFFD`。
   这个坑咬过两次（CLI 一次、构建脚本一次），所以提成了共用函数。
