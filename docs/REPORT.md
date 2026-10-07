# 《明日方舟：终末地》下载/更新机制调查 + 省空间安装方案

> 调查日期：2026-10-06
> 目标机器：本机（C: 30 GB / D: 111.8 GB / E: 81.5 GB）
> 结论状态：**方案已完整验证；1601 个文件 / 57.96 GiB 已全部装完，游戏可正常启动**
> （2026-10-07 09:36 完成；实测细节见第 11 章）

---

## 0. 一句话结论

**官方启动器在本机装不下（峰值 110.97 GiB > D 盘可用 91.2 GB）；本文实现的流式安装器只需要 59.46 GiB 峰值，能装，装完还剩约 33 GB。**

---

## 1. 磁盘现实（这是整件事的起因）

| 盘 | 总容量 | 可用 |
|---|---|---|
| C: | 30 GB | 0.89 GB |
| **D:** | 111.8 GB | **89.9 GB**（安装前 91.2） |
| E: | 81.5 GB | 22.0 GB |

| 方案 | 峰值需求 | 结果 |
|---|---|---|
| 官方启动器 | 压缩包 53.01 + 解压 57.96 = **110.97 GiB** | ❌ **差 19.8 GiB** |
| 本方案 | 最终 57.96 + 单文件缓冲 1.50 = **59.46 GiB** | ✅ 余 ~31 GB |
| **节省** | **51.51 GiB** | 全部来自「压缩包从不落盘」 |

> 第 0、1 章用的是**安装完成后复核**的数字（第 11 章实测口径，1024 进制 GiB）。
> 写作当时按中央目录初算是 57.85 / 110.86 —— 差 0.11 GiB，因为初算漏了目录条目。

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

**推论（也是 `--prune` 的坑）**：路径稳定意味着「本地有哪些路径」= 通道 A 清单 ∪ 后来新增的。
通道 B 热更新会往 `StreamingAssets/VFS` 下**新增**文件，这些新增路径当然不在通道 A 清单里 ——
但它们不是陈旧文件。所以「不在清单里」**不能**单独作为删除依据，见第 12 章与
[`README.md` 已知限制](../README.md)。

---

## 4. 已建成的工具（纯标准库，零依赖）

