# 《明日方舟：终末地》下载/更新机制调查 + 省空间安装方案

> 调查日期：2026-10-06
> 目标机器：本机（C: 30 GB / D: 111.8 GB / E: 81.5 GB）
> 结论状态：**方案已验证可跑；首次安装已完成 1.26 GB 试点；剩余 56.59 GB 待装**

---

## 0. 一句话结论

**官方启动器在本机装不下（峰值 110.86 GB > D 盘可用 91.2 GB）；本文实现的流式安装器只需要 59.35 GB 峰值，能装，装完还剩约 33 GB。**

---

## 1. 磁盘现实（这是整件事的起因）

| 盘 | 总容量 | 可用 |
|---|---|---|
| C: | 30 GB | 0.89 GB |
| **D:** | 111.8 GB | **89.9 GB**（安装前 91.2） |
| E: | 81.5 GB | 22.0 GB |

| 方案 | 峰值需求 | 结果 |
|---|---|---|
| 官方启动器 | 压缩包 53.01 + 解压 57.85 = **110.86 GB** | ❌ **差 19.7 GB** |
| 本方案 | 最终 57.85 + 单文件缓冲 1.50 = **59.35 GB** | ✅ 余 ~31 GB |

---

## 2. 官方分发链：两条，彼此独立

### 通道 A — 启动器客户端包（版本 1.5.3）

- 形态：**54 卷标准 ZIP**，每卷恰好 1 GiB（`.zip.001` … `.zip.054`）
- 逻辑总长：**56,915,023,037 B (53.01 GiB)**
- 条目：**1692**（1600 文件 + 92 目录）
- 压缩后 53.01 GB → 解压后 57.96 GB
- **中央目录完整落在最后一卷内**（偏移 6446323，长度 259944）
- 覆盖**整个游戏目录**：`Endfield.exe` / `GameAssembly.dll` / `UnityPlayer.dll` / VFS 资源 / ACE / Qt5 / 各种 SDK

入口：

```
POST https://launcher.hypergryph.com/api/proxy/batch_proxy
     appcode=6LL0KJuqHBVz33WK  launcher_appcode=abYeZZ16BPluCFyT
     → proxy_rsps[get_latest_game_rsp].pkg.packs[]  (54 卷 URL，带 auth_key)
     → pkg.total_size / pkg.file_path
```

### 通道 B — 资源热更新（res `10506507-7`）

- 清单：`{path}/index_main.json` + `index_initial.json`
- 解密：**Base64 → 逐字节环状相减**，两把不同的密钥

  | part | 密钥 |
  |---|---|
  | main | `Assets/Beyond/DynamicAssets/Gameplay/UI/Fonts/` |
  | initial | `Assets/Beyond/InitialAssets/` |

- 条目：main 1094 + initial 12 = **1106**（1085 `.chk` + 21 `.blc`），**全量清单不是增量**
- 字段：`index, name, hash, size, type, md5`（`hash` 对 `.chk` 为空）
- 总量 63.21 GB
- `.chk` 从 CDN 下载后 **md5 与清单精确一致** → **无需解密/解包，纯字节搬运**
- `.blc` 首 4 字节 `03 00 00 00`（protocol=3），与公开逆向结论吻合

入口：

```
GET https://launcher.hypergryph.com/api/game/get_latest_resources
    ?appcode=..&platform=Windows&game_version=1.5&version=1.5.3&rand_str=..
    → resources[] { name, version, path }
```

---

## 3. 三个关键技术判定

### 判定 1：官方的 `total_size` 就是"两份都占"的峰值

```
压缩包总和            56,915,023,037 B  ( 53.01 GB)
解压后总和(中央目录)   62,233,634,670 B  ( 57.96 GB)
两者相加             119,148,657,707 B  (110.97 GB)
官方 Seed total_size 119,390,457,746 B  (111.19 GB)
偏差                                      0.23 GB
```

**厂商自己的接口数字证实了用户的"两倍预留空间"观察。** `total_size` 不是"安装后大小"，而是"压缩包 + 解压产物"。

### 判定 2：VFS chunk 文件名是**稳定身份**，不是内容哈希

两个 40 字节的 chunk：

```
A557940E5BE48FE54507370E4CE451E3.chk   40 B   md5=e1d41e1e7eb7b24939ff29cc4c350602
C81609E1D919399363313B5C5C607E2F.chk   40 B   md5=e1d41e1e7eb7b24939ff29cc4c350602
```

**内容完全相同，文件名不同。** 若文件名 = f(内容)，二者必须同名。

→ 文件名是**逻辑身份**，`md5` 字段才是内容版本
→ **原地覆盖永远成立** → 更新时峰值 ≈ 现有安装 + 单块缓冲
（也与索引里 `md5Name` 与 `contentMD5` 两个字段并存的设计一致）

