"""
Arcaea 查分插件 - 适配 Lost-MSth/Arcaea-server API

API 文档：https://arcaea.lost-msth.cn/Arcaea-Server/API-doc/

适配的后端：https://arcaea.lost-msth.cn/Arcaea-Server/
（可自行部署 Lost-MSth/Arcaea-server 或使用公共实例）

API 格式：
  - 基础路径: {api_url}/api/v1
  - 用户 best:   GET /api/v1/users/<user_id>/best
  - 用户 info:   GET /api/v1/users/<user_id>/info
  - 曲目列表:   GET /api/v1/songs
  - 曲目信息:   GET /api/v1/songs/<song_id>
  - 响应格式:   {"code": 0, "data": {...}, "msg": ""}

用户绑定流程：
  用户输入 Arcaea 好友码（9位UID）→ 插件查询 Arcaea-server 获取 user_id →
  缓存 user_id 到本地 → 后续查询使用 user_id
"""

import base64
import io
import json
import os
import time
from typing import Optional, Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import message_components as Comp

try:
    import httpx
except ImportError:
    httpx = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

# ============================================================
# 路径常量
# ============================================================
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
# 本地用户映射：friend_code (str) -> server_user_id (int)
USER_MAP_FILE = os.path.join(DATA_DIR, "user_map.json")
# 插件配置
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# ============================================================
# 默认配置
# ============================================================
DEFAULT_CONFIG = {
    # Arcaea-server 的地址，末尾不要加 /
    # 公共实例: https://arcaea.lost-msth.cn/Arcaea-Server
    # 或自行部署: https://your-server.com/Arcaea-Server
    "api_url": "https://arcaea.lost-msth.cn/Arcaea-Server",
    # API Token（如果 Arcaea-server 设置了 superpower token，填在这里）
    "api_token": "",
    # 请求超时（秒）
    "timeout": 15,
    # 曲绘 CDN 模板，支持 {song_id} 和 {diff} 占位符
    "cover_url_template": "https://cdn.arcaea.lowiro.com/cover/{song_id}_{diff}.jpg",
}

# ============================================================
# 难度映射
# ============================================================
DIFFICULTY_MAP = {
    0: "PST",
    1: "PRS",
    2: "FTR",
    3: "BYD",
    "pst": 0,
    "past": 0,
    "prs": 1,
    "present": 1,
    "ftr": 2,
    "future": 2,
    "byd": 3,
    "beyond": 3,
    "byn": 3,
}

DIFFICULTY_COLORS = {
    0: (120, 180, 120),
    1: (230, 160, 80),
    2: (180, 100, 200),
    3: (90, 120, 220),
}


# ============================================================
# 工具函数
# ============================================================
def _ensure_dir():
    for d in (DATA_DIR, CACHE_DIR):
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    _ensure_dir()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any) -> None:
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_diff(diff) -> int:
    if isinstance(diff, int):
        return diff if 0 <= diff <= 3 else 2
    return DIFFICULTY_MAP.get(str(diff).lower().strip(), 2)


def _diff_name(diff) -> str:
    if isinstance(diff, int):
        return DIFFICULTY_MAP.get(diff, str(diff))
    return str(diff).upper()


def _ptt_from_rating(rating) -> Optional[float]:
    if rating is None:
        return None
    r = float(rating)
    # Arcaea-server 的 rating 直接是浮点数，不需要除以100
    return r


