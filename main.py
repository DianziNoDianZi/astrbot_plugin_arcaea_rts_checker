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

# 配置与数据路径
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
BIND_FILE = os.path.join(DATA_DIR, "bind.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# 默认配置
DEFAULT_CONFIG = {
    "api_url": "",
    "api_token": "",
    "timeout": 15,
    "cover_url_template": "https://cdn.arcaea.lowiro.com/cover/{song_id}_{diff}.jpg",
}

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

# 难度对应的颜色（RGB）
DIFFICULTY_COLORS = {
    0: (120, 180, 120),  # PST - 绿
    1: (230, 160, 80),  # PRS - 橙
    2: (180, 100, 200),  # FTR - 紫
    3: (90, 120, 220),  # BYD - 蓝
}


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


def _format_ptt(ptt: float) -> str:
    if ptt is None:
        return "?"
    if abs(float(ptt)) >= 100:
        ptt = float(ptt) / 100
    return f"{float(ptt):.4f}"


def _difficulty_name(diff) -> str:
    if isinstance(diff, int):
        return DIFFICULTY_MAP.get(diff, str(diff))
    return str(diff).upper()


def _normalize_diff_int(diff) -> int:
    if isinstance(diff, int):
        return diff if 0 <= diff <= 3 else 2
    d = str(diff).lower().strip()
    return DIFFICULTY_MAP.get(d, 2)


class ArcaeaPlugin(Star):
    """Arcaea 查分插件"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.config: Dict[str, Any] = _load_json(CONFIG_FILE, DEFAULT_CONFIG)
        self.bindings: Dict[str, str] = _load_json(BIND_FILE, {})
        logger.info("[Arcaea] 插件已加载。")
        if not self.config.get("api_url"):
            logger.warning(
                "[Arcaea] 未配置 api_url，请使用 /arc config <API地址> 配置后再使用查分功能。"
            )

    async def terminate(self):
        pass

    # ---------- 工具方法 ----------
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

    def _api_url(self) -> str:
        url = self.config.get("api_url", "").strip()
        if url.endswith("/"):
            url = url[:-1]
        return url

    def _cover_url(self, song_info: Dict[str, Any], difficulty: int) -> Optional[str]:
        """根据 song_info 和难度构造曲绘 URL。"""
        if not song_info:
            return None
        direct = (
            song_info.get("cover_url")
            or song_info.get("image_url")
            or song_info.get("url")
        )
        if direct:
            return direct
        song_id = song_info.get("id") or song_info.get("song_id")
        if not song_id:
            return None
        template = self.config.get(
            "cover_url_template",
            DEFAULT_CONFIG["cover_url_template"],
        )
        diff_str = {0: "pst", 1: "prs", 2: "ftr", 3: "byd"}.get(difficulty, "ftr")
        return template.format(song_id=song_id, diff=diff_str)

    async def _api_request(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("缺少依赖 httpx，请先执行 pip install httpx")
        base = self._api_url()
        if not base:
            raise RuntimeError(
                "尚未配置 Arcaea API 地址，请先使用 /arc config <API地址> 进行配置。"
            )
        url = f"{base}{endpoint}"
        headers = {}
        token = self.config.get("api_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = self.config.get("timeout", 15)
        logger.info(f"[Arcaea] 请求: {url} params={params}")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _fetch_cover(self, url: str) -> Optional[Image.Image]:
        """异步下载曲绘图片并缓存，返回 PIL.Image；失败返回 None。"""
        if not url or httpx is None:
            return None
        _ensure_dir()
        cache_key = hex(abs(hash(url))) + ".jpg"
        cache_path = os.path.join(CACHE_DIR, cache_key)
        # 命中缓存
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
            # 每累计下载 20 张后触发一次缓存清理
            self._cache_clean_counter = getattr(self, "_cache_clean_counter", 0) + 1
            if self._cache_clean_counter >= 20:
                self._cache_clean_counter = 0
                self._clean_cache()

    def _clean_cache(self) -> None:
        """
        清理缓存目录中超过 7 天的旧文件，防止缓存无限膨胀。
        最多保留 500 个缓存文件（超出时按时间倒序删除最旧的）。
        """
        if not os.path.exists(CACHE_DIR):
            return
        max_age_seconds = 7 * 24 * 60 * 60
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
                if now - mtime > max_age_seconds:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
            # 超出数量限制时，删除最旧的文件
            if len(files) > max_files:
                files.sort(key=lambda x: x[1])
                for fpath, _ in files[: len(files) - max_files]:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
            logger.debug(f"[Arcaea] 缓存清理完成，当前缓存文件数: {len(files)}")
        except Exception as e:
            logger.debug(f"[Arcaea] 缓存清理失败: {e}")

    def _parse_user_identifier(
        self, arg: Optional[str], event: AstrMessageEvent
    ) -> str:
        if arg and arg.strip():
            return arg.strip()
        key = self._get_binder(event)
        if not key:
            raise RuntimeError("无法获取当前会话标识。")
        arc_id = self.bindings.get(key)
        if not arc_id:
            raise RuntimeError(
                "未绑定 Arcaea 账号，请先使用 /arc bind <用户名或9位UID> 绑定。"
            )
        return arc_id

    # ---------- 图片渲染 ----------
    def _get_font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        """尝试加载支持中文的字体，失败时回退到默认字体。"""
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
            return ImageFont.load_default(size)
        except Exception:
            try:
                return ImageFont.load_default()
            except Exception:
                return None

    async def _compose_best30_image(
        self,
        player_name: str,
        player_id: str,
        ptt_avg: Optional[float],
        max_ptt: Optional[float],
        recent_avg: Optional[float],
        records: List[Dict[str, Any]],
        overflow: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """
        合成 Best30 长图并保存到 data/best30_<timestamp>.jpg，返回图片路径。
        返回 None 表示渲染失败。
        """
        if Image is None:
            logger.error(
                "[Arcaea] 缺少 Pillow 依赖，无法合成图片。请先 pip install Pillow"
            )
            return None

        # 尺寸设置
        cols = 3
        card_w, card_h = 540, 200
        gap_x, gap_y = 20, 20
        margin_x, margin_top = 30, 220  # 顶部留玩家信息区
        margin_bottom = 60

        # 每行有多少
        rows = (len(records) + cols - 1) // cols
        total_rows = rows
        overflow_rows = 0
        if overflow:
            # overflow 标题占一行，内容占 ceil(len(overflow)/cols) 行
            overflow_rows = 1 + (len(overflow) + cols - 1) // cols

        # 总高度：顶部信息 + 卡片网格 + overflow + 底部
        total_w = margin_x * 2 + cols * card_w + (cols - 1) * gap_x
        total_h = (
            margin_top
            + total_rows * card_h
            + (total_rows - 1) * gap_y
            + overflow_rows * card_h
            + 40
            + margin_bottom
        )

        # 创建画布 - 深色星空风背景
        bg = Image.new("RGB", (total_w, total_h), (18, 22, 48))
        # 渐变 + 星点
        draw = ImageDraw.Draw(bg)
        # 从上到下渐变：深蓝 -> 深紫
        for y in range(0, total_h, 2):
            t = y / total_h
            r = int(18 + (40 - 18) * t)
            g = int(22 + (22 - 22) * t)
            b = int(48 + (78 - 48) * t)
            draw.rectangle([0, y, total_w, y + 1], fill=(r, g, b))
        # 随机星点（用确定性 hash 避免每次都不同）
        import random as _random

        rnd = _random.Random(42)
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

        white = (255, 255, 255)
        gray = (200, 200, 210)

        # ---------- 顶部信息区 ----------
        y = 30
        # 大标题
        title = "ARCAEA  Player Bests"
        draw.text((margin_x, y), title, fill=white, font=font_title)
        y += 58

        # 玩家名 + ID
        name_text = player_name
        id_text = f"ID: {player_id}"
        draw.text((margin_x, y), name_text, fill=white, font=font_score)
        draw.text((margin_x, y + 38), id_text, fill=gray, font=font_small)

        # 右侧：PTT 数据
        stats = []
        if ptt_avg is not None:
            stats.append(
                (
                    "BEST 30 AVG.",
                    f"{float(ptt_avg):.4f}"
                    if float(ptt_avg) < 100
                    else f"{float(ptt_avg) / 100:.4f}",
                )
            )
        if max_ptt is not None:
            stats.append(
                (
                    "MAX POTENTIAL",
                    f"{float(max_ptt):.4f}"
                    if float(max_ptt) < 100
                    else f"{float(max_ptt) / 100:.4f}",
                )
            )
        if recent_avg is not None:
            stats.append(
                (
                    "RECENT TOP10 AVG.",
                    f"{float(recent_avg):.4f}"
                    if float(recent_avg) < 100
                    else f"{float(recent_avg) / 100:.4f}",
                )
            )

        right_x = total_w - margin_x
        cur_y = 50
        for label, val in stats:
            # 右对齐
            val_bbox = draw.textbbox((0, 0), val, font=font_title)
            label_bbox = draw.textbbox((0, 0), label, font=font_small)
            val_w = val_bbox[2] - val_bbox[0]
            label_w = label_bbox[2] - label_bbox[0]
            draw.text(
                (right_x - val_w, cur_y), val, fill=(255, 220, 150), font=font_title
            )
            draw.text(
                (right_x - label_w, cur_y + 48), label, fill=gray, font=font_small
            )
            cur_y += 90
            if cur_y > 200:
                break

        # 分隔线
        draw.rectangle(
            [margin_x, 200, total_w - margin_x, 202],
            fill=(
                120,
                120,
                180,
            ),
        )

        # ---------- 下载所有曲绘（并行简单处理） ----------
        cover_images: List[Optional[Image.Image]] = [None] * len(records)
        for idx, rec in enumerate(records):
            song = rec.get("song_info") or {}
            diff_int = _normalize_diff_int(rec.get("difficulty", 2))
            cover_url = self._cover_url(song, diff_int)
            if cover_url:
                cover_images[idx] = await self._fetch_cover(cover_url)

        # ---------- 绘制每张卡片 ----------
        for idx, rec in enumerate(records):
            row = idx // cols
            col = idx % cols
            x = margin_x + col * (card_w + gap_x)
            y = margin_top + row * (card_h + gap_y)
            await self._draw_card(
                bg,
                draw,
                x,
                y,
                card_w,
                card_h,
                rank=idx + 1,
                record=rec,
                cover=cover_images[idx],
                fonts={
                    "rank": font_rank,
                    "score": font_score,
                    "ptt": font_ptt,
                    "song": font_song,
                    "small": font_small,
                },
            )

        # ---------- Overflow 区 ----------
        if overflow:
            # overflow 标题
            title_y = margin_top + total_rows * card_h + (total_rows - 1) * gap_y + 20
            draw.text(
                (margin_x, title_y), "Overflow", fill=(255, 220, 150), font=font_score
            )
            draw.rectangle(
                [margin_x, title_y + 40, total_w - margin_x, title_y + 42],
                fill=(120, 120, 180),
            )

            # 下载 overflow 曲绘
            overflow_covers = []
            for rec in overflow:
                song = rec.get("song_info") or {}
                diff_int = _normalize_diff_int(rec.get("difficulty", 2))
                cover_url = self._cover_url(song, diff_int)
                overflow_covers.append(
                    await self._fetch_cover(cover_url) if cover_url else None
                )

            start_y = title_y + 50
            for idx, rec in enumerate(overflow):
                row = idx // cols
                col = idx % cols
                x = margin_x + col * (card_w + gap_x)
                y = start_y + row * (card_h + gap_y)
                await self._draw_card(
                    bg,
                    draw,
                    x,
                    y,
                    card_w,
                    card_h,
                    rank=idx + 31,
                    record=rec,
                    cover=overflow_covers[idx],
                    fonts={
                        "rank": font_rank,
                        "score": font_score,
                        "ptt": font_ptt,
                        "song": font_song,
                        "small": font_small,
                    },
                    overflow_card=True,
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

    async def _draw_card(
        self,
        bg: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        rank: int,
        record: Dict[str, Any],
        cover: Optional[Image.Image],
        fonts: Dict[str, ImageFont.ImageFont],
        overflow_card: bool = False,
    ):
        """在 bg 的 (x, y) 位置绘制一张曲目卡片。"""
        song = record.get("song_info") or {}
        diff_int = _normalize_diff_int(record.get("difficulty", 2))
        diff_color = DIFFICULTY_COLORS.get(diff_int, (180, 180, 180))
        song_name = song.get("title") or song.get("name") or "Unknown"
        score = record.get("score", 0)
        ptt_val = record.get("rating")
        if ptt_val is not None and float(ptt_val) > 100:
            ptt_val = float(ptt_val) / 100
        shiny = record.get("shiny_perfect_count", "?")
        near = record.get("near_count", "?")
        miss = record.get("miss_count", "?")

        # 1. 绘制卡片底板
        card = Image.new("RGB", (w, h), diff_color)
        # 2. 曲绘作为背景（左半），并做暗化处理
        cover_region_w = int(w * 0.5)  # 左半放曲绘
        if cover is not None:
            try:
                cover_resized = cover.convert("RGB").resize(
                    (cover_region_w, h), Image.LANCZOS
                )
                # 覆盖左侧
                card.paste(cover_resized, (0, 0))
                # 在上面叠一层半透明暗色蒙版，让文字更清晰
                dark_overlay = Image.new("RGBA", (cover_region_w, h), (0, 0, 0, 110))
                card.paste(dark_overlay, (0, 0), mask=dark_overlay)
            except Exception:
                pass
        else:
            # 没有曲绘：直接用纯色
            dark = Image.new(
                "RGB",
                (cover_region_w, h),
                (
                    max(0, diff_color[0] - 60),
                    max(0, diff_color[1] - 60),
                    max(0, diff_color[2] - 60),
                ),
            )
            card.paste(dark, (0, 0))

        # 右侧信息区：用稍亮的颜色填充
        right_bg = Image.new(
            "RGB",
            (w - cover_region_w, h),
            (
                max(0, diff_color[0] - 20),
                max(0, diff_color[1] - 20),
                max(0, diff_color[2] - 20),
            ),
        )
        card.paste(right_bg, (cover_region_w, 0))
        # 再叠一层暗色半透明让文字更可读
        dark_right = Image.new("RGBA", (w - cover_region_w, h), (0, 0, 0, 100))
        card.paste(dark_right, (cover_region_w, 0), mask=dark_right)

        # 3. 在左侧曲绘上：左上显示排名，下方显示曲目名
        cdraw = ImageDraw.Draw(card)

        # 排名标签（左上小矩形）
        rank_text = f"#{rank}"
        rank_bbox = cdraw.textbbox((0, 0), rank_text, font=fonts["rank"])
        rank_text_w = rank_bbox[2] - rank_bbox[0]
        rank_text_h = rank_bbox[3] - rank_bbox[1]
        pad_r = 10
        cdraw.rectangle(
            [10, 10, 10 + rank_text_w + pad_r * 2, 10 + rank_text_h + pad_r],
            fill=(0, 0, 0, 160),
        )
        cdraw.text(
            (10 + pad_r, 10 + pad_r - 4),
            rank_text,
            fill=(255, 255, 255),
            font=fonts["rank"],
        )

        # 曲目名（左下角，截断）
        max_chars = 22
        display_name = (
            song_name
            if len(str(song_name)) <= max_chars
            else str(song_name)[: max_chars - 1] + "…"
        )
        cdraw.text((10, h - 40), display_name, fill=(255, 255, 255), font=fonts["song"])

        # 4. 右侧信息区：难度 + 分数 + PTT + 小统计
        rx = cover_region_w + 16
        ry = 14

        # 难度标签
        diff_name = _difficulty_name(diff_int)
        cdraw.text((rx, ry), diff_name, fill=(255, 255, 255), font=fonts["small"])
        ry += 28

        # 分数（大字）
        score_text = f"{int(score):,}"
        cdraw.text((rx, ry), score_text, fill=(255, 255, 255), font=fonts["score"])
        ry += 34

        # PTT（橙色）
        if ptt_val is not None:
            ptt_text = f"PTT {float(ptt_val):.4f}"
        else:
            ptt_text = "PTT ?"
        cdraw.text((rx, ry), ptt_text, fill=(255, 220, 150), font=fonts["ptt"])
        ry += 32

        # 小统计（shiny / near / miss）
        stats_line = f"Pure {shiny}   Far {near}   Lost {miss}"
        # 截断避免过长
        if len(stats_line) > 28:
            stats_line = f"P {shiny} F {near} L {miss}"
        cdraw.text((rx, ry), stats_line, fill=(220, 220, 220), font=fonts["small"])

        if overflow_card:
            # 在右上角打一个 "OF" 标记
            cdraw.rectangle([w - 50, 10, w - 10, 38], fill=(200, 80, 80))
            cdraw.text((w - 45, 12), "OF", fill=(255, 255, 255), font=fonts["small"])

        # 卡片外边框
        cdraw.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255), width=2)

        bg.paste(card, (x, y))

    async def _compose_recent_image(
        self,
        player_name: str,
        player_id: str,
        ptt: Optional[float],
        records: List[Dict[str, Any]],
    ) -> Optional[str]:
        """合成最近游玩记录的长图。"""
        if Image is None:
            return None

        cols = 3
        card_w, card_h = 540, 200
        gap_x, gap_y = 20, 20
        margin_x, margin_top = 30, 180
        margin_bottom = 60
        rows = (len(records) + cols - 1) // cols
        total_w = margin_x * 2 + cols * card_w + (cols - 1) * gap_x
        total_h = margin_top + rows * card_h + (rows - 1) * gap_y + margin_bottom

        bg = Image.new("RGB", (total_w, total_h), (20, 24, 50))
        draw = ImageDraw.Draw(bg)
        # 渐变
        for y in range(0, total_h, 2):
            t = y / total_h
            r = int(20 + (40 - 20) * t)
            b = int(50 + (80 - 50) * t)
            draw.rectangle([0, y, total_w, y + 1], fill=(r, 24, b))

        font_title = self._get_font(40, True)
        font_small = self._get_font(20)
        font_rank = self._get_font(34, True)
        font_score = self._get_font(28, True)
        font_ptt = self._get_font(22, True)
        font_song = self._get_font(22)

        # 顶部
        draw.text(
            (margin_x, 30), "ARCAEA  Recent", fill=(255, 255, 255), font=font_title
        )
        draw.text((margin_x, 80), player_name, fill=(255, 255, 255), font=font_score)
        draw.text(
            (margin_x, 112), f"ID: {player_id}", fill=(200, 200, 210), font=font_small
        )
        if ptt is not None:
            p = float(ptt) / 100 if float(ptt) > 100 else float(ptt)
            # 放在右侧
            ptt_text = f"{p:.4f}"
            bb = draw.textbbox((0, 0), ptt_text, font=font_title)
            draw.text(
                (total_w - margin_x - (bb[2] - bb[0]), 70),
                ptt_text,
                fill=(255, 220, 150),
                font=font_title,
            )
            label_bbox = draw.textbbox((0, 0), "Potential", font=font_small)
            draw.text(
                (total_w - margin_x - (label_bbox[2] - label_bbox[0]), 50),
                "Potential",
                fill=(200, 200, 210),
                font=font_small,
            )

        draw.rectangle([margin_x, 160, total_w - margin_x, 162], fill=(120, 120, 180))

        # 下载曲绘
        covers = []
        for rec in records:
            song = rec.get("song_info") or {}
            diff_int = _normalize_diff_int(rec.get("difficulty", 2))
            url = self._cover_url(song, diff_int)
            covers.append(await self._fetch_cover(url) if url else None)

        for idx, rec in enumerate(records):
            row = idx // cols
            col = idx % cols
            x = margin_x + col * (card_w + gap_x)
            y = margin_top + row * (card_h + gap_y)
            await self._draw_card(
                bg,
                draw,
                x,
                y,
                card_w,
                card_h,
                rank=idx + 1,
                record=rec,
                cover=covers[idx],
                fonts={
                    "rank": font_rank,
                    "score": font_score,
                    "ptt": font_ptt,
                    "song": font_song,
                    "small": font_small,
                },
            )

        ts = int(time.time())
        save_path = os.path.join(DATA_DIR, f"recent_{ts}.jpg")
        try:
            bg.save(save_path, format="JPEG", quality=92)
            return save_path
        except Exception as e:
            logger.exception(f"[Arcaea] recent 图片保存失败: {e}")
            return None

    # ---------- 指令 ----------
    @filter.command("arc bind")
    async def arc_bind(self, event: AstrMessageEvent, arc_id: str):
        """绑定 Arcaea 账号。用法: /arc bind <用户名或9位UID>"""
        if not arc_id or not arc_id.strip():
            yield event.plain_result("用法: /arc bind <用户名或9位UID>")
            return
        key = self._get_binder(event)
        if not key:
            yield event.plain_result("无法获取会话信息，绑定失败。")
            return
        self.bindings[key] = arc_id.strip()
        _save_json(BIND_FILE, self.bindings)
        yield event.plain_result(f"已绑定 Arcaea 账号: {arc_id.strip()}")

    @filter.command("arc unbind")
    async def arc_unbind(self, event: AstrMessageEvent):
        """解除绑定。用法: /arc unbind"""
        key = self._get_binder(event)
        if key and key in self.bindings:
            removed = self.bindings.pop(key)
            _save_json(BIND_FILE, self.bindings)
            yield event.plain_result(f"已解除绑定: {removed}")
        else:
            yield event.plain_result("当前会话没有绑定任何 Arcaea 账号。")

    @filter.command("arc config")
    async def arc_config(
        self,
        event: AstrMessageEvent,
        api_url: str = "",
        token: str = "",
        cover_url: str = "",
    ):
        """配置 Arcaea API。用法: /arc config <API地址> [token] [cover_url]"""
        if not api_url or not api_url.strip():
            current = self._api_url() or "(未配置)"
            cover = self.config.get(
                "cover_url_template", DEFAULT_CONFIG["cover_url_template"]
            )
            yield event.plain_result(
                f"当前 API 地址: {current}\n"
                f"当前曲绘 URL: {cover}\n\n"
                f"用法: /arc config <API地址> [token] [cover_url]"
            )
            return
        self.config["api_url"] = api_url.strip()
        if token and token.strip():
            self.config["api_token"] = token.strip()
        if cover_url and cover_url.strip():
            self.config["cover_url_template"] = cover_url.strip()
        _save_json(CONFIG_FILE, self.config)
        msg = f"已更新 Arcaea API 配置: {self.config['api_url']}"
        if cover_url:
            msg += f"\n曲绘 URL: {self.config['cover_url_template']}"
        yield event.plain_result(msg)

    @filter.command("arc help")
    async def arc_help(self, event: AstrMessageEvent):
        text = (
            "===== Arcaea 查分帮助 =====\n"
            "/arc bind <用户名/UID>      - 绑定账号\n"
            "/arc unbind                 - 解除绑定\n"
            "/arc config <url> [token] [cover_url]\n"
            "                            - 配置 API\n"
            "/arc info [用户名/UID]      - 玩家信息\n"
            "/arc recent [用户名/UID]    - 最近游玩（合成图）\n"
            "/arc best [用户名/UID]      - Best 30（合成图）\n"
            "/arc b30 [用户名/UID]       - Best 30（别名）\n"
            "/arc song <曲目名> [难度]   - 某曲目最高分\n"
            "\n难度: pst / prs / ftr / byd\n"
            "已绑定账号后，用户名/UID 可省略。"
        )
        yield event.plain_result(text)

    @filter.command("arc info")
    async def arc_info(self, event: AstrMessageEvent, user: str = ""):
        try:
            uid = self._parse_user_identifier(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            params = {"user": uid}
            if uid.isdigit() and len(uid) == 9:
                params = {"usercode": uid}
            data = await self._api_request("/api/user/info", params=params)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            content = data.get("content", data.get("data", data))
            acc = content.get("account_info", {})
            name = acc.get("name", "?")
            code = acc.get("code", "?")
            ptt = acc.get("potential")
            ptt_float = None
            if ptt is not None:
                ptt_float = float(ptt) / 100 if float(ptt) > 100 else float(ptt)
            character = acc.get("character")
            join_date = acc.get("join_date")

            recent = content.get("recent_score") or []
            recent = recent if isinstance(recent, list) else [recent]

            lines = [
                "=== Arcaea 玩家信息 ===",
                f"用户名: {name}",
                f"UID: {code}",
            ]
            if ptt_float is not None:
                lines.append(f"PTT: {ptt_float:.4f}")
            if character is not None:
                lines.append(f"搭档: {character}")
            if join_date:
                lines.append(f"注册时间: {join_date}")

            if recent and recent[0]:
                rec = recent[0]
                song = rec.get("song_info") or {}
                title = song.get("title") or song.get("name") or "?"
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = rec.get("rating")
                if ptt_v is not None and float(ptt_v) > 100:
                    ptt_v = float(ptt_v) / 100
                lines.append("")
                lines.append("最近 1 次游玩:")
                line = f"[{_difficulty_name(diff)}] {title} — {score}"
                if ptt_v is not None:
                    line += f" (PTT {float(ptt_v):.4f})"
                lines.append(line)

                # 附带曲绘
                diff_int = _normalize_diff_int(diff)
                cover_url = self._cover_url(song, diff_int)
                if cover_url:
                    img = await self._fetch_cover(cover_url)
                    if img is not None:
                        # 保存到临时文件发送
                        ts = int(time.time())
                        cover_path = os.path.join(DATA_DIR, f"recent_cover_{ts}.jpg")
                        try:
                            img.save(cover_path, format="JPEG", quality=92)
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
            yield event.plain_result(f"解析返回数据失败: {e}\n原始数据: {data}")

    @filter.command("arc recent")
    async def arc_recent(self, event: AstrMessageEvent, user: str = ""):
        try:
            uid = self._parse_user_identifier(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            params = {"user": uid, "recent": 10}
            if uid.isdigit() and len(uid) == 9:
                params = {"usercode": uid, "recent": 10}
            data = await self._api_request("/api/user/info", params=params)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            content = data.get("content", data.get("data", data))
            acc = content.get("account_info", {}) or {}
            name = acc.get("name", uid)
            ptt = acc.get("potential")
            ptt_float = None
            if ptt is not None:
                ptt_float = float(ptt) / 100 if float(ptt) > 100 else float(ptt)
            recent = (
                content.get("recent_score", []) if isinstance(content, dict) else []
            )
            if not isinstance(recent, list):
                recent = [recent] if recent else []

            if not recent:
                yield event.plain_result(f"{name} 没有最近的游玩记录。")
                return

            # 尝试合成图片
            img_path = await self._compose_recent_image(
                player_name=name,
                player_id=str(acc.get("code", uid)),
                ptt=ptt,
                records=recent,
            )

            if img_path:
                # 输出文本摘要 + 图片
                ptt_line = ""
                if ptt_float is not None:
                    ptt_line = f"\nPotential: {ptt_float:.4f}"
                yield event.chain_result(
                    [
                        Comp.Plain(
                            f"[Arcaea Recent]\n玩家: {name}{ptt_line}\n最近 {len(recent)} 条游玩记录"
                        ),
                        Comp.Image.fromFileSystem(img_path),
                    ]
                )
                return

            # 回退：逐条发送
            for idx, rec in enumerate(recent, 1):
                song = rec.get("song_info") or {}
                title = song.get("title") or song.get("name") or "?"
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = rec.get("rating")
                if ptt_v is not None and float(ptt_v) > 100:
                    ptt_v = float(ptt_v) / 100
                text = f"{idx}. [{_difficulty_name(diff)}] {title} — {score}"
                if ptt_v is not None:
                    text += f" (PTT {float(ptt_v):.4f})"
                yield event.plain_result(text)
        except Exception as e:
            logger.exception(f"[Arcaea] recent 解析失败: {e}")
            yield event.plain_result(f"解析返回数据失败: {e}\n原始数据: {data}")

    @filter.command("arc best")
    async def arc_best(self, event: AstrMessageEvent, user: str = ""):
        try:
            uid = self._parse_user_identifier(user, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        try:
            params = {"user": uid, "overflow": 30}
            if uid.isdigit() and len(uid) == 9:
                params = {"usercode": uid, "overflow": 30}
            data = await self._api_request("/api/user/best", params=params)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            content = data.get("content", data.get("data", data))
            records = content.get("records", [])
            overflow = content.get("overflow", []) or []
            ptt_avg = content.get("best30_avg")
            ptt_max = content.get("max_ptt") or content.get("max_potential")
            recent_avg = content.get("recent10_avg")
            acc = content.get("account_info", {}) or {}
            name = acc.get("name", uid)
            player_code = str(acc.get("code", uid))

            if not records or not isinstance(records, list):
                yield event.plain_result(f"{name} 暂无 best30 数据。\n原始数据: {data}")
                return

            # 先给一个提示
            yield event.plain_result(
                f"[Arcaea Bests] 正在为 {name} 合成 Best30 图片，请稍候…"
            )

            img_path = await self._compose_best30_image(
                player_name=name,
                player_id=player_code,
                ptt_avg=ptt_avg,
                max_ptt=ptt_max,
                recent_avg=recent_avg,
                records=records[:30],
                overflow=overflow if isinstance(overflow, list) else [],
            )

            if img_path:
                ptt_lines = []
                if ptt_avg is not None:
                    pa = (
                        float(ptt_avg) / 100 if float(ptt_avg) > 100 else float(ptt_avg)
                    )
                    ptt_lines.append(f"B30 平均: {pa:.4f}")
                if recent_avg is not None:
                    ra = (
                        float(recent_avg) / 100
                        if float(recent_avg) > 100
                        else float(recent_avg)
                    )
                    ptt_lines.append(f"R10 平均: {ra:.4f}")
                if ptt_max is not None:
                    pm = (
                        float(ptt_max) / 100 if float(ptt_max) > 100 else float(ptt_max)
                    )
                    ptt_lines.append(f"最大 PTT: {pm:.4f}")
                summary = f"[Arcaea Bests] 玩家: {name}\n" + "\n".join(ptt_lines)
                yield event.chain_result(
                    [
                        Comp.Plain(summary),
                        Comp.Image.fromFileSystem(img_path),
                    ]
                )
                return

            # 回退：文本列表
            lines = [f"=== {name} 的 Best 30 ==="]
            if ptt_avg is not None:
                pa = float(ptt_avg) / 100 if float(ptt_avg) > 100 else float(ptt_avg)
                lines.append(f"B30 平均 PTT: {pa:.4f}")
            if recent_avg is not None:
                ra = (
                    float(recent_avg) / 100
                    if float(recent_avg) > 100
                    else float(recent_avg)
                )
                lines.append(f"R10 平均 PTT: {ra:.4f}")
            lines.append("")
            for idx, rec in enumerate(records[:30], 1):
                song = rec.get("song_info") or {}
                title = song.get("title") or song.get("name") or "?"
                diff = rec.get("difficulty", "?")
                score = rec.get("score", "?")
                ptt_v = rec.get("rating")
                if ptt_v is not None and float(ptt_v) > 100:
                    ptt_v = float(ptt_v) / 100
                line = f"{idx:>2}. [{_difficulty_name(diff)}] {title} — {score}"
                if ptt_v is not None:
                    line += f" (PTT {float(ptt_v):.4f})"
                lines.append(line)
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception(f"[Arcaea] best 解析失败: {e}")
            yield event.plain_result(f"解析返回数据失败: {e}\n原始数据: {data}")

    @filter.command("arc b30")
    async def arc_b30(self, event: AstrMessageEvent, user: str = ""):
        """查询 Best 30（等价 arc best）"""
        async for msg in self.arc_best(event, user):
            yield msg

    @filter.command("ab30")
    async def arc_ab30(self, event: AstrMessageEvent, user: str = ""):
        """ab30 别名：Best30 合成图"""
        async for msg in self.arc_best(event, user):
            yield msg

    @filter.command("arc song")
    async def arc_song(
        self, event: AstrMessageEvent, song_name: str = "", difficulty: str = ""
    ):
        if not song_name or not song_name.strip():
            yield event.plain_result("用法: /arc song <曲目名> [难度]")
            return

        try:
            uid = self._parse_user_identifier(None, event)
        except RuntimeError as e:
            yield event.plain_result(str(e))
            return

        diff_int = None
        if difficulty:
            d = difficulty.lower().strip()
            diff_int = DIFFICULTY_MAP.get(d)
            if diff_int is None:
                yield event.plain_result("不支持的难度标识。支持: pst/prs/ftr/byd")
                return

        try:
            params = {"user": uid, "songname": song_name.strip()}
            if uid.isdigit() and len(uid) == 9:
                params = {"usercode": uid, "songname": song_name.strip()}
            if diff_int is not None:
                params["difficulty"] = diff_int
            data = await self._api_request("/api/user/score", params=params)
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")
            return

        try:
            content = data.get("content", data.get("data", data))
            records = content.get("records", []) if isinstance(content, dict) else []
            if not records or not isinstance(records, list):
                yield event.plain_result(
                    f"未找到《{song_name}》的记录。\n原始数据: {data}"
                )
                return

            for rec in records[:5]:
                song = rec.get("song_info") or {}
                diff = rec.get("difficulty", "?")
                diff_i = _normalize_diff_int(diff)
                score = rec.get("score", "?")
                ptt_v = rec.get("rating")
                if ptt_v is not None and float(ptt_v) > 100:
                    ptt_v = float(ptt_v) / 100
                rating = song.get("rating")
                title = song.get("title") or song.get("name") or song_name

                text = f"[{_difficulty_name(diff)}] {title} — {score}"
                if ptt_v is not None:
                    text += f" (PTT {float(ptt_v):.4f})"
                if rating is not None:
                    text += f"\n曲目定数: {float(rating):.1f}"

                cover_url = self._cover_url(song, diff_i)
                if cover_url:
                    cover = await self._fetch_cover(cover_url)
                    if cover is not None:
                        ts = int(time.time())
                        cover_path = os.path.join(DATA_DIR, f"song_cover_{ts}.jpg")
                        try:
                            cover.save(cover_path, format="JPEG", quality=92)
                            yield event.chain_result(
                                [
                                    Comp.Plain(text),
                                    Comp.Image.fromFileSystem(cover_path),
                                ]
                            )
                            continue
                        except Exception:
                            pass
                yield event.plain_result(text)
        except Exception as e:
            logger.exception(f"[Arcaea] song 解析失败: {e}")
            yield event.plain_result(f"解析返回数据失败: {e}\n原始数据: {data}")

    # 默认触发
    @filter.command("arc")
    async def arc_default(self, event: AstrMessageEvent):
        async for msg in self.arc_help(event):
            yield msg
