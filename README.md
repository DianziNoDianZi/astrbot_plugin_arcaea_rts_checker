# Arcaea 查分插件（AstrBot）

基于 **Lost-MSth/Arcaea-server** 的 AstrBot 插件，支持 Best30 曲绘合成图、Recent 最近游玩、Info 玩家信息、Song 单曲查询等全部指令。

## 后端说明

插件使用 [Lost-MSth/Arcaea-server](https://github.com/Lost-MSth/Arcaea-server) 作为后端。

**Arcaea-server** 是一款自托管的 Arcaea 成绩管理系统，支持：
- 多用户注册与 st3 成绩文件上传
- REST API 查询（Best30 / Recent / 曲目信息等）
- 多人协作与好友分享
- 详细的数据管理与可视化

你有两个选择：

1. **使用公共实例**：无需部署，直接使用社区维护的公共 Arcaea-server
2. **自建 Arcaea-server**：在服务器上部署自己的实例（推荐，有完整数据控制权）

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

## 使用前配置

### 方式一：使用公共 Arcaea-server 实例（最快）

直接发送以下命令配置公共实例：

```
/arc config https://arcaea.lost-msth.cn/Arcaea-Server
```

> 注意：公共实例的可用性由维护者决定，如有不稳定请考虑自建。

### 方式二：自建 Arcaea-server（推荐）

1. 克隆 Arcaea-server：

   ```bash
   git clone https://github.com/Lost-MSth/Arcaea-server.git
   cd Arcaea-server
   ```

2. 参考 [部署文档](https://arcaea.lost-msth.cn/Arcaea-Server/) 配置并启动

3. 在 Arcaea-server 的网页上注册账号并上传 st3 成绩文件

4. 配置插件：

   ```
   /arc config https://你的服务器地址/Arcaea-Server
   ```

### 绑定账号

```
/arc bind <你的Arcaea好友码>
```

> 如果不知道好友码，可以在 Arcaea 客户端的个人资料页面找到（9 位数字）

## 命令一览

| 命令 | 说明 |
| ---- | ---- |
| `/arc` 或 `/arc help` | 查看帮助 |
| `/arc config <server地址> [token]` | 配置 Arcaea-server 地址 |
| `/arc bind <好友码/用户名/user_id>` | 绑定账号 |
| `/arc unbind` | 解除绑定 |
| `/arc info [用户]` | 查询玩家基本信息 + PTT + 附曲绘 |
| `/arc recent [用户]` | 查询最近游玩记录（最多 10 条）+ 附曲绘 |
| `/arc best [用户]` | 查询 Best 30 + **曲绘合成图** |
| `/arc b30 [用户]` | 同上（别名） |
| `/arc song <曲目名> [难度]` | 查询某曲目最高成绩 + 附曲绘 |

**`/arc best` 和 `/arc recent` 输出曲绘合成长图。**

难度标识：`pst` / `prs` / `ftr` / `byd`

绑定账号后，所有用户参数均可省略，默认查询自己。

## 数据存储

| 数据 | 路径 |
| ---- | ---- |
| 用户映射 | `data/plugins/astrbot_plugin_arcaea/data/user_map.json` |
| 插件配置 | `data/plugins/astrbot_plugin_arcaea/data/config.json` |
| 曲绘缓存 | `data/plugins/astrbot_plugin_arcaea/data/cache/`（自动清理，最多保留 7 天 / 500 文件）|

## 字体说明

插件生成的合成图默认会尝试加载系统中的以下字体（按优先级）：

- Linux: `NotoSansCJK-Bold.ttc` / `wqy-microhei.ttc`
- macOS: `PingFang.ttc`
- Windows: `msyh.ttc`（微软雅黑）

若未安装中文字体，中文曲目名可能显示为方块。推荐安装：

```bash
# Debian / Ubuntu
apt-get install fonts-noto-cjk
```

## 开发

代码格式化工具：[ruff](https://docs.astral.sh/ruff/)

```bash
ruff format main.py
ruff check main.py --fix
```

## API 格式（供参考）

插件使用 Arcaea-server 的 REST API：

```
GET  {api_url}/api/v1/users/<user_id>/info   → 用户信息 + Recent
POST {api_url}/api/v1/users/<user_id>/best  → Best30
GET  {api_url}/api/v1/songs                 → 全部曲目列表
GET  {api_url}/api/v1/songs/<song_id>       → 曲目详情
```

响应格式：`{"code": 0, "data": {...}, "msg": ""}`

完整 API 文档：https://arcaea.lost-msth.cn/Arcaea-Server/API-doc/

---

**注意：使用 Arcaea 第三方查分 API 可能违反 Arcaea 使用条款。请在使用前确认遵守相关协议。建议使用小号而非主号作为查分账号。**