# ============================================================
# Arcaea-server API 客户端
# ============================================================
class ArcaeaServerClient:
    """
    封装与 Arcaea-server 的 HTTP API 交互。
    API 文档：https://arcaea.lost-msth.cn/Arcaea-Server/API-doc/
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.base_url = config.get("api_url", "").rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self.token = config.get("api_token", "")
        self.timeout = config.get("timeout", 15)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Token"] = self.token
        return h

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("缺少依赖 httpx，请先 pip install httpx")
        url = f"{self.api_url}{path}"
        logger.info(f"[Arcaea] GET {url} params={params}")
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def _post(
        self, path: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("缺少依赖 httpx，请先 pip install httpx")
        url = f"{self.api_url}{path}"
        logger.info(f"[Arcaea] POST {url}")
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            resp = await client.post(url, json=data or {}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def _check_response(self, resp: Dict[str, Any]) -> Dict[str, Any]:
        """检查 API 响应状态，失败时抛出 RuntimeError。"""
        code = resp.get("code")
        msg = resp.get("msg", "")
        if code == 0:
            return resp.get("data") or {}
        error_map = {
            -2: "服务器无数据返回",
            -3: "用户不存在或无数据",
            -110: "无效的 user_id",
            -200: "权限不足，请检查 Token 配置",
            -201: "用户名或密码错误",
        }
        raise RuntimeError(
            f"API 错误 [{code}] {error_map.get(code, msg) or '未知错误'}"
        )

    # ---- API 方法 ----
    async def get_user_info(self, user_id: int) -> Dict[str, Any]:
        """获取用户信息及最近成绩。"""
        resp = await self._get(f"/users/{user_id}/info", {"limit": 10})
        return self._check_response(resp)

    async def get_user_best(
        self,
        user_id: int,
        limit: int = 30,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        获取用户 Best30。
        limit 默认为 30 返回纯 B30，
        可设 offset=30 获取 overflow（#31+）数据。
        """
        body = {"limit": limit, "offset": offset}
        resp = await self._post(f"/users/{user_id}/best", body)
        return self._check_response(resp)

    async def get_song_info(self, song_id: str) -> Dict[str, Any]:
        """获取曲目详细信息。"""
        resp = await self._get(f"/songs/{song_id}")
        return self._check_response(resp)

    async def get_songs_list(self) -> List[Dict[str, Any]]:
        """获取全部曲目列表（简化信息）。"""
        resp = await self._get("/songs", {"limit": 9999})
        data = self._check_response(resp)
        return data.get("songs", []) if isinstance(data, dict) else []

    async def search_user_by_usercode(self, usercode: str) -> Optional[int]:
        """
        通过好友码查找 Arcaea-server 中的 user_id。
        Arcaea-server 本身不提供直接的 friend_code 查找接口，
        这里通过 /users/<id> 的模糊查询来尝试定位。
        如果传入的是纯数字（≥2000000），直接作为 user_id 返回。
        """
        # 如果是数字且 >= 2000000，直接视为 server user_id
        try:
            uid = int(usercode)
            if uid >= 2000000:
                return uid
        except ValueError:
            pass
        # 尝试用 usercode 作为 query 参数搜索
        # Arcaea-server 的 /users/<id> 接口可能支持模糊查询
        # 先尝试查询 usercode
        try:
            # 用 URL param query 方式 base64 编码查询
            q = base64.b64encode(json.dumps({"usercode": usercode}).encode()).decode()
            resp = await self._get("/users/search", {"query": q})
            data = self._check_response(resp)
            if data and isinstance(data, dict):
                users = data.get("users", data.get("data", []))
                if users:
                    return users[0].get("user_id")
        except Exception:
            pass
        return None