### 判定 3：包里的路径是真实文件系统路径，不是内容寻址

```
Endfield_Data/StreamingAssets/VFS/24ED34CF/51BDC7E2DE98FD46B96F1937F19A86A4.chk
GameAssembly.dll
zlib.dll
```

结合判定 2 → **通道 A 的每个文件路径都稳定 → 原地覆盖 → 峰值 = 最终体积 + 单文件缓冲**。

---

## 4. 已建成的工具（纯标准库，零依赖）

> **注**：本节记录调查阶段的原型形态。代码后来重构为 `efd/` 包，
> 模块划分见 [附录 C](#附录-c产出文件)。功能与结论未变。

| 原型文件 | 作用 | 现在在哪 |
|---|---|---|
| `volreader.py` | **跨卷可寻址流**（~80 行）：把 54 个分卷伪装成一个 53 GiB 的 seekable 流，喂给标准库 `zipfile`。zip64 / 中央目录 / deflate / CRC32 全部由标准库负责 | `efd/volumes.py` |
| `step2_http.py` | HTTP Range 版阅读器 + 字节计数器 | 并入 `efd/volumes.py`，实验本身成为 `efd probe` |
| `plan.py` | 规划器（默认 dry-run），可选本地扫描做三分类 | `efd/planner.py` + `efd cli plan` |
| `install.py` | 首次安装器，流式解包，**从不落盘压缩包** | `efd/installer.py` + `efd cli install` |

**设计要点**：不手写 zip64/中央目录/deflate/CRC32，只造一个 `RawIOBase`，其余交给标准库。

原型阶段这三份脚本各自复制了一份 Seed 请求、`HttpVolume`、`human()` 和续传判定；
重构时合并为单一实现（详见 [附录 C](#附录-c产出文件)）。

---

## 5. 实测数据

### 5.1 正确性

| 测试 | 结果 |
|---|---|
| 离线解出 vol054 内全部条目 | **52 / 52 通过**（CRC32 由标准库校验） |
| HTTP Range 抠取中段 3 个文件 | **3 / 3 通过** |
| 真实写入 20 个文件 | **20 / 20 通过** |
| 真实写入 540 个文件（阶段 A） | **540 / 540 通过** |

### 5.2 效率

| 指标 | 数值 |
|---|---|
| 清单获取成本 | **254.11 KB**（一次 Range 请求换全部 1692 条） |
| 单文件 Range 抓取 | 3 个文件下载 3 MB（整卷需 3.2 GB）**省 99.9%** |
| 阶段 A 网络下载 | 498.25 MB / 146 次请求 |
| 阶段 A 压缩数据总量 | 497.73 MB → **浪费 ≈ 0** |
| 阶段 A 吞吐 | **5.6 MB/s**（顺序读） |
| 散点 Range 吞吐 | 2.7 MB/s 单流 / 3.3 MB/s 8 并发 |

**关键推论**：散点并发只快 22%，说明是**带宽瓶颈不是连接瓶颈** → **并发无意义，单流顺序读即可**（反而更简单、更省内存）。顺序读还能把吞吐从 2.7 提到 5.6 MB/s。

---

## 6. 阶段 C：两个最大未知的调查结果

### 6.1 反作弊 ACE —— ✅ 风险解除

腾讯游戏安全中心官方说明（[来源](https://gamesafe.qq.com/article/928.shtml)）：

> 临时删除 `C:\Program Files\AntiCheatExpert` 文件夹，并重新启动游戏（**此文件夹会被自动重装**）

- ACE 安装在 **`C:\Program Files\AntiCheatExpert`**，**不在游戏目录**
- **游戏启动时会自动安装/修复 ACE** → **我们不需要自己安装反作弊**
- 包内自带的 `ACE-Setup64.exe` 官方用途是**卸载** ACE，不是安装

⚠️ **但有个新风险**：ACE 要装到 **C 盘**，而 C 盘只剩 **0.89 GB**。ACE 解压后约 89 MB，勉强够，但游戏着色器缓存、日志也吃 C 盘，偏紧。

### 6.2 启动器状态记录 —— ⚠️ 部分未知

本机实测（只读）：

```
D:\Apps\Hypergryph Launcher\              ← 启动器已装，v1.6.0.1604
├── Launcher.exe / Uninstall.exe
├── 1.6.0\   (7z.dll, 7zg.exe, plugins, resources, translations …)
├── Cache\Config\Config
└── games\
    └── Arknights Endfield\              ← 目标目录，安装前为空

HKCU:\Software\Hypergryph\Endfield                     ← 存在但【空】（游戏未注册）
HKCU:\Software\Hypergryph\Launcher
    device_id = F12C55E4-...
HKCU:\Software\Hypergryph\Launcher\2f2b80a6...\install_path = D:\Apps\Hypergryph Launcher
HKCU:\Software\Hypergryph\Launcher\33a0a629...\{downloader_wid, wid, REG_GUID, language, …}
```

- 启动器目录自带 **`7z.dll` / `7zg.exe`** → 证实用 7-Zip 解包
- 实际下载委托给独立的 `ArknightsEndfield_Downloader_*.exe`（166 MB，**已加壳，无法静态分析**）
- 该下载器残留在 `C:\Users\libai\AppData\Local\Hypergryph\33a0a629...\tmp\downloader\download\42131218\`（可清理以腾出 C 盘空间）
- `games\Arknights Endfield` 目录已由启动器创建（空）

**未知**：启动器是否会因为文件齐全（`game_files` 完整性清单匹配）而认账，还是需要它自己的额外登记。**这一点只能在阶段 A 之后打开启动器实测。**

---

## 7. 阶段 A：试点安装结果

**已装到 `D:\Apps\Hypergryph Launcher\games\Arknights Endfield`（跳过 StreamingAssets）**

```
文件数      540
落盘占用    1.26 GB
网络下载    498.25 MB / 146 请求
耗时        231 秒  (5.6 MB/s)
峰值磁盘    ≈ 1.50 GB   （未落盘任何压缩包）
```

核对：

| 文件 | 状态 |
|---|---|
| `Endfield.exe` | ✓ 823,352 B |
| `GameAssembly.dll` | ✓ 256,677,352 B |
| `UnityPlayer.dll` | ✓ 33,069,624 B |
| `EndfieldBase.dll` | ✓ 35,787,664 B |
| `game_files`（完整性清单） | ✓ 209,952 B |
| `AntiCheatExpert\ACE-Setup64.exe` | ✓ 899,984 B |
| `Endfield_Data\app.info` / `boot.config` | ✓ |
| `Endfield_Data\StreamingAssets` | ✗ 未装（预期，56.59 GB） |

目录结构：

```
AntiCheatExpert\    93,109,524 B
CefView\           327,099,672 B
Endfield_Data\     244,874,052 B
plugins\ / resources\ / sdkresources\ / translations\ / U8Data\ / WebviewConfig\
+ 根目录 100+ 个 dll/exe
```

磁盘变化：D: 91.16 GB → **89.90 GB**

---

## 8. 剩余工作

| 项 | 量 | 估算 |
|---|---|---|
| StreamingAssets 待装 | 56.59 GB | **≈ 2.9 小时**（5.6 MB/s） |
| 装完 D 盘剩余 | — | ≈ 33.3 GB |

命令：

```powershell
python -m efd install --target "D:\Apps\Hypergryph Launcher\games\Arknights Endfield" --apply
```

---

## 9. 风险清单

| # | 风险 | 等级 | 说明 / 缓解 |
|---|---|---|---|
| 1 | ~~ACE 反作弊需自己安装~~ | ✅ **解除** | 游戏启动时自动安装到 `C:\Program Files\AntiCheatExpert` |
| 2 | 启动器是否认账 | ⚠️ **未知** | `game_files` 完整性清单已就位且字节正确（CRC32 全过），理论上能过检查；需实测 |
| 3 | **C 盘只剩 0.89 GB** | ⚠️ **高** | ACE(~89MB) + 着色器缓存 + 日志都吃 C 盘。建议立刻清理（含 166 MB 下载器残留） |
| 4 | 首次启动会走热更新 | ℹ️ 正常 | 客户端 1.5.3 资源 < 当前 hotfix `10506507-7`，会有一波增量（走通道 B，量小） |
| 5 | ToS 灰色地带 | ⚠️ 中 | 绕过了官方分发链路的下载/解压环节 |
| 6 | 接口/密钥会变 | ℹ️ 低 | appcode 与 URL 结构可能随版本变更 |
| 7 | 吞吐 5.6 MB/s | ℹ️ | 带宽瓶颈，非实现问题；官方走同一 CDN 速度相同 |
| 8 | ~~无断点续传~~ | ✅ **已解决** | `planner._looks_installed` 按「存在且尺寸相同」跳过，不产生任何网络请求。中断后直接重跑即可 |

---

## 10. 建议的下一步

1. **立刻清 C 盘**：删掉 `C:\Users\libai\AppData\Local\Hypergryph\33a0a6296a20400d503c59ac0fd6341e\tmp\downloader\download\42131218\ArknightsEndfield_Downloader_*.exe`（166 MB）
2. **打开鹰角启动器**，看它面对一个"半装"目录的反应 → 这是验证风险 #2 的最便宜方式
3. 根据结果决定是「继续补 StreamingAssets」还是「换策略」
4. ~~跑全量前给 `install.py` 补 `--resume` 与断点日志~~ → ✅ 已实现（判定逻辑在 `efd/planner.py`，CLI 与 GUI 共用）

---

## 附录 A：关键常量

```
VOL_SIZE            = 1073741824        (1 GiB/卷)
归档总长            = 56915023037       (54 卷之和)
中央目录            = 56914762995 .. 56915022939
GAME_APPCODE        = "6LL0KJuqHBVz33WK"
LAUNCHER_APPCODE    = "abYeZZ16BPluCFyT"
启动器版本          = 1.6.0.1604
游戏版本            = 1.5.3 / game_version=1.5 / rand_str=1mexHxqVLooNlufz
热更新 res_version  = initial_10506507-7_main_10506507-7
```

## 附录 B：体积分布

```
顶层（按解压体积）
   Endfield_Data            1218 个   56.93 GB   ← 98.2%
   CefView                   122 个  311.95 MB
   GameAssembly.dll            1 个  244.79 MB
   Qt5WebEngineCore.dll        1 个  108.67 MB
   AntiCheatExpert            10 个   88.80 MB
   nvngx_dlss.dll              1 个   52.24 MB
   EndfieldBase.dll            1 个   34.13 MB
   UnityPlayer.dll             1 个   31.54 MB

VFS 块
   7064D8E2 (Bundle)  42 个  39.00 GB
   55FC21C6 (Video)  627 个   6.11 GB
   24ED34CF (Audio)    7 个   4.44 GB
   A63D7E6A (IV)      63 个   3.88 GB
   F84BF5E6           26 个   1.11 GB
   775A31D1 (Json)   113 个 799.87 MB
   C3442D43           70 个 692.90 MB
   …

.chk 尺寸分布（1085 个）
   min 40 B / p50 0.89 MB / p90 72.74 MB / p99 1153.67 MB / max 1536.67 MB
```

## 附录 C：产出文件

项目根目录：`E:\Projects\EFD(EFDownloader)\`

调查阶段的 4 个平铺脚本已重构为一个包。业务逻辑（规划、续传判定、安装循环）
现在**各只有一份实现**，CLI 与 GUI 共用。

```
EFD(EFDownloader)/
├── README.md
├── pyproject.toml
├── run-gui.cmd              双击启动 GUI
├── efd/
│   ├── config.py            常量与接口参数
│   ├── util.py              格式化 / CRC32 / safe_join
│   ├── seed.py              Seed 接口客户端
│   ├── volumes.py           跨卷可寻址流（核心）
│   ├── archive.py           ZIP 归档视图
│   ├── planner.py           差集与续传判定
│   ├── installer.py         安装循环（流式）
│   ├── cli.py               命令行
│   └── gui.py               图形界面（Tkinter）
├── tests/                   66 个用例，标准库 unittest
├── data/
│   ├── package_manifest.csv 1692 条清单（派生物）
│   ├── hotfix_index_main.json / hotfix_index_initial.json
│   └── fixtures/            vol054.bin + pack_sizes.json
├── docs/
│   ├── REPORT.md            本文档
│   └── evidence/            取证样本（见该目录 README）
└── tools/export_manifest.py 从夹具重建清单 CSV
```

### 重构时删除的东西

| 文件 | 删除理由 |
|---|---|
| `volreader.py` / `plan.py` / `install.py` / `step2_http.py` / `gui.py` | 拆入 `efd/` 包。这三份脚本各自复制了 Seed 请求、`HttpVolume`、`human()` 和续传判定（同一个请求写了 3 遍，`HttpVolume` 有 3 个不同实现） |
| `main.json` | 与 `idx_main.json` SHA256 完全相同 |
| `cd.csv` | zip64 解析有 bug 的旧版；本文档原本就标注「勿用」 |
| `cd_fixed.csv` | 未删除内容，改为**重新生成** `data/package_manifest.csv`。原文件是 PowerShell `Export-Csv` 的产物，表头被套了两层引号且带 BOM，字段名成了 `"Name"` |

### 重构时修掉的两个真 bug

1. **0 字节空文件被当成目录丢弃。**
   旧判据 `if i.file_size or i.compress_size` 会把
   `Endfield_Data/Plugins/x86_64/TQM64/dump/space.txt`（真实空文件）判为目录，
   于是**旧安装器从来没创建过它**。新实现改用 `ZipInfo.is_dir()`：
   文件数 **1600 → 1601**，目录数 **92 → 91**。回归测试见
   `tests/test_archive_fixture.py::TestEmptyFileRegression`。

2. **最大单文件会撑爆内存。**
   旧实现用 `zf.read()` 一次性读入整个文件，而最大单文件 **1,610,306,268 B（1.50 GB）**——
   装到那个文件时内存峰值就等于文件大小。新实现改为分块流式拷贝
   （`installer.COPY_CHUNK = 1 MiB`），内存占用恒定。