> **注**：本节记录调查阶段的原型形态。代码后来重构为 `efd/` 包
> （`core/` `net/` `ui/` `cli/` 四层），模块划分见 [附录 C](#附录-c产出文件)。
> 功能与结论未变。

| 原型文件 | 作用 | 现在在哪 |
|---|---|---|
| `volreader.py` | **跨卷可寻址流**（~80 行）：把 54 个分卷伪装成一个 53 GiB 的 seekable 流，喂给标准库 `zipfile`。zip64 / 中央目录 / deflate / CRC32 全部由标准库负责 | `efd/net/stream.py` |
| `step2_http.py` | HTTP Range 版阅读器 + 字节计数器 | `efd/net/http.py`（+ `efd/net/volume.py` 的分卷协议），实验本身成为 `efd probe` |
| `plan.py` | 规划器（默认 dry-run），可选本地扫描做三分类 | `efd/core/planner.py` + `efd cli plan` |
| `install.py` | 首次安装器，流式解包，**从不落盘压缩包** | `efd/core/installer.py` + `efd cli install` |

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
| 阶段 A 网络吞吐 | **2.2 MB/s**（498.25 MB / 231 s） |
| 阶段 A 落盘吞吐 | 5.5 MB/s（1.26 GB / 231 s） |
| 散点 Range 吞吐 | 2.7 MB/s 单流 / 3.3 MB/s 8 并发 |

> **口径更正（2026-10）**：这里原先写的「阶段 A 吞吐 5.6 MB/s（顺序读）」是**落盘**
> 吞吐（1.26 GB / 231 s），不是网络吞吐（498.25 MB / 231 s = 2.2 MB/s）。
> 于是当时那句「顺序读把吞吐从 2.7 提到 5.6」实际是拿**散点的网络速度**去比
> **顺序的落盘速度**，两个口径不同，不能这样比。网络侧的真实对照是
> 散点 2.7 vs 顺序约 2.2–2.7 MB/s，两者本来就在同一量级。

**推论（当时）**：散点并发只快 22% → 单流顺序读即可，不必把读取打散。

> **2026-10 补充：上面这条被读得过头了，需要区分「哪种并发」。**
>
> 它推翻的是**把顺序读打散**——多路随机 Range 拿不到好处。但**不改变读取顺序**
> 的并发（顺序预读：消费者仍按偏移线性前进，只是把后面几个块提前取回来）
> 是另一回事，实测确实有效。见 5.3。

### 5.3 顺序预读与限速（2026-10 实测）

单流每 1 MiB 一个 Range 请求，每个请求都要重新建连（含 TLS 握手），
请求与响应之间的空档全在等 RTT。预读把这段空档叠起来，读的仍是同一条顺序路径。

| jobs | 吞吐 | 相对单流 |
|---|---|---|
| 1 | 2.74 MB/s | 1.00× |
| 4 | 3.58 MB/s | 1.30× |
| **8** | **3.89 MB/s** | **1.42×** |
| 16 | 3.60 MB/s | 1.31× |

复现：`python tools/bench_prefetch.py`（同一卷、同一段、依次换 jobs，每档两轮取较快值）。

**代价（白读）**：预读只管往后取块、不看计划，跨过空洞时会取来没人要的数据。

| jobs | 白读块 | 占下载量 | 白读字节 |
|---|---|---|---|
| 2 | 53 | 0.1% | 53 MB |
| 4 | 159 | 0.3% | 159 MB |
| **8** | **371** | **0.7%** | **371 MB** |
| 16 | 795 | 1.5% | 795 MB |

复现：`python tools/bench_gaps.py`（纯计算，不联网）。上表是真实续装计划
（1601 条里 1061 条待装）；**全新安装没有空洞，白读为 0**。

另外，读取按 1 MiB 块对齐，比按条目精确累加多 **1.73 MB / 52.52 GB = 0.00%**。

**结论**：`DEFAULT_JOBS = 8` —— 速度拿满，代价不到百分之一。16 反而回落，
所以不是越多越好。`--jobs 1` 可完全关闭预读，回到纯单流。
`--limit-rate` 限速用虚拟时间槽实现，全部分卷（含预读线程）共享一个限速器。

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

HKCU:\Software\Hypergryph\Endfield                     ← 安装前为空；游戏启动后写入 189 个值（见第 11 章）
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
耗时        231 秒  （网络 2.2 MB/s，落盘 5.5 MB/s）
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

## 8. 剩余工作（本文档写作时）

> ⚠️ **本章是历史记录。** 当时 StreamingAssets 尚未安装；该部分已于 2026-10-07 09:36
> 全部装完，**本章已不再描述待办事项**。完成情况见第 11 章。

| 项 | 量 | 估算 |
|---|---|---|
| StreamingAssets 待装 | 56.59 GB | **≈ 4.1 小时**（3.89 MB/s，`--jobs 8`） |
| 同上，`--jobs 1` | 56.59 GB | ≈ 5.9 小时（2.74 MB/s） |
| 装完 D 盘剩余 | — | ≈ 33.3 GB |

> 时间按 2026-10 实测的网络吞吐算（见 5.3），**不是**按落盘吞吐——
> 瓶颈在网络，不在盘。实际耗时随链路波动，这里只是量级参考。

命令：

```powershell
python -m efd install --target "D:\Apps\Hypergryph Launcher\games\Arknights Endfield" --apply
```

---

## 9. 风险清单

| # | 风险 | 等级 | 说明 / 缓解 |
|---|---|---|---|
| 1 | ~~ACE 反作弊需自己安装~~ | ✅ **解除** | 游戏启动时自动安装到 `C:\Program Files\AntiCheatExpert` |
| 2 | 启动器是否认账 | ✅ **已解除** | 实测：装完后打开启动器与游戏均正常，注册表写下 189 个值（见第 11 章） |
| 3 | **C 盘只剩 0.89 GB** | ⚠️ **高** | ACE(~89MB) + 着色器缓存 + 日志都吃 C 盘。建议立刻清理（含 166 MB 下载器残留） |
| 4 | 首次启动会走热更新 | ℹ️ 正常 | 客户端 1.5.3 资源 < 当前 hotfix `10506507-7`，会有一波增量（走通道 B，量小） |
| 5 | ToS 灰色地带 | ⚠️ 中 | 绕过了官方分发链路的下载/解压环节 |
| 6 | 接口/密钥会变 | ℹ️ 低 | appcode 与 URL 结构可能随版本变更 |
| 7 | 吞吐受链路限制 | ℹ️ | 约 2.7–3.9 MB/s。官方走同一 CDN，速度相同；已用顺序预读把这段链路上的单流吞吐提高约 1.4 倍（`--jobs 8`，见 5.3） |
| 8 | ~~无断点续传~~ | ✅ **已解决** | `planner._looks_installed` 按「存在且尺寸相同」跳过，不产生任何网络请求。中断后直接重跑即可 |

---

## 10. 建议的下一步

> ⚠️ **本章也是历史记录**（写于第 7 章试点之后）。第 1、2 条均已完成：C 盘已清，
> 启动器与游戏都已实测认账（第 11 章）。真正剩下的待办是通道 B 增量，见第 9 章风险 6 之外的
> `README.md` 已知限制。

1. **立刻清 C 盘**：删掉 `C:\Users\libai\AppData\Local\Hypergryph\33a0a6296a20400d503c59ac0fd6341e\tmp\downloader\download\42131218\ArknightsEndfield_Downloader_*.exe`（166 MB）
2. **打开鹰角启动器**，看它面对一个"半装"目录的反应 → 这是验证风险 #2 的最便宜方式
3. 根据结果决定是「继续补 StreamingAssets」还是「换策略」
4. ~~跑全量前给 `install.py` 补 `--resume` 与断点日志~~ → ✅ 已实现（判定逻辑在 `efd/core/planner.py`，CLI 与 GUI 共用）

---

## 11. 完整安装实测（2026-10-07）

> 本章是第 5–8 章的**实测收尾**：前面几章的数字来自试点与估算，本章是"真的从零装完一次"的记录。

### 11.1 结果对账

装在 `D:\Apps\Hypergryph Launcher\games\Arknights Endfield`，**1613 个文件 / 57.96 GiB**。

| 项 | 数 |
|---|---|
| 清单声明的文件 | 1601 |
| 磁盘上匹配 | **1601（尺寸不符 0 条）** |
| 磁盘多出 | 12（全部是游戏运行后自己写的，见 11.4） |
| 落盘占用 | 62,233,961,484 B = **57.96 GiB** |

### 11.2 三段耗时（按 mtime 还原）

整个过程被两次**机器休眠/中断**切成三段（与工具无关：System 日志显示 `LastBootUpTime = 2026/10/6 9:05:00`，期间未重启）：

| 段 | 文件 | 体积 | 耗时 | 速率 |
|---|---|---|---|---|
| 阶段 A 试点 | 540 | 1.26 GiB | ~7 min | — |
| 空档 | — | — | **265.7 min** | — |
| leg 1 | 744 | 14.22 GiB | 2.65 h | 1.52 MiB/s |
| 空档 | — | — | **103.3 min** | — |
| leg 2 | 317 | 42.47 GiB | 2.23 h | **5.41 MiB/s** |

**联网段合计：56.70 GiB / 4.88 h = 3.38 MiB/s 平均。** 1601 个文件的 mtime 跨度 11.1 h（含上述停顿）。
最后写入的是 `Endfield_Data/StreamingAssets/VFS/F84BF5E6/…`，收在 `index_main.json`（09:36:50）。

> leg 2 的 5.41 MiB/s 明显高于第 5.3 章 `--jobs 8` 的 3.89 MB/s——说明**瓶颈确实在网络链路**，
> 链路好时段速就上去，与安装器本身无关。

### 11.3 游戏确实启动过（硬证据）

| 证据 | 内容 |
|---|---|
| `HKCU:\Software\Hypergryph\Endfield` | **189 个值**，含 Unity 屏幕/音频/语言设置与 `unity.player_sessionid_*` / `player_session_count_*` |
| `U8Data\config\Launcher.meta` | 76 B，运行时生成（base64 串） |
| `CrashSightLog\CrashSight.*.log` | `app:[abfe34dd3a]`，`response code is 200`，`CLOSE LOG!` |
| 启动器**没有重新下载** | `launcher_tmp\...\tmp` 为空；下载器残留 exe 的 mtime 仍是安装前的 2026/10/6 20:48:32 |

**这一条同时推翻了本文档早先的断言**（原文见第 6 章旧版：`HKCU:\…\Endfield ← 存在但【空】（游戏未注册）`），
也解除了风险 #2「启动器是否认账」——**认账**。

### 11.4 装完之后，游戏自己写了 12 个文件

**这是 `--prune` 的真实风险面。** 这 12 个文件的 mtime 都晚于安装结束（09:36:50），
且 `efd plan --stale` 会把它们**全部列为"本地多余(陈旧)"**：

```
eld_Endfield.db
AntiCheatExpert/pld.dat
CrashSightLog/CrashSight.1791349891.11800.log
CrashSightLog/CrashSight.1791349901.7892.log
Endfield_Data/Plugins/x86_64/wesight/crashsight_data/Endfield.exe_crashsight_data_db
Endfield_Data/Plugins/x86_64/wesight/crashsight_data/PlatformProcess.exe_crashsight_data_db
HGEventLog_Encrypted/sdid_s
U8Data/config/Launcher.meta
mmkv/gameprotocol_cache
mmkv/gameprotocol_cache.crc
mmkv/login_cache
mmkv/login_cache.crc
```

全是运行期存档 / 日志 / 缓存，**删掉会重置登录态**。当时的 `--prune` 会把它们全部列为陈旧。

> ⚠️ **这里记的是当时的现象，该 bug 已修**：现在的 `--prune` 只删安装日志记过的文件，
> 上面这 12 个一个都不会动。修复过程与更完整的实测数据见第 12 章。
> 另外这个数字本身就说明问题 —— 安装后目录从 1613 涨到 1635、再到 1747 个文件，
> 不在清单里的从 12 涨到 **146 个**，路径事先无法枚举。
> （`StreamingAssets/VFS/` 下的热更新产物已在 `6a4a2db` 排除出删除范围。）

### 11.5 独立监视线（第三方观测）

`watch_download.py` 每 30 s 采样一次，共 **64 个采样点（32 分钟）**，四点判据全过：

| # | 判据 | 结果 |
|---|---|---|
| 1 | 进程存活 | 64/64 个点都活着 |
| 2 | 已写字节单调性 | 单调不回退 |
| 3 | 同时进行中的 `.part` 数量 | `{1: 64}` —— 任何时刻恰好一个 |
| 4 | 换文件交接 | 交接 8 次，每次 `.part` 归零都**恰好 +1** 个完成文件 |

- 30 min 时界面显示 21.0%（9/317 文件，8.55 GB / 42.47 GB）vs 实测 9 文件 / 8.45 GiB = 9.07 GB —— **一致**。
- 该窗口 8.45 GiB / 31.8 min = **4.54 MiB/s**（含换文件间隙）。D 盘 73.49 → 65.26 GB。
- **老 bug（提前报完成）未复现。**

### 11.6 磁盘守恒

D 盘起点 91.16 GB 可用 → 预期剩 33.20 GB，**实际 31.6 GB**，差 **−1.6 GB**。
差额来自游戏运行期写入的日志/缓存与 NTFS 元数据，属于合理范围，**不是泄漏**。

---

## 12. `--prune` 误删运行期文件：修复记录

第 11.4 节记录的 12 个运行期文件不是「一批特例」，而是一类会**持续增长**的现象。
本章记录修它的过程 —— 其中两条判据被真实数据证伪，值得留着别再走一遍。

### 12.1 问题的真实规模

游戏每启动一次就往安装目录里写新文件。实测同一份安装，目录文件数变化：

| 时点 | 磁盘文件数 | 不在清单里的 |
|---|---|---|
| 安装刚完成 | 1613 | 12 |
| 又启动过几次 | 1635 | 34 |
| 再启动过几次 | 1747 | **146** |

清单（`data/package_manifest.csv`，1601 个 file 条目）本身**没有任何变化**，涨的全是
游戏自己写的。146 个里包括整个 `Endfield_Data/Persistent/` 树（下载缓存、`Temp/`、
`VFS/`、`index_main.json`、`pref_initial.json`）、`WebviewConfig/localBulletin`、
`sdklogs/HGEventLog.log`、`eld_PlatformProcess.db` 等，**单个文件最大 1,473,737,190 B（≈1.37 GiB）**。

**关键结论：路径事先无法枚举。** 游戏既往未声明的目录写（`Endfield_Data/Persistent/`、
`sdklogs/`、`mmkv/`），也往清单确实声明过的目录写（`WebviewConfig/` 有 8 条清单条目，
却仍多出 `localBulletin`、`popupVersion`），也往根目录写（`eld_Endfield.db`）。

### 12.2 两条被证伪的判据（不要再试）

**① 结构判据：「父目录没被清单声明」即运行期产物。**
清单自带 91 个 `Kind=dir` 目录条目，实测它与「由 1601 个文件路径推出的父目录集合」
**双向差集都是空**（`inferred - declared = []`、`declared - inferred = []`），
不变量看起来完美成立（`file entries whose parent is NOT a declared dir = 0`）。

但它**是错的**：`mmkv/`（游戏运行期建的）与 `leftover/`（旧版本删剩的）结构完全相同，
都是未被声明的父目录，无从区分。跑全量测试立刻挨了一记：
`FAIL: test_prune_removes_stale_files`，`AssertionError: 'leftover/old.chk' not found in []`
—— 那不是「测试过时」，是**真实回归**：该判据让 `--prune` 静默放过真正该删的旧残留。
即便只看覆盖率它也不够，结构判据对当时那 12 个文件只覆盖 9/12。

**② 静态路径黑名单（`RUNTIME_PATHS`）。**
回退成显式列出已知运行期路径（4 个目录前缀 + 3 个具体文件）。测试全绿，但拿到真实安装上
一跑就露馅：清单保护 14 条、仍有 **20 条会被误删**，其中 16 条在 `Endfield_Data/Persistent/`。
黑名单永远是「上一次观测的合影」，而这个集合每次都变。

### 12.3 采用的方案：安装日志

`efd/core/journal.py` —— EFD 记下**自己写过哪些文件、写完是什么尺寸**：

```json
{"targets": {"d:/apps/.../arknights endfield": {"Endfield.exe": 1234567, ...}}}
```

`--prune` 只删「日志里有 **且** 尺寸与当时一致」的文件，其余进「本地外来(保留)」桶只报告不删。

三个设计取舍：

1. **尺寸也记。** 光记名字的话，玩家手改过的文件（比如换了个 mod）会被删掉。
2. **路径规范化**（`os.path.normcase(os.path.abspath(...))`）—— 大小写、正反斜杠、
   尾分隔符的不同写法都算同一个目标；最多记 8 个目标，按最近使用淘汰。
3. **`plan.already` 也要补记。** 老用户拿 v0.2.4 装完 57 GiB 才升级到这一版，
   这些文件路径尺寸与清单分毫不差、已被 `_looks_installed` 认过一遍；不补记的话它们
   永远是「外来文件」，`--prune` 再也回收不了旧版本残留 —— 而那正是功能存在的意义。

配套改动：`Plan` 新增 `foreign` 字段（进 `--json` 与 `report.print_plan` 的
「本地外来(保留)」行）；`_prune` 返回值从 `int` 改成真的删掉的名字列表（好把它从日志里划掉）；
`cmd_install` 的早退条件从 `if not plan.need` 改成 `if not plan.need and not args.prune` ——
否则装好的目录永远等不到第一次 `install`，日志永远建不起来。

**已知代价（刻意接受）：** 日志启用前的老安装第一次跑 `--prune` **什么也不删**（先补记，
下次才具备回收能力）；`StreamingAssets/VFS` 仍在删除范围外（见 12.4）。

### 12.4 真实安装上的验证

`D:\tmp\efd-probe\verify_journal.py`（只读，离线用清单 CSV）把真实安装喂给新判据：

```
清单文件条目 = 1601
磁盘文件数   = 1747
[首次启用，日志为空] 可删 = 0   只报告 = 146
[日志已建立]         可删 = 0   只报告 = 146
```

**146 个运行期文件一个都不会删**，包括整个 `Endfield_Data/Persistent/` 树。
第二个场景（日志已用真实安装的 `already` 引导过）仍是 0 —— 因为这 146 个从未被本工具写过。

`StreamingAssets/VFS` 依然排除在外：通道 B 热更新会往那里新增文件（实测 115 个文件、
7.23 GiB），它们不在通道 A 清单里。这条排除现在**同时**由日志判据兜底（日志不会记 VFS
里的热更新文件），但显式排除保留 —— 双保险。代价仍是旧版本 VFS 残留（67 个文件、
0.71 GiB）回收不了：**漏删 0.71 GiB，好过误删 7.23 GiB。**

### 12.5 测试

测试从 438 涨到 **470**（新增 `tests/test_journal.py` 25 个用例，`test_installer.TestPrune`
改写并扩到 9 个，`test_commands` 增 2 个）。三条最关键的：

- `test_prune_spares_files_the_journal_never_saw` —— 造 `mmkv/login_cache` +
  `Endfield_Data/Persistent/index_main.json`，断言 `plan.stale == []`、两者都在
  `plan.foreign`、`pruned == 0`。**这就是原始 bug 的回归测试。**
- `test_prune_removes_stale_files` —— 守住另一头：日志记过的旧残留**必须**还能删。
  （结构判据正是死在这条上。）
- `test_already_installed_files_are_recorded_too` —— 装一遍 → 清空日志 → 再装一遍全 already
  → 断言补记成功。**它抓出一个真 bug**：主循环里重复声明 `placed` 把播种的 `already` 清空了。

`test_journal.py` 的 `JournalCase` 把 `journal.state_path` 打桩到临时目录，
`test_installer.InstallerCase.setUp` 同理 —— 17 处真实 `install()` 调用绝不会碰用户真实的
`%LOCALAPPDATA%\EFD\journal.json`。

> 两个自己写的测试用例最初是错的，值得记一笔：① 把文件名 `"7"` 当数字过滤掉 —— JSON 的键
> 一定是字符串，`"7"` 是**合法文件名**（真有个文件叫 `7`），丢掉它才是 bug；
> ② 「状态文件位置」那条拿打桩后的 `state_path()` 跟 `state_dir()` 比，是在自问自答。
> 装置里得先存一份**没被打桩的**真函数。

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
├── LICENSE                  MIT
├── ico.png                  图标源图（1254×1254，字标为白字暗底）
├── pyproject.toml
├── run-gui.cmd              双击从源码启动 GUI
├── build.cmd                双击构建单文件 exe
├── efd/
│   ├── core/                与界面无关的一切
│   │   ├── config.py            常量与接口参数
│   │   ├── util.py              格式化 / CRC32 / safe_join / 输出编码
│   │   ├── detect.py            从注册表定位游戏目录
│   │   ├── settings.py          记住上次用过的安装目录与网络选项
│   │   ├── journal.py           安装日志：记下本工具写过哪些文件（--prune 的唯一判据）
│   │   ├── seed.py              Seed 接口客户端
│   │   ├── throttle.py          限速（虚拟时间槽令牌桶）
│   │   ├── archive.py           ZIP 归档视图
│   │   ├── planner.py           差集与续传判定
│   │   └── installer.py         安装循环（流式）
│   ├── net/                 传输层
│   │   ├── volume.py            分卷协议（本地 / 缺失占位）
│   │   ├── http.py              HTTP Range 分卷 + 取数重试
│   │   ├── stream.py            跨卷可寻址流（ConcatReader）
│   │   └── prefetch.py          顺序预读线程池
│   ├── ui/                  Tkinter 界面
│   │   ├── app.py / panels.py   窗口与面板
│   │   ├── handlers.py          回调（按钮、进度、收尾）
│   │   ├── messages.py          worker ↔ 界面的消息协议
│   │   ├── theme.py             观感
│   │   └── entry.py             进 GUI 的入口
│   └── cli/                 命令行
│       ├── parser.py            参数解析
│       ├── commands.py          plan / install / probe / gui
│       ├── report.py            人读与 --json 两种输出
│       ├── runner.py            子命令分发与异常→退出码
│       └── exitcode.py          退出码常量
├── packaging/               PyInstaller 入口、spec、图标
├── tests/                   470 个用例，标准库 unittest
├── data/
│   ├── package_manifest.csv 1692 条清单（派生物）
│   ├── hotfix_index_main.json / hotfix_index_initial.json
│   └── fixtures/            vol054.bin + pack_sizes.json
├── docs/
│   ├── REPORT.md            本文档
│   └── evidence/            取证样本（见该目录 README）
└── tools/
    ├── build_exe.py         构建单文件 exe（含冒烟测试）
    ├── make_icon.py         从 ico.png 生成 packaging/efd.ico（手写 ICO，无 Pillow）
    ├── export_manifest.py   从夹具重建清单 CSV
    ├── bench_prefetch.py    实测顺序预读的收益（联网）
    └── bench_gaps.py        实测预读在带空洞计划上的白读（纯计算）
```

### 分发形态

| 形态 | 产物 | 目标机要求 |
|---|---|---|
| 单文件 exe | `dist/EFD.exe`（13.7 MiB） | **无需 Python**，双击即用 |
| wheel | `efd-<版本>-py3-none-any.whl`（33 KB） | Python 3.10+，`pip install efd` |
| 源码 | 本仓库 | Python 3.10+，`python -m efd gui` |

exe 是**一个文件同时承载 CLI 与 GUI**：双击（无参数）进图形界面，
终端里可以正常跑 `EFD.exe plan …`，`EFD.exe gui` 会自动摘掉控制台窗口。
exe 带完整版本资源（发布者 / 版本 / 版权 / MIT 声明）与图标，
但**未做代码签名**——分发时对方会看到 SmartScreen 警告，这在本机无法解决。

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