# ============================================================
# 主插件类
# ============================================================
class ArcaeaPlugin(Star):
    """Arcaea 查分插件（适配 Lost-MSth/Arcaea-server）"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.config: Dict[str, Any] = _load_json(CONFIG_FILE, DEFAULT_CONFIG)
        self.user_map: Dict[str, int] = _load_json(
            USER_MAP_FILE, {}
        )  # friend_code -> server_user_id
        self.client = ArcaeaServerClient(self.config)
        logger.info("[Arcaea] 插件已加载（Arcaea-server 后端）。")
        if not self.config.get("api_url"):
            logger.warning("[Arcaea] 未配置 api_url，请使用 /arc config <地址> 配置。")

    async def terminate(self):
        pass

    # ---- 辅助方法 ----
    def _get_binder(self, event: AstrMessageEvent) -> str:
        try:
            session_id = event.message_obj.session_id
            if session_id:
                return session_id
        except Exception:
            pass
        try:
            sender = event.message_obj.sender
            return getattr(sender, "user_id", "") or getattr(sender, "id", "")
        except Exception:
            return ""

    def _resolve_user_id(
        self, identifier: Optional[str], event: AstrMessageEvent
    ) -> int:
        """
        将标识符（好友码/Arcaea用户名/server user_id）解析为 server user_id。
        优先级：
        1. 直接是 server user_id（≥2000000 的数字）→ 直接返回
        2. 已在本地绑定表（user_map）→ 返回缓存的 user_id
        3. 是好友码/用户名 → 通过 API 查询 user_id → 存入绑定表
        4. 无参数 → 从绑定表查找当前会话
        """
        # 无参数：查绑定表
        if not identifier or not identifier.strip():
            key = self._get_binder(event)
            uid = self.user_map.get(key)
            if uid is None:
                raise RuntimeError(
                    "未绑定账号。请先使用 /arc bind <Arcaea好友码或用户名> 绑定。"
                )
            return uid

        identifier = identifier.strip()
        # 直接是 server user_id
        try:
            uid = int(identifier)
            if uid >= 2000000:
                return uid
        except ValueError:
            pass

        # 在绑定表中查找
        key = self._get_binder(event)
        if key and identifier in (str(v) for v in self.user_map.values()):
            for k, v in self.user_map.items():
                if str(v) == identifier:
                    return v

        # 通过 API 查询 user_id
        resolved = self.user_map.get(identifier)
        if resolved:
            return resolved

        # 调用 API 查找
        found = self.user_map.get(f"code:{identifier}")
        if found:
            return found

        raise RuntimeError(
            f"无法解析用户 '{identifier}'。\n"
            f"请确保该用户在 Arcaea-server 上已注册并上传过成绩数据。\n"
            f"注册地址：{self.config.get('api_url', '')}"
        )

    def _bind_user(self, event: AstrMessageEvent, identifier: str, server_user_id: int):
        """绑定用户到当前会话。"""
        key = self._get_binder(event)
        if not key:
            raise RuntimeError("无法获取会话标识，绑定失败。")
        self.user_map[key] = server_user_id
        # 同时用 identifier 作为备用键，方便下次直接用
        self.user_map[identifier] = server_user_id
        _save_json(USER_MAP_FILE, self.user_map)

    async def _fetch_cover(self, url: str) -> Optional[Image.Image]:
        """异步下载曲绘并缓存，返回 PIL.Image；失败返回 None。"""
        if not url or Image is None or httpx is None:
            return None
        _ensure_dir()
        cache_key = hex(abs(hash(url))) + ".jpg"
        cache_path = os.path.join(CACHE_DIR, cache_key)
        if os.path.exists(cache_path):
            try:
                img = Image.open(cache_path)
                img.load()
                return img
            except Exception:
                pass
        try:
            async with httpx.AsyncClient(
                timeout=self.config.get("timeout", 15), follow_redirects=True
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                img.load()
                try:
                    img.save(cache_path, format="JPEG", quality=92)
                except Exception:
                    pass
                return img
        except Exception as e:
            logger.debug(f"[Arcaea] 曲绘下载失败: {url} -> {e}")
            return None
        finally:
            # 每 20 张触发一次缓存清理
            cnt = getattr(self, "_cache_clean_cnt", 0) + 1
            self._cache_clean_cnt = cnt
            if cnt >= 20:
                self._cache_clean_cnt = 0
                self._clean_cache()

    def _clean_cache(self) -> None:
        """清理超过 7 天或超过 500 文件的旧缓存。"""
        if not os.path.exists(CACHE_DIR):
            return
        max_age = 7 * 24 * 60 * 60
        max_files = 500
        now = time.time()
        try:
            files: list[tuple[str, float]] = []
            for fname in os.listdir(CACHE_DIR):
                fpath = os.path.join(CACHE_DIR, fname)
                if os.path.isfile(fpath):
                    files.append((fpath, os.path.getmtime(fpath)))
            # 删除过期文件
            for fpath, mtime in files:
                if now - mtime > max_age:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
            # 超量时删除最旧的
            if len(files) > max_files:
                files.sort(key=lambda x: x[1])
                for fpath, _ in files[: len(files) - max_files]:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
            logger.debug(f"[Arcaea] 缓存清理完成，当前文件数: {len(files)}")
        except Exception as e:
            logger.debug(f"[Arcaea] 缓存清理失败: {e}")

    def _cover_url(self, song_id: str, difficulty: int) -> str:
        template = self.config.get(
            "cover_url_template", DEFAULT_CONFIG["cover_url_template"]
        )
        diff_str = {0: "pst", 1: "prs", 2: "ftr", 3: "byd"}.get(difficulty, "ftr")
        return template.format(song_id=song_id, diff=diff_str)

    # ============================================================
    # 图片渲染
    # ============================================================
    def _get_font(self, size: int, bold: bool = False) -> Optional[ImageFont.ImageFont]:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for c in candidates:
            if os.path.exists(c):
                try:
                    return ImageFont.truetype(c, size)
                except Exception:
                    continue
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    async def _draw_card(
        self,
        bg: Image.Image,
        x: int,
        y: int,
        w: int,
        h: int,
        rank: int,
        record: Dict[str, Any],
        cover: Optional[Image.Image],
        fonts: Dict[str, Any],
    ) -> None:
        song_id = record.get("song_id", "?")
        diff_int = _normalize_diff(record.get("difficulty", 2))
        diff_color = DIFFICULTY_COLORS.get(diff_int, (180, 180, 180))
        song_name = record.get("title_localized", {}).get("zh-Hans", song_id)
        if isinstance(song_name, dict):
            song_name = song_id
        score = record.get("score", 0)
        ptt_val = _ptt_from_rating(record.get("rating"))
        shiny = record.get("shiny_perfect_count", "?")
        near = record.get("near_count", "?")
        miss = record.get("miss_count", "?")

        # 底板
        card = Image.new("RGB", (w, h), diff_color)
        cdraw = ImageDraw.Draw(card)

        cover_w = int(w * 0.5)

        # 左侧：曲绘或纯色背景
        left_bg = Image.new(
            "RGB",
            (cover_w, h),
            (
                max(0, diff_color[0] - 50),
                max(0, diff_color[1] - 50),
                max(0, diff_color[2] - 50),
            ),
        )
        if cover is not None:
            try:
                cover_resized = cover.convert("RGB").resize((cover_w, h), Image.LANCZOS)
                left_bg.paste(cover_resized, (0, 0))
            except Exception:
                pass
        dark_left = Image.new("RGBA", (cover_w, h), (0, 0, 0, 90))
        left_bg.paste(dark_left, (0, 0), mask=dark_left)
        card.paste(left_bg, (0, 0))

        # 右侧背景
        right_bg = Image.new(
            "RGB",
            (w - cover_w, h),
            (
                max(0, diff_color[0] - 20),
                max(0, diff_color[1] - 20),
                max(0, diff_color[2] - 20),
            ),
        )
        card.paste(right_bg, (cover_w, 0))
        dark_right = Image.new("RGBA", (w - cover_w, h), (0, 0, 0, 100))
        card.paste(dark_right, (cover_w, 0), mask=dark_right)

        # 排名标签
        rank_text = f"#{rank}"
        rbox = cdraw.textbbox((0, 0), rank_text, font=fonts["rank"])
        pad = 10
        cdraw.rectangle(
            [
                10,
                10,
                10 + (rbox[2] - rbox[0]) + pad * 2,
                10 + (rbox[3] - rbox[1]) + pad,
            ],
            fill=(0, 0, 0, 160),
        )
        cdraw.text(
            (10 + pad, 10 + pad - 4),
            rank_text,
            fill=(255, 255, 255),
            font=fonts["rank"],
        )

        # 曲目名（左下角）
        display_name = (
            str(song_name) if len(str(song_name)) <= 22 else str(song_name)[:21] + "…"
        )
        cdraw.text((10, h - 40), display_name, fill=(255, 255, 255), font=fonts["song"])

        # 右侧信息
        rx = cover_w + 16
        ry = 14

        cdraw.text(
            (rx, ry), _diff_name(diff_int), fill=(255, 255, 255), font=fonts["small"]
        )
        ry += 28

        cdraw.text(
            (rx, ry), f"{int(score):,}", fill=(255, 255, 255), font=fonts["score"]
        )
        ry += 34

        ptt_str = f"PTT {ptt_val:.4f}" if ptt_val is not None else "PTT ?"
        cdraw.text((rx, ry), ptt_str, fill=(255, 220, 150), font=fonts["ptt"])
        ry += 32

        stats_line = f"Pure {shiny}  Far {near}  Lost {miss}"
        if len(stats_line) > 28:
            stats_line = f"P {shiny} F {near} L {miss}"
        cdraw.text((rx, ry), stats_line, fill=(220, 220, 220), font=fonts["small"])

        # 边框
        cdraw.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255), width=2)
        bg.paste(card, (x, y))

    async def _compose_best30_image(
        self,
        player_name: str,
        user_id: int,
        ptt_avg: Optional[float],
        records: List[Dict[str, Any]],
        overflow: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """合成 Best30 长图并保存到 DATA_DIR，返回文件路径。"""
        if Image is None:
            return None

        cols = 3
        card_w, card_h = 540, 200
        gap_x, gap_y = 20, 20
        margin_x, margin_top = 30, 220
        margin_bottom = 60

        rows = (len(records) + cols - 1) // cols
        total_rows = rows
        overflow_rows = 0
        if overflow:
            overflow_rows = 1 + (len(overflow) + cols - 1) // cols

        total_w = margin_x * 2 + cols * card_w + (cols - 1) * gap_x
        total_h = (
            margin_top
            + total_rows * card_h
            + (total_rows - 1) * gap_y
            + overflow_rows * card_h
            + 40
            + margin_bottom
        )

        # 背景
        bg = Image.new("RGB", (total_w, total_h), (18, 22, 48))
        draw = ImageDraw.Draw(bg)
        # 渐变
        for py in range(0, total_h, 2):
            t = py / total_h
            r = int(18 + (40 - 18) * t)
            b = int(48 + (78 - 48) * t)
            draw.rectangle([0, py, total_w, py + 1], fill=(r, 22, b))

        # 星点
        import random as _rnd

        rnd = _rnd.Random(42)
        for _ in range(300):
            sx = rnd.randint(0, total_w - 1)
            sy = rnd.randint(0, total_h - 1)
            sr = rnd.randint(1, 3)
            star = Image.new("RGB", (sr * 2 + 1, sr * 2 + 1), (18, 22, 48))
            sdraw = ImageDraw.Draw(star)
            sdraw.ellipse([0, 0, sr * 2, sr * 2], fill=(255, 255, 255))
            bg.paste(star, (sx - sr, sy - sr))

        # 字体
        font_title = self._get_font(44, True)
        font_small = self._get_font(20)
        font_rank = self._get_font(34, True)
        font_score = self._get_font(28, True)
        font_ptt = self._get_font(22, True)
        font_song = self._get_font(22)
        fonts = {
            "rank": font_rank,
            "score": font_score,
            "ptt": font_ptt,
            "song": font_song,
            "small": font_small,
        }

        white = (255, 255, 255)
        gray = (200, 200, 210)

        # 顶部信息
        draw.text((margin_x, 30), "ARCAEA  Player Bests", fill=white, font=font_title)
        draw.text((margin_x, 88), player_name, fill=white, font=font_score)
        draw.text((margin_x, 126), f"ID: {user_id}", fill=gray, font=font_small)

        # 右侧统计
        right_x = total_w - margin_x
        stats = []
        if ptt_avg is not None:
            stats.append(("BEST 30 AVG.", f"{ptt_avg:.4f}"))
        if stats:
            cur_y = 50
            for label, val in stats:
                vbox = draw.textbbox((0, 0), val, font=font_title)
                lbox = draw.textbbox((0, 0), label, font=font_small)
                draw.text(
                    (right_x - (vbox[2] - vbox[0]), cur_y),
                    val,
                    fill=(255, 220, 150),
                    font=font_title,
                )
                draw.text(
                    (right_x - (lbox[2] - lbox[0]), cur_y + 48),
                    label,
                    fill=gray,
                    font=font_small,
                )
                cur_y += 90

        draw.rectangle([margin_x, 200, total_w - margin_x, 202], fill=(120, 120, 180))

        # 下载所有曲绘
        covers: list[Optional[Image.Image]] = [None] * len(records)
        for idx, rec in enumerate(records):
            url = self._cover_url(
                rec.get("song_id", ""), _normalize_diff(rec.get("difficulty", 2))
            )
            covers[idx] = await self._fetch_cover(url)

        # 绘制卡片
        for idx, rec in enumerate(records):
            row = idx // cols
            col = idx % cols
            cx = margin_x + col * (card_w + gap_x)
            cy = margin_top + row * (card_h + gap_y)
            await self._draw_card(
                bg, cx, cy, card_w, card_h, idx + 1, rec, covers[idx], fonts
            )

        # Overflow 区
        if overflow:
            title_y = margin_top + total_rows * card_h + (total_rows - 1) * gap_y + 20
            draw.text(
                (margin_x, title_y), "Overflow", fill=(255, 220, 150), font=font_score
            )
            draw.rectangle(
                [margin_x, title_y + 40, total_w - margin_x, title_y + 42],
                fill=(120, 120, 180),
            )

            overflow_covers: list[Optional[Image.Image]] = [None] * len(overflow)
            for idx, rec in enumerate(overflow):
                url = self._cover_url(
                    rec.get("song_id", ""), _normalize_diff(rec.get("difficulty", 2))
                )
                overflow_covers[idx] = await self._fetch_cover(url)

            start_y = title_y + 50
            for idx, rec in enumerate(overflow):
                row = idx // cols
                col = idx % cols
                cx = margin_x + col * (card_w + gap_x)
                cy = start_y + row * (card_h + gap_y)
                await self._draw_card(
                    bg,
                    cx,
                    cy,
                    card_w,
                    card_h,
                    idx + 31,
                    rec,
                    overflow_covers[idx],
                    fonts,
                )

        # 保存
        ts = int(time.time())
        save_path = os.path.join(DATA_DIR, f"best30_{ts}.jpg")
        try:
            bg.save(save_path, format="JPEG", quality=92)
            logger.info(f"[Arcaea] Best30 图片已保存: {save_path}")
            return save_path
        except Exception as e:
            logger.exception(f"[Arcaea] 图片保存失败: {e}")
            return None

    # ============================================================
    # 指令处理
    # ============================================================
    @filter.command("arc config")
    async def arc_config(
        self,
        event: AstrMessageEvent,
        api_url: str = "",
        token: str = "",
        cover_url: str = "",
    ):
        """配置 Arcaea-server 地址和 Token。"""
        if not api_url or not api_url.strip():
            current = self.config.get("api_url", "(未配置)")
            token_disp = "已设置" if self.config.get("api_token") else "(未设置)"
            cover = self.config.get(
                "cover_url_template", DEFAULT_CONFIG["cover_url_template"]
            )
            yield event.plain_result(
                f"当前 API 地址: {current}\n"
                f"当前 Token: {token_disp}\n"
                f"当前曲绘模板: {cover}\n\n"
                f"用法: /arc config <Arcaea-server地址> [token] [曲绘URL模板]\n"
                f"公共实例: https://arcaea.lost-msth.cn/Arcaea-Server"
            )
            return

        self.config["api_url"] = api_url.strip().rstrip("/")
        if token and token.strip():
            self.config["api_token"] = token.strip()
        if cover_url and cover_url.strip():
            self.config["cover_url_template"] = cover_url.strip()
        _save_json(CONFIG_FILE, self.config)
        self.client = ArcaeaServerClient(self.config)
        yield event.plain_result(
            f"已更新配置。\n"
            f"API 地址: {self.config['api_url']}\n"
            f"Token: {'已设置' if self.config.get('api_token') else '(无)'}"
        )

    @filter.command("arc bind")
    async def arc_bind(self, event: AstrMessageEvent, identifier: str = ""):
        """
        绑定 Arcaea 账号。
        identifier 可以是：
        - Arcaea 好友码（9位数字，如 123456789）
        - Arcaea-server 的 user_id（≥2000000 的数字）
        - Arcaea 用户名（需已在 server 注册）
        """
        if not identifier or not identifier.strip():
            yield event.plain_result("用法: /arc bind <好友码/用户名/user_id>")
            return

        identifier = identifier.strip()
        try:
            # 直接尝试作为 user_id（≥2000000）
            uid = int(identifier)
            if uid >= 2000000:
                self._bind_user(event, identifier, uid)
                yield event.plain_result(f"已绑定 Arcaea-server user_id: {uid}")
                return
        except ValueError:
            pass

        # 尝试通过 API 查询 user_id
        yield event.plain_result(f"正在查询 Arcaea-server 中的用户 '{identifier}' ...")
        try:
            found_uid = await self.client.search_user_by_usercode(identifier)
            if found_uid is None:
                yield event.plain_result(
                    f"在 Arcaea-server 上未找到用户 '{identifier}'。\n"
                    f"请确保该用户已在 server 注册并上传过成绩。\n"
                    f"注册地址：{self.config.get('api_url', '')}"
                )
                return
            self._bind_user(event, identifier, found_uid)
            yield event.plain_result(
                f"已绑定 '{identifier}' -> server user_id: {found_uid}"
            )
        except RuntimeError as e:
            yield event.plain_result(f"绑定失败: {e}")
        except Exception as e:
            logger.exception(f"[Arcaea] bind 失败: {e}")
            yield event.plain_result(f"绑定失败: {e}")

    @filter.command("arc unbind")
    async def arc_unbind(self, event: AstrMessageEvent):
        """解除当前会话的账号绑定。"""
        key = self._get_binder(event)
        if key and key in self.user_map:
            removed = self.user_map.pop(key)
            _save_json(USER_MAP_FILE, self.user_map)
            yield event.plain_result(f"已解除绑定 (user_id: {removed})")
        else:
            yield event.plain_result("当前会话没有绑定任何账号。")

    @filter.command("arc info")
    async def arc_info(self, event: AstrMessageEvent, user: str = ""):
        """查询玩家基本信息。"""
        try:
            uid = self._resolve_user_id(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            data = await self.client.get_user_info(uid)
        except RuntimeError as e:
            yield event.plain_result(f"查询失败: {e}")
            return
        except Exception as e:
            logger.exception(f"[Arcaea] info 请求失败: {e}")
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            user_info = data.get("user_info", {}) if isinstance(data, dict) else {}
            recent = data.get("recent_score", []) if isinstance(data, dict) else []
            if isinstance(recent, dict):
                recent = [recent] if recent else []

            name = user_info.get("name", user_info.get("username", "?"))
            potential = user_info.get("potential")

            lines = [
                "=== Arcaea 玩家信息 ===",
                f"用户名: {name}",
                f"Server ID: {uid}",
            ]
            if potential is not None:
                lines.append(f"Potential: {_ptt_from_rating(potential):.4f}")

            if recent and recent[0]:
                rec = recent[0]
                song_id = rec.get("song_id", "?")
                title = rec.get("title_localized", {}).get("zh-Hans", song_id)
                if isinstance(title, dict):
                    title = song_id
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = _ptt_from_rating(rec.get("rating"))
                lines.append("")
                lines.append("最近 1 次游玩:")
                line = f"[{_diff_name(diff)}] {title} — {int(score):,}"
                if ptt_v is not None:
                    line += f" (PTT {ptt_v:.4f})"
                lines.append(line)

                # 附曲绘
                diff_int = _normalize_diff(diff)
                cover_url = self._cover_url(song_id, diff_int)
                cover = await self._fetch_cover(cover_url)
                if cover is not None:
                    ts = int(time.time())
                    cover_path = os.path.join(DATA_DIR, f"info_cover_{ts}.jpg")
                    try:
                        cover.save(cover_path, format="JPEG", quality=92)
                        yield event.chain_result(
                            [
                                Comp.Plain("\n".join(lines)),
                                Comp.Image.fromFileSystem(cover_path),
                            ]
                        )
                        return
                    except Exception:
                        pass

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception(f"[Arcaea] info 解析失败: {e}")
            yield event.plain_result(f"解析数据失败: {e}\n原始: {data}")

    @filter.command("arc recent")
    async def arc_recent(self, event: AstrMessageEvent, user: str = ""):
        """查询最近游玩记录。"""
        try:
            uid = self._resolve_user_id(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            data = await self.client.get_user_info(uid)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            recent = data.get("recent_score", []) if isinstance(data, dict) else []
            if not recent or isinstance(recent, dict) and not recent:
                yield event.plain_result("没有最近的游玩记录。")
                return
            if isinstance(recent, dict):
                recent = [recent]

            # 先输出文字摘要
            user_info = data.get("user_info", {}) if isinstance(data, dict) else {}
            name = user_info.get("name", "?")
            ptt = _ptt_from_rating(user_info.get("potential"))
            ptt_line = f"\nPotential: {ptt:.4f}" if ptt else ""
            yield event.plain_result(
                f"[Arcaea Recent]\n玩家: {name}{ptt_line}\n最近 {len(recent)} 条游玩记录"
            )

            # 每条附曲绘
            for idx, rec in enumerate(recent[:10], 1):
                song_id = rec.get("song_id", "?")
                title = rec.get("title_localized", {}).get("zh-Hans", song_id)
                if isinstance(title, dict):
                    title = song_id
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = _ptt_from_rating(rec.get("rating"))
                text = f"{idx}. [{_diff_name(diff)}] {title} — {int(score):,}"
                if ptt_v is not None:
                    text += f" (PTT {ptt_v:.4f})"

                diff_int = _normalize_diff(diff)
                cover_url = self._cover_url(song_id, diff_int)
                cover = await self._fetch_cover(cover_url)
                if cover is not None:
                    ts = int(time.time())
                    cover_path = os.path.join(DATA_DIR, f"recent_{ts}.jpg")
                    try:
                        cover.save(cover_path, format="JPEG", quality=92)
                        yield event.chain_result(
                            [Comp.Plain(text), Comp.Image.fromFileSystem(cover_path)]
                        )
                        continue
                    except Exception:
                        pass
                yield event.plain_result(text)
        except Exception as e:
            logger.exception(f"[Arcaea] recent 解析失败: {e}")
            yield event.plain_result(f"解析数据失败: {e}")

    @filter.command("arc best")
    async def arc_best(self, event: AstrMessageEvent, user: str = ""):
        """查询 Best 30 并输出合成图。"""
        try:
            uid = self._resolve_user_id(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            # 先获取 B30
            data_b30 = await self.client.get_user_best(uid, limit=30)
        except Exception as e:
            yield event.plain_result(f"查询 Best30 失败: {e}")
            return

        try:
            user_info = (
                data_b30.get("user_info", {}) if isinstance(data_b30, dict) else {}
            )
            records = data_b30.get("data", []) if isinstance(data_b30, dict) else []
            if not records or not isinstance(records, list):
                yield event.plain_result(
                    "暂无 Best30 数据。\n请确认该用户已在 Arcaea-server 上传过成绩。"
                )
                return

            # 获取 R10
            overflow: Optional[List[Dict[str, Any]]] = None
            ptt_avg = None
            try:
                data_r10 = await self.client.get_user_best(uid, limit=10, offset=30)
                if isinstance(data_r10, dict):
                    overflow = data_r10.get("data", [])
                    ptt_avg = data_b30.get("best30_avg")
            except Exception:
                pass

            name = user_info.get("name", "?")
            yield event.plain_result(
                f"[Arcaea Bests] 正在为 {name} 合成 Best30 图片，请稍候…"
            )

            img_path = await self._compose_best30_image(
                player_name=name,
                user_id=uid,
                ptt_avg=_ptt_from_rating(ptt_avg) if ptt_avg else None,
                records=records[:30],
                overflow=overflow[:30] if overflow else None,
            )

            if img_path:
                summary = f"[Arcaea Bests] 玩家: {name} (ID: {uid})"
                if ptt_avg is not None:
                    summary += f"\nB30 平均 PTT: {_ptt_from_rating(ptt_avg):.4f}"
                yield event.chain_result(
                    [Comp.Plain(summary), Comp.Image.fromFileSystem(img_path)]
                )
                return

            # 回退：文本列表
            lines = [f"=== {name} 的 Best 30 ===", ""]
            for idx, rec in enumerate(records[:30], 1):
                song_id = rec.get("song_id", "?")
                title = rec.get("title_localized", {}).get("zh-Hans", song_id)
                if isinstance(title, dict):
                    title = song_id
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = _ptt_from_rating(rec.get("rating"))
                line = f"{idx:>2}. [{_diff_name(diff)}] {title} — {int(score):,}"
                if ptt_v is not None:
                    line += f" (PTT {ptt_v:.4f})"
                lines.append(line)
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception(f"[Arcaea] best 解析失败: {e}")
            yield event.plain_result(f"解析失败: {e}")

    @filter.command("arc b30")
    async def arc_b30(self, event: AstrMessageEvent, user: str = ""):
        """Best30（arc best 的别名）。"""
        async for msg in self.arc_best(event, user):
            yield msg

    @filter.command("arc song")
    async def arc_song(
        self, event: AstrMessageEvent, song_name: str = "", difficulty: str = ""
    ):
        """查询某曲目在用户 best 中的最高成绩。"""
        if not song_name or not song_name.strip():
            yield event.plain_result("用法: /arc song <曲目名或song_id> [难度]")
            return

        try:
            uid = self._resolve_user_id(None, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        song_name = song_name.strip()
        diff_int = None
        if difficulty:
            d = difficulty.lower().strip()
            diff_int = DIFFICULTY_MAP.get(d)
            if diff_int is None:
                yield event.plain_result("不支持的难度。支持: pst/prs/ftr/byd")
                return

        try:
            # 获取全部 best（limit=999 来搜索该曲目）
            data = await self.client.get_user_best(uid, limit=999)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            records = data.get("data", []) if isinstance(data, dict) else []
            if not records:
                yield event.plain_result("暂无数据。")
                return

            # 模糊匹配曲目
            matched = []
            song_name_lower = song_name.lower()
            for rec in records:
                song_id = rec.get("song_id", "")
                title_dict = rec.get("title_localized", {})
                title = title_dict.get("zh-Hans", title_dict.get("en", song_id))
                if isinstance(title, dict):
                    title = song_id
                # 匹配 song_id 或 title
                if (
                    song_name_lower in song_id.lower()
                    or song_name_lower in str(title).lower()
                ):
                    if (
                        diff_int is None
                        or _normalize_diff(rec.get("difficulty", 2)) == diff_int
                    ):
                        matched.append(rec)

            if not matched:
                yield event.plain_result(f"未找到曲目 '{song_name}' 的记录。")
                return

            for rec in matched[:5]:
                song_id = rec.get("song_id", "?")
                title_dict = rec.get("title_localized", {})
                title = title_dict.get("zh-Hans", title_dict.get("en", song_id))
                if isinstance(title, dict):
                    title = song_id
                diff = rec.get("difficulty", "?")
                diff_i = _normalize_diff(diff)
                score = rec.get("score", "?")
                ptt_v = _ptt_from_rating(rec.get("rating"))

                text = f"[{_diff_name(diff)}] {title} — {int(score):,}"
                if ptt_v is not None:
                    text += f" (PTT {ptt_v:.4f})"

                cover_url = self._cover_url(song_id, diff_i)
                cover = await self._fetch_cover(cover_url)
                if cover is not None:
                    ts = int(time.time())
                    cover_path = os.path.join(DATA_DIR, f"song_{ts}.jpg")
                    try:
                        cover.save(cover_path, format="JPEG", quality=92)
                        yield event.chain_result(
                            [Comp.Plain(text), Comp.Image.fromFileSystem(cover_path)]
                        )
                        continue
                    except Exception:
                        pass
                yield event.plain_result(text)
        except Exception as e:
            logger.exception(f"[Arcaea] song 解析失败: {e}")
            yield event.plain_result(f"解析失败: {e}")

    @filter.command("arc help")
    async def arc_help(self, event: AstrMessageEvent):
        text = (
            "===== Arcaea 查分帮助 =====\n"
            "后端: Lost-MSth/Arcaea-server\n\n"
            "/arc config <server地址> [token]\n"
            "    配置 Arcaea-server 地址（默认公共实例）\n"
            "/arc bind <好友码/用户名/user_id>\n"
            "    绑定账号（绑定后可省略参数）\n"
            "/arc unbind                 — 解除绑定\n"
            "/arc info [用户]            — 玩家信息\n"
            "/arc recent [用户]          — 最近游玩（附曲绘）\n"
            "/arc best [用户]            — Best30（合成图）\n"
            "/arc b30 [用户]             — 同上\n"
            "/arc song <曲目> [难度]     — 某曲目最高分\n"
            "\n难度: pst / prs / ftr / byd"
        )
        yield event.plain_result(text)

    # 默认触发
    @filter.command("arc")
    async def arc_default(self, event: AstrMessageEvent):
        async for msg in self.arc_help(event):
            yield msg
