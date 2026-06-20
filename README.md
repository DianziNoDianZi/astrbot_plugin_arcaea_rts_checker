# Arcaea 查分插件（AstrBot）

一个基于 **ArcaeaUnlimitedAPI (AUA)** 的 AstrBot 插件，用于查询 Arcaea 玩家的基本信息、最近游玩、Best 30 及曲目最高成绩，支持 Best30 曲绘合成图。

## 安装

1. 将本目录整个复制到 AstrBot 的 `data/plugins/` 目录下：

   ```
   AstrBot/data/plugins/astrbot_plugin_arcaea/
   ```

2. 安装依赖：

   ```bash
   cd AstrBot/data/plugins/astrbot_plugin_arcaea
   pip install -r requirements.txt
   ```

3. 在 AstrBot 的插件管理中重载插件，或重启 AstrBot。

## 字体说明

插件生成的 Best30 / Recent 等合成图默认会尝试加载系统中的以下字体（按顺序优先级）：

- Linux: `NotoSansCJK-Bold.ttc` / `wqy-microhei.ttc` / `wqy-zenhei.ttc`
- macOS: `PingFang.ttc`
- Windows: `msyh.ttc`（微软雅黑）

若未安装中文字体，图片中的中文曲目名可能显示为方块。推荐安装：

```bash
# Debian / Ubuntu
apt-get install fonts-noto-cjk

# macOS (Homebrew)
brew install font-noto-sans-cjk-jp
```

## 使用前配置

插件依赖 **ArcaeaUnlimitedAPI (AUA)** 提供的第三方查分接口。
你可以使用社区公开的 AUA 实例，也可以自行搭建。

配置方式（私聊或群里对 Bot 发送）：

```
/arc config https://your-aua-server.example.com [可选Token] [可选曲绘URL]
```

曲绘 URL 支持 `{song_id}` 和 `{diff}` 占位符，默认为：

```
https://cdn.arcaea.lowiro.com/cover/{song_id}_{diff}.jpg
```

> 如果你使用的 AUA 实例不需要 Token，则第二个参数省略即可。

## 命令一览

| 命令 | 说明 |
| ---- | ---- |
| `/arc` 或 `/arc help` | 查看帮助 |
| `/arc bind <用户名或9位UID>` | 绑定 Arcaea 账号 |
| `/arc unbind` | 解除绑定 |
| `/arc config <API地址> [token] [cover_url]` | 配置查分 API |
| `/arc info [用户名/UID]` | 查询玩家基本信息 + PTT + 附曲绘 |
| `/arc recent [用户名/UID]` | 查询最近游玩记录（最多 10 条）+ 附曲绘 |
| `/arc best [用户名/UID]` | 查询 Best 30 + 附曲绘 |
| `/arc b30 [用户名/UID]` | 同上（别名） |
| `/arc song <曲目名> [难度]` | 查询某曲目最高分 + 附曲绘 |

**`/arc best` 和 `/arc recent` 输出曲绘合成长图。**

难度标识可选：`pst` / `prs` / `ftr` / `byd`

绑定账号后，所有 `[用户名/UID]` 均可省略，默认查询自己。

## 数据存储

| 数据 | 路径 |
| ---- | ---- |
| 绑定数据 | `data/plugins/astrbot_plugin_arcaea/data/bind.json` |
| 插件配置 | `data/plugins/astrbot_plugin_arcaea/data/config.json` |
| 曲绘缓存 | `data/plugins/astrbot_plugin_arcaea/data/cache/`（自动清理，最长保留 7 天） |

## 开发

代码格式化工具：[ruff](https://docs.astral.sh/ruff/)

```bash
ruff format main.py
ruff check main.py --fix
```

**注意：使用 Arcaea 第三方查分 API 可能违反 Arcaea 使用条款。请在使用前确认遵守相关协议。**
