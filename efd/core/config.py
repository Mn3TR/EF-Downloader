"""常量与接口参数。

这里的接口地址与 appcode 来自启动器自身的网络请求（取证过程见 docs/REPORT.md），
**不是官方公开 API**，服务端随时可能改动。改动时优先看这里。

命名约定：``CLIENT_*`` 前缀表示「我们在请求里自称的版本」，不是我们探测到的真实版本。
"""

from __future__ import annotations

# --- Seed 接口 ---------------------------------------------------------------
SEED_URL = "https://launcher.hypergryph.com/api/proxy/batch_proxy"

GAME_APPCODE = "6LL0KJuqHBVz33WK"
LAUNCHER_APPCODE = "abYeZZ16BPluCFyT"

CHANNEL = "1"
SUB_CHANNEL = "1"
SEED_SEQ = "6"

# 请求参数里自称的版本。服务端据此判断「你需要全量包还是增量」，
# 自称一个较老的版本会稳定拿到全量包。
CLIENT_LAUNCHER_VERSION = "1.2.1"
CLIENT_GAME_VERSION = "1.1.9"

# 实测可用。沿用 AKEDataTool 的 UA 是为了不改变服务端行为；
# 未验证过 UA 是否参与判定，改动前请先跑 ``efd plan`` 确认仍能拿到 54 卷。
USER_AGENT = "AKEDataTool/0.1"

DEFAULT_TIMEOUT = 60

# --- 分卷 ---------------------------------------------------------------------
# 实测：前 53 卷每卷恰好 1 GiB，末卷 6,706,365 B，合计 56,915,023,037 B。
# 见 data/fixtures/pack_sizes.json（由 Seed 实测导出）。
VOLUME_SIZE = 1 << 30

# --- 可选的排除集 --------------------------------------------------------------
# 默认**不排除任何东西**：「安装游戏」的默认含义就是把归档里的东西都装上。
# 下面两组是给想要收窄范围的人用的开关。
#
# 反作弊（ACE）装到 C:\Program Files\AntiCheatExpert，且在游戏启动时会被自动
# 重装；解压它的文件不等于完成安装（内核驱动/服务/注册表要它的安装程序来做）。
EXCLUDE_ACE: tuple[str, ...] = (
    "AntiCheatExpert/",
    "sdkresources/",
    "sdkdata/",
)

# 占 96% 体积的流式资源。跳过它可以只看小文件，用于试探性安装。
EXCLUDE_STREAMING: tuple[str, ...] = (
    "Endfield_Data/StreamingAssets/",
)

# 通道 B（资源热更新）会往这些子树里**新增**文件，而新增的条目不在通道 A 的
# 清单里。所以「本地存在但通道 A 清单里没有」在这一子树下**不等于多余** ——
# 实测热更新相对 1.5.3 包新增 115 个文件、7.23 GiB，全部落在 VFS 下。
#
# 代价是 `--prune` 再也回收不了旧版本留下的 VFS 残留（实测 67 个文件、0.71 GiB）。
# 这是刻意的取舍：**漏删 0.71 GiB，好过误删 7.23 GiB。**
HOT_UPDATE_PATHS: tuple[str, ...] = (
    "Endfield_Data/StreamingAssets/VFS/",
)

__all__ = [
    "SEED_URL",
    "GAME_APPCODE",
    "LAUNCHER_APPCODE",
    "CHANNEL",
    "SUB_CHANNEL",
    "SEED_SEQ",
    "CLIENT_LAUNCHER_VERSION",
    "CLIENT_GAME_VERSION",
    "USER_AGENT",
    "DEFAULT_TIMEOUT",
    "VOLUME_SIZE",
    "EXCLUDE_ACE",
    "EXCLUDE_STREAMING",
    "HOT_UPDATE_PATHS",
    "seed_payload",
]


def seed_payload() -> dict:
    """构造 batch_proxy 请求体。

    一次请求同时问「最新启动器」和「最新游戏包」。前者其实用不上，
    但去掉后服务端会拒答 ``get_latest_game``，所以保留。
    """
    return {
        "proxy_reqs": [
            {
                "get_latest_launcher_req": {
                    "appcode": LAUNCHER_APPCODE,
                    "channel": CHANNEL,
                    "sub_channel": SUB_CHANNEL,
                    "target_app": "EndField",
                    "version": CLIENT_LAUNCHER_VERSION,
                },
                "kind": "get_latest_launcher",
            },
            {
                "get_latest_game_req": {
                    "appcode": GAME_APPCODE,
                    "channel": CHANNEL,
                    "sub_channel": SUB_CHANNEL,
                    "launcher_appcode": LAUNCHER_APPCODE,
                    "version": CLIENT_GAME_VERSION,
                },
                "kind": "get_latest_game",
            },
        ],
        "seq": SEED_SEQ,
    }
