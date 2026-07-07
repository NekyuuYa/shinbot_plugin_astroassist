"""Command handlers for 晴天钟, 设置位置, 雷达 and 台风."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from shinbot.schema.elements import MessageElement

from .forecast import fetch_forecast
from .geo import amap_geocode
from .models import LocationData
from .radar import download_radar_image, fetch_radar, fetch_radar_gif
from .storage import LocationStore
from .typhoon import (
    NmcTyphoonNewsProvider,
    TyphoonProvider,
    TyphoonTrackImage,
    TyphoonUnavailable,
    download_typhoon_track_image,
    format_typhoon_detail,
    format_typhoon_help,
    format_typhoon_list,
    parse_typhoon_args,
)

if TYPE_CHECKING:
    from shinbot.core.dispatch.message_context import MessageContext

try:
    from shinbot_plugin_renderkit import RenderOptions, render_template_to_file

    _RENDERKIT_AVAILABLE = True
except ImportError:
    _RENDERKIT_AVAILABLE = False

_LOG = logging.getLogger(__name__)

_HELP_TEXT = (
    "🔭 AstroAssist 晴天钟助手 | 指南\n"
    "━━━━━━━━━━━━━━━\n"
    "📍 1. 设置观测位置\n"
    "!设置位置 [地名] → 自动纠偏\n"
    "!设置位置 -c [纬度] [经度] → 手动坐标 (WGS-84)\n"
    "  (每个群聊或私聊可独立设置默认位置)\n\n"
    "🌤️ 2. 获取看板预报\n"
    "!晴天钟 → 查看默认位置3天预报\n"
    "!晴天钟 [地名] → 临时查询某地天气\n"
    "!晴天钟 -d [天数] → 指定预报长度(1-7天)\n"
    "!晴天钟 -n → 过滤夜间窗口(18点至06点)\n\n"
    "📡 3. 雷达回波\n"
    "!雷达 → 获取最新全国雷达回波拼图\n"
    "!雷达 华北 → 区域拼图 (华北/华东/华南/...)\n"
    "!雷达 北京 → 单站雷达 (省份或城市名)\n"
    "!雷达动图 → 全国雷达回波动画 (~2小时)\n\n"
    "🌀 4. 台风路径\n"
    "!台风 → 查询中央气象台最新台风快讯\n"
    "!台风 <名称或编号> → 查询当前快讯详情；若有对应路径页会附带路径预报图\n"
    "  (数据源：中央气象台 NMC 台风快讯与路径预报图)\n\n"
    "📊 5. 核心指标说明\n"
    "• 视宁度 (Seeing): 大气抖动，越小越稳\n"
    "• 透明度 (Transparency): 大气透亮感\n"
    "• 露点风险: 红色代表极易结露，需保护器材\n"
    "• 云量方块: 内部白色填充代表天空遮挡度\n\n"
    "💡 示例：!晴天钟 -d 1 -n 西藏阿里\n"
)


# ------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------

_CMD_RE = re.compile(r"^(\S+)\s*(.*)?$", re.DOTALL)


def _parse_astro_args(raw: str) -> tuple[int, bool, str | None]:
    """Parse ``-d <days>`` ``-n`` and optional place name.

    Returns ``(days, night_only, target_place)``.
    """
    args = raw.strip().split()
    days = 3
    night_only = False
    place_parts: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-d" and i + 1 < len(args):
            try:
                days = max(1, min(7, int(args[i + 1])))
            except ValueError:
                pass
            i += 2
            continue
        if args[i] == "-n":
            night_only = True
            i += 1
            continue
        place_parts = args[i:]
        break
    place = " ".join(place_parts) if place_parts else None
    return days, night_only, place


# ------------------------------------------------------------------
# Handler registration
# ------------------------------------------------------------------


def register_commands(
    plg: Any,
    config: Any,
    store: LocationStore,
    template_path: Path,
    typhoon_provider: TyphoonProvider | None = None,
) -> None:
    """Register AstroAssist commands on *plg*."""
    provider = typhoon_provider or NmcTyphoonNewsProvider()

    # ---- 晴天钟 ----
    @plg.on_command(
        "晴天钟",
        aliases=["astro", "astroassist"],
        description="获取天文气象看板预报",
        usage="!晴天钟 [-d 天数] [-n] [地名]",
    )
    async def handle_astro(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        args = raw_args.strip()

        # Help
        if args.split()[:1] in (["help"], ["帮助"], ["-h"]):
            await ctx.send(_HELP_TEXT)
            ctx.stop()
            return

        days, night_only, target_place = _parse_astro_args(raw_args)

        # Resolve location
        location: LocationData | None = None
        if target_place:
            if not config.amap_key:
                await ctx.send("❌ 未配置 amap_key，无法解析地名。")
                ctx.stop()
                return
            try:
                lat, lon = await amap_geocode(target_place, config.amap_key)
                location = LocationData(lat=lat, lon=lon, name=target_place)
            except ValueError as exc:
                await ctx.send(f"❌ 临时解析失败: {exc}")
                ctx.stop()
                return
        else:
            location = await store.get(ctx.session_id)
            if not location:
                await ctx.send("❌ 请先用 `!设置位置 [地名]` 设置默认观测位置。")
                ctx.stop()
                return

        # Check RenderKit
        if not _RENDERKIT_AVAILABLE:
            await ctx.send("❌ 渲染引擎 (RenderKit) 未安装，无法生成看板图片。")
            ctx.stop()
            return

        # Fetch & process
        try:
            render_data = await fetch_forecast(
                location.lat, location.lon, days=days, night_only=night_only
            )
            render_data.location_name = location.name
        except Exception as exc:
            _LOG.exception("AstroAssist forecast error")
            await ctx.send(f"❌ 预报获取异常: {exc}")
            ctx.stop()
            return

        # Render to PNG
        try:
            result = await render_template_to_file(
                template_path,
                data={
                    "lat": render_data.lat,
                    "lon": render_data.lon,
                    "location_name": render_data.location_name,
                    "ref_time": render_data.ref_time,
                    "rows": render_data.rows,
                    "theme_mode": render_data.theme_mode,
                    "model_name": render_data.model_name,
                },
                output_dir=plg.data_dir,
                options=RenderOptions(
                    width=700,
                    height=800,
                    device_scale_factor=3.0,
                    full_page=True,
                ),
                cache=False,
            )
            await ctx.send([MessageElement.img(str(result.path))])
        except Exception as exc:
            _LOG.exception("AstroAssist render error")
            await ctx.send(f"❌ 渲染异常: {exc}")

        ctx.stop()

    # ---- 设置位置 ----
    @plg.on_command(
        "设置位置",
        aliases=["setloc"],
        description="设置默认观测位置",
        usage="!设置位置 [地名] 或 !设置位置 -c [纬度] [经度]",
    )
    async def handle_set_location(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        args = raw_args.strip().split()
        if not args:
            await ctx.send(
                "❌ 请提供地名或坐标。用法: `!设置位置 [地名]` 或 `!设置位置 -c [纬度] [经度]`"
            )
            ctx.stop()
            return

        try:
            if args[0].lower() == "-c" and len(args) >= 3:
                lat = float(args[1])
                lon = float(args[2])
                loc = LocationData(lat=lat, lon=lon, name=f"坐标({lat},{lon})")
            else:
                if not config.amap_key:
                    await ctx.send("❌ 未配置 amap_key，无法解析地名。")
                    ctx.stop()
                    return
                place = " ".join(args)
                lat, lon = await amap_geocode(place, config.amap_key)
                loc = LocationData(lat=lat, lon=lon, name=place)

            await store.put(ctx.session_id, loc)
            await ctx.send(f"📍 位置已设置为：{loc.name}")
        except Exception as exc:
            await ctx.send(f"❌ 失败: {exc}")

        ctx.stop()

    # ---- 雷达 ----
    @plg.on_command(
        "雷达",
        aliases=["radar"],
        description="获取最新雷达回波图 (全国/区域/单站)",
        usage="!雷达 [区域或城市名]",
    )
    async def handle_radar(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_radar_static(ctx, raw_args.strip(), plg)
        ctx.stop()

    # ---- 雷达动图 ----
    @plg.on_command(
        "雷达动图",
        aliases=["radargif"],
        description="获取雷达回波动图 (~2小时动画)",
        usage="!雷达动图 [区域或城市名]",
    )
    async def handle_radar_gif(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_radar_gif(ctx, raw_args.strip(), plg)
        ctx.stop()

    # ---- 台风 ----
    @plg.on_command(
        "台风",
        aliases=["typhoon"],
        description="查询台风实时路径",
        usage="!台风 [list|名称或编号]",
    )
    async def handle_typhoon(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        parsed = parse_typhoon_args(raw_args)

        if parsed.action == "help":
            await ctx.send(format_typhoon_help())
            ctx.stop()
            return

        try:
            if parsed.action == "list":
                message = format_typhoon_list(await provider.list_active())
                await ctx.send(message)
            else:
                detail = await provider.get_detail(parsed.query)
                if isinstance(detail, TyphoonUnavailable):
                    image_sent = await _send_typhoon_response(
                        ctx,
                        None,
                        parsed.query,
                        provider,
                        plg,
                    )
                    if not image_sent:
                        await ctx.send(format_typhoon_detail(detail))
                else:
                    await _send_typhoon_response(
                        ctx,
                        format_typhoon_detail(detail),
                        detail.summary.name or parsed.query,
                        provider,
                        plg,
                    )
        except Exception as exc:
            _LOG.exception("AstroAssist typhoon query error")
            message = f"❌ 台风数据查询失败: {exc}"
            await ctx.send(message)

        ctx.stop()


async def _handle_radar_static(
    ctx: MessageContext, query: str, plg: Any,
) -> None:
    """Send the latest single radar frame as PNG."""
    try:
        url, obs_time, label = await fetch_radar(query)
    except Exception as exc:
        _LOG.exception("AstroAssist radar fetch error")
        await ctx.send(f"❌ 雷达数据获取失败: {exc}")
        return

    img_path = Path(plg.data_dir) / "radar_latest.png"
    try:
        await download_radar_image(url, img_path)
    except Exception as exc:
        _LOG.exception("AstroAssist radar download error")
        await ctx.send(f"❌ 雷达图下载失败: {exc}")
        return

    tag = "📡" if "全国" in label or "华" in label else "📍"
    msg = f"{tag} {label}雷达回波"
    if obs_time:
        msg += f"  ({obs_time})"
    await ctx.send(msg)
    await ctx.send([MessageElement.img(str(img_path))])


async def _handle_radar_gif(
    ctx: MessageContext, query: str, plg: Any,
) -> None:
    """Send an animated radar echo GIF (~2 h history)."""
    try:
        gif_bytes, newest, oldest, label = await fetch_radar_gif(query)
    except Exception as exc:
        _LOG.exception("AstroAssist radar GIF error")
        await ctx.send(f"❌ 雷达动图生成失败: {exc}")
        return

    gif_path = Path(plg.data_dir) / "radar_animated.gif"
    gif_path.write_bytes(gif_bytes)

    tag = "📡" if "全国" in label or "华" in label else "📍"
    time_range = f"{oldest} → {newest}" if newest and oldest else newest
    msg = f"{tag} {label}雷达回波动图"
    if time_range:
        msg += f"  ({time_range})"
    await ctx.send(msg)
    await ctx.send([MessageElement.img(str(gif_path))])


async def _send_typhoon_response(
    ctx: MessageContext,
    text: str | None,
    query: str,
    provider: TyphoonProvider,
    plg: Any,
) -> bool:
    """Send typhoon text and track image, preferring folded chat records."""
    try:
        payload = await _prepare_typhoon_track_image(query, provider, plg)
    except Exception as exc:
        _LOG.exception("AstroAssist typhoon track image download error")
        if text:
            await ctx.send(text)
        await ctx.send(f"⚠️ 台风路径图下载失败: {exc}")
        return bool(text)

    if payload is None:
        if text:
            await ctx.send(text)
            return True
        return False

    image, img_path = payload
    caption = _format_typhoon_track_caption(image)
    if await _send_typhoon_forward_message(ctx, text, caption, img_path):
        return True

    if text:
        await ctx.send(text)
    await ctx.send(caption)
    await ctx.send([MessageElement.img(str(img_path))])
    return True


async def _prepare_typhoon_track_image(
    query: str,
    provider: TyphoonProvider,
    plg: Any,
) -> tuple[TyphoonTrackImage, Path] | None:
    """Download the latest NMC typhoon path forecast image when available."""
    if not hasattr(provider, "get_track_image"):
        return None
    image = await provider.get_track_image(query)
    if isinstance(image, TyphoonUnavailable):
        _LOG.info("AstroAssist typhoon track image unavailable: %s", image.message)
        return None

    suffix = Path(image.url.split("?", 1)[0]).suffix.lower() or ".jpg"
    filename_label = _safe_filename_piece(image.name or query or "track")
    img_path = Path(plg.data_dir) / f"typhoon_track_{filename_label}_{uuid4().hex}{suffix}"
    await download_typhoon_track_image(image.url, str(img_path))
    return image, img_path


async def _send_typhoon_forward_message(
    ctx: MessageContext,
    text: str | None,
    caption: str,
    img_path: Path,
) -> bool:
    """Try to send typhoon output as a collapsed chat-record message."""
    if not _supports_onebot_forward_message(ctx):
        return False

    text_factory = getattr(MessageElement, "text", None)
    message_factory = getattr(MessageElement, "message", None)
    forward_factory = getattr(MessageElement, "forward", None)
    if not (callable(text_factory) and callable(message_factory) and callable(forward_factory)):
        return False

    nodes: list[Any] = []
    if text:
        nodes.append(message_factory([text_factory(text)], nickname="AstroAssist"))
    nodes.append(
        message_factory(
            [text_factory(caption), MessageElement.img(str(img_path))],
            nickname="AstroAssist",
        )
    )
    try:
        await ctx.send([forward_factory(nodes)])
    except Exception:
        _LOG.exception("AstroAssist typhoon folded message send failed")
        return False
    return True


def _supports_onebot_forward_message(ctx: MessageContext) -> bool:
    adapter = getattr(ctx, "adapter", None)
    return str(getattr(adapter, "platform", "") or "").lower() == "onebot_v11"


def _format_typhoon_track_caption(image: TyphoonTrackImage) -> str:
    display_label = image.name or "台风路径预报"
    msg = f"🌀 {display_label}路径预报图"
    if image.time:
        msg += f"  ({image.time})"
    return msg


def _safe_filename_piece(value: str) -> str:
    """Return a short filesystem-safe label for generated image files."""
    safe = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._-")
    return safe[:40] or "track"
