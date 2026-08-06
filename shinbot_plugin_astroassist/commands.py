"""Command handlers for 晴天钟, 设置位置, 雷达, 海区云图, 台风 and 过境卫星."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from shinbot.schema.elements import MessageElement

from .dapiya_floater import (
    DapiyaFloaterError,
    DapiyaFloaterFrame,
    download_dapiya_floater_image,
    fetch_dapiya_floater,
    fetch_dapiya_floater_gif,
    normalize_dapiya_product,
)
from .forecast import fetch_forecast
from .geo import amap_geocode
from .lightpollution import (
    LightPollution,
    fetch_light_pollution,
    format_light_pollution_report,
)
from .models import LocationData
from .radar import download_radar_image, fetch_radar, fetch_radar_gif
from .satellite import (
    download_satellite_image,
    fetch_satellite,
    fetch_satellite_gif,
)
from .storage import LocationStore
from .transit import fetch_transit_report, split_satellite_query
from .typhoon import (
    NmcTyphoonNewsProvider,
    TyphoonDetail,
    TyphoonProvider,
    TyphoonSummary,
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
    "!晴天钟 -n → 过滤夜间窗口(18点至06点)\n"
    "!光污染 [地名] [年份] → 当前位置光污染 (Bortle) 报告，年份 2012-2025\n\n"
    "📡 3. 雷达回波\n"
    "!雷达 → 获取最新全国雷达回波拼图\n"
    "!雷达 华北 → 区域拼图 (华北/华东/华南/...)\n"
    "!雷达 北京 → 单站雷达 (省份或城市名)\n"
    "!雷达 无锡 → 无对应站点时自动选择临近站点\n"
    "!雷达动图 → 全国雷达回波动画 (~2小时)\n\n"
    "🌊 4. 海区云图\n"
    "!海区云图 → 获取最新海区红外云图\n"
    "!海区云图 西北太平洋 → 西北太平洋海区红外云图\n"
    "!海区云图动图 → 海区红外云图动画\n\n"
    "🌀 5. 台风路径\n"
    "!台风 → 查询中央气象台最新台风快讯\n"
    "!台风 <名称或编号> → 查询任一活跃台风详情；若有对应路径页会附带路径预报图\n"
    "!台风云图 [名称或编号] [VIS|RGB|TRUECOLOR] → 查询 Dapiya 台风云图\n"
    "!台风云图动图 [名称或编号] [VIS|RGB|TRUECOLOR] → 查询 Dapiya 台风云图动画\n"
    "  (数据源：中央气象台 NMC 台风快讯/路径图，Dapiya 台风云图)\n\n"
    "🛰️ 6. 卫星过境预报\n"
    "!过境卫星 → 默认位置未来3天 国际空间站/天宫空间站 过境时刻\n"
    "!过境卫星 [卫星名] → 国际空间站/天宫空间站/哈勃，或直接输入 NORAD 编号\n"
    "!过境卫星 -d [天数] → 指定预报长度(1-7天)\n"
    "!过境卫星 -n → 仅夜间(太阳在地平线下)过境\n"
    "!过境卫星 [地名] → 临时查询某地\n"
    "  (数据源：CelesTrak TLE + SGP4 本地轨道推算)\n\n"
    "📊 7. 核心指标说明\n"
    "• 视宁度 (Seeing): 大气抖动，越小越稳\n"
    "• 透明度 (Transparency): 大气透亮感\n"
    "• 光污染 (Bortle 1-9): 级别越低天空越暗，观星条件越好 (DarkMap)\n"
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


def _parse_light_pollution_args(raw: str) -> tuple[str | None, int | None]:
    """Split ``[地名] [年份]`` for the light pollution command."""
    tokens = raw.strip().split()
    year = None
    place_tokens: list[str] = []
    for token in tokens:
        if token.isdigit() and len(token) == 4:
            year = int(token)
        else:
            place_tokens.append(token)
    place = " ".join(place_tokens) if place_tokens else None
    return place, year


def _parse_transit_args(raw: str) -> tuple[int, bool, str]:
    """Parse ``-d <days>`` ``-n`` anywhere and return the remaining text."""
    args = raw.strip().split()
    days = 3
    night_only = False
    rest: list[str] = []
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
        rest.append(args[i])
        i += 1
    return days, night_only, " ".join(rest)


async def _resolve_observation_location(
    ctx: MessageContext,
    config: Any,
    store: LocationStore,
    target_place: str | None,
) -> LocationData | None:
    """Resolve a stored or temporary observation location.

    Returns ``None`` after sending an error message to *ctx* when the
    location cannot be resolved.
    """
    if target_place:
        if not config.amap_key:
            await ctx.send("❌ 未配置 amap_key，无法解析地名。")
            return None
        try:
            lat, lon = await amap_geocode(target_place, config.amap_key)
            return LocationData(lat=lat, lon=lon, name=target_place)
        except ValueError as exc:
            await ctx.send(f"❌ 临时解析失败: {exc}")
            return None
    location = await store.get(ctx.session_id)
    if not location:
        await ctx.send("❌ 请先用 `!设置位置 [地名]` 设置默认观测位置。")
        return None
    return location


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
        location = await _resolve_observation_location(ctx, config, store, target_place)
        if location is None:
            ctx.stop()
            return

        # Check RenderKit
        if not _RENDERKIT_AVAILABLE:
            await ctx.send("❌ 渲染引擎 (RenderKit) 未安装，无法生成看板图片。")
            ctx.stop()
            return

        # Fetch & process
        light_task = asyncio.create_task(
            fetch_light_pollution(location.lat, location.lon)
        )
        try:
            render_data = await fetch_forecast(
                location.lat, location.lon, days=days, night_only=night_only
            )
            render_data.location_name = location.name
        except Exception as exc:
            light_task.cancel()
            _LOG.exception("AstroAssist forecast error")
            await ctx.send(f"❌ 预报获取异常: {exc}")
            ctx.stop()
            return

        light_pollution: LightPollution | None = None
        try:
            light_pollution = await light_task
        except Exception:
            _LOG.info("AstroAssist light pollution unavailable", exc_info=True)
        light_pollution_text = (
            f"{light_pollution.bortle_text} · {light_pollution.mpsas:.2f} mag/arcsec²"
            if light_pollution is not None
            else None
        )

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
                    "light_pollution_text": light_pollution_text,
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

    # ---- 光污染 ----
    @plg.on_command(
        "光污染",
        aliases=["lightpollution", "bortle", "光害"],
        description="生成当前位置的光污染 (Bortle) 报告",
        usage="!光污染 [地名] [年份]",
    )
    async def handle_light_pollution(ctx: MessageContext, raw_args: str) -> None:
        args = raw_args.strip()
        if args.split()[:1] in (["help"], ["帮助"], ["-h"]):
            await ctx.send(
                "🌌 光污染报告 | 说明\n"
                "!光污染 → 默认观测位置 (最新数据)\n"
                "!光污染 [地名] → 临时查询某地 (需配置 amap_key)\n"
                "!光污染 [年份] → 指定数据年份 (2012-2025，逐年对比)"
            )
            ctx.stop()
            return

        target_place, year = _parse_light_pollution_args(args)
        location = await _resolve_observation_location(
            ctx, config, store, target_place
        )
        if location is None:
            ctx.stop()
            return

        try:
            light_pollution = await fetch_light_pollution(
                location.lat, location.lon, year=year
            )
        except Exception as exc:
            _LOG.exception("AstroAssist light pollution query error")
            await ctx.send(f"❌ 光污染数据获取失败: {exc}")
            ctx.stop()
            return

        await ctx.send(
            format_light_pollution_report(
                location.name,
                location.lat,
                location.lon,
                light_pollution,
            )
        )
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
        await _handle_radar_static(
            ctx,
            raw_args.strip(),
            plg,
            amap_key=getattr(config, "amap_key", ""),
        )
        ctx.stop()

    # ---- 雷达动图 ----
    @plg.on_command(
        "雷达动图",
        aliases=["radargif"],
        description="获取雷达回波动图 (~2小时动画)",
        usage="!雷达动图 [区域或城市名]",
    )
    async def handle_radar_gif(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_radar_gif(
            ctx,
            raw_args.strip(),
            plg,
            amap_key=getattr(config, "amap_key", ""),
        )
        ctx.stop()

    # ---- 海区云图 ----
    @plg.on_command(
        "海区云图",
        aliases=["seacloud", "sea"],
        description="获取最新中央气象台海区红外云图",
        usage="!海区云图 [产品名]",
    )
    async def handle_sea_cloud(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_sea_cloud_static(ctx, raw_args.strip(), plg)
        ctx.stop()

    # ---- 海区云图动图 ----
    @plg.on_command(
        "海区云图动图",
        aliases=["seacloudgif", "seagif"],
        description="获取中央气象台海区红外云图动画",
        usage="!海区云图动图 [产品名]",
    )
    async def handle_sea_cloud_gif(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_sea_cloud_gif(ctx, raw_args.strip(), plg)
        ctx.stop()

    # ---- 台风云图 ----
    @plg.on_command(
        "台风云图",
        aliases=["typhooncloud", "tccloud"],
        description="获取 Dapiya 热带气旋 floater 云图",
        usage="!台风云图 [名称或编号] [VIS|RGB|TRUECOLOR]",
    )
    async def handle_typhoon_cloud(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        await _handle_typhoon_cloud_static(ctx, raw_args.strip(), provider, plg)
        ctx.stop()

    # ---- 台风云图动图 ----
    @plg.on_command(
        "台风云图动图",
        aliases=["typhooncloudgif", "tccloudgif"],
        description="获取 Dapiya 热带气旋 floater 云图动画",
        usage="!台风云图动图 [名称或编号] [VIS|RGB|TRUECOLOR]",
    )
    async def handle_typhoon_cloud_gif(
        ctx: MessageContext, raw_args: str
    ) -> None:  # noqa: UP037
        await _handle_typhoon_cloud_gif(ctx, raw_args.strip(), provider, plg)
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

    # ---- 过境卫星 ----
    @plg.on_command(
        "过境卫星",
        aliases=["卫星过境", "transit", "satpass"],
        description="预报国际空间站等亮卫星的过境时刻",
        usage="!过境卫星 [卫星名] [-d 天数] [-n] [地名]",
    )
    async def handle_transit(ctx: MessageContext, raw_args: str) -> None:  # noqa: UP037
        args = raw_args.strip()

        if args.split()[:1] in (["help"], ["帮助"], ["-h"]):
            await ctx.send(
                "🛰️ 过境卫星预报 | 说明\n"
                "!过境卫星 → 默认观测位置，未来3天国际空间站/天宫空间站过境\n"
                "!过境卫星 [卫星名] → 国际空间站(ISS)/天宫空间站(CSS)/哈勃(HST)，或直接输入 NORAD 编号\n"
                "!过境卫星 -d [天数] → 预报长度 1-7 天\n"
                "!过境卫星 -n → 仅夜间（太阳在地平线下）过境\n"
                "!过境卫星 [地名] → 临时查询某地\n"
                "  (数据源：CelesTrak TLE + SGP4 本地轨道推算)"
            )
            ctx.stop()
            return

        days, night_only, rest = _parse_transit_args(args)
        _satellites, place = split_satellite_query(rest)
        location = await _resolve_observation_location(
            ctx, config, store, place or None
        )
        if location is None:
            ctx.stop()
            return

        try:
            report = await fetch_transit_report(
                location.lat,
                location.lon,
                location.name,
                query=rest,
                days=days,
                night_only=night_only,
            )
        except Exception as exc:
            _LOG.exception("AstroAssist satellite transit error")
            await ctx.send(f"❌ 过境预报获取失败: {exc}")
            ctx.stop()
            return

        await ctx.send(report)
        ctx.stop()


async def _handle_radar_static(
    ctx: MessageContext,
    query: str,
    plg: Any,
    *,
    amap_key: str = "",
) -> None:
    """Send the latest single radar frame as PNG."""
    try:
        if amap_key:
            url, obs_time, label = await fetch_radar(query, amap_key=amap_key)
        else:
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
    ctx: MessageContext,
    query: str,
    plg: Any,
    *,
    amap_key: str = "",
) -> None:
    """Send an animated radar echo GIF (~2 h history)."""
    try:
        if amap_key:
            gif_bytes, newest, oldest, label = await fetch_radar_gif(
                query,
                amap_key=amap_key,
            )
        else:
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
    await ctx.send([MessageElement.img(str(gif_path), sub_type="0")])


async def _handle_sea_cloud_static(
    ctx: MessageContext,
    query: str,
    plg: Any,
) -> None:
    """Send the latest single sea-area cloud frame."""
    try:
        url, obs_time, label = await fetch_satellite(query)
    except Exception as exc:
        _LOG.exception("AstroAssist sea cloud fetch error")
        await ctx.send(f"❌ 海区云图获取失败: {exc}")
        return

    try:
        img_path = await _download_sea_cloud_image(plg, url, prefix="sea_cloud_latest")
    except Exception as exc:
        _LOG.exception("AstroAssist sea cloud download error")
        await ctx.send(f"❌ 海区云图下载失败: {exc}")
        return

    msg = _format_sea_cloud_caption(label, obs_time)
    await ctx.send(msg)
    await ctx.send([MessageElement.img(str(img_path))])


async def _handle_sea_cloud_gif(
    ctx: MessageContext,
    query: str,
    plg: Any,
) -> None:
    """Send an animated sea-area cloud GIF."""
    try:
        gif_bytes, newest, oldest, label = await fetch_satellite_gif(query)
    except Exception as exc:
        _LOG.exception("AstroAssist sea cloud GIF error")
        await ctx.send(f"❌ 海区云图动图生成失败: {exc}")
        return

    gif_path = Path(plg.data_dir) / f"sea_cloud_animated_{uuid4().hex}.gif"
    gif_path.write_bytes(gif_bytes)

    time_range = f"{oldest} → {newest}" if newest and oldest else newest
    msg = f"🌊 {label}动图"
    if time_range:
        msg += f"  ({time_range})"
    await ctx.send(msg)
    await ctx.send([MessageElement.img(str(gif_path), sub_type="0")])


async def _handle_typhoon_cloud_static(
    ctx: MessageContext,
    raw_args: str,
    provider: TyphoonProvider,
    plg: Any,
) -> None:
    """Send the latest Dapiya tropical cyclone floater frame."""
    try:
        query, product = _parse_typhoon_cloud_args(raw_args)
        frame = await _fetch_dapiya_floater_for_query(query, product, provider)
    except Exception as exc:
        _LOG.exception("AstroAssist Dapiya typhoon cloud fetch error")
        await ctx.send(f"❌ 台风云图获取失败: {exc}")
        return

    try:
        img_path = await _download_dapiya_floater_frame(plg, frame, prefix="typhoon_cloud")
    except Exception as exc:
        _LOG.exception("AstroAssist Dapiya typhoon cloud download error")
        await ctx.send(f"❌ 台风云图下载失败: {exc}")
        return

    await ctx.send(_format_typhoon_cloud_caption(frame))
    await ctx.send([MessageElement.img(str(img_path))])


async def _handle_typhoon_cloud_gif(
    ctx: MessageContext,
    raw_args: str,
    provider: TyphoonProvider,
    plg: Any,
) -> None:
    """Send a Dapiya tropical cyclone floater GIF."""
    try:
        query, product = _parse_typhoon_cloud_args(raw_args)
        gif_bytes, newest, oldest = await _fetch_dapiya_floater_gif_for_query(
            query,
            product,
            provider,
        )
    except Exception as exc:
        _LOG.exception("AstroAssist Dapiya typhoon cloud GIF error")
        await ctx.send(f"❌ 台风云图动图生成失败: {exc}")
        return

    gif_path = (
        Path(plg.data_dir)
        / f"typhoon_cloud_{newest.storm_id}_{newest.product}_{uuid4().hex}.gif"
    )
    gif_path.write_bytes(gif_bytes)

    time_range = (
        f"{oldest.time} → {newest.time}"
        if oldest.time and newest.time
        else newest.time
    )
    msg = _format_typhoon_cloud_caption(newest, suffix="动图")
    if time_range:
        msg += f"  ({time_range})"
    await ctx.send(msg)
    await ctx.send([MessageElement.img(str(gif_path), sub_type="0")])


async def _send_typhoon_response(
    ctx: MessageContext,
    text: str | None,
    query: str,
    provider: TyphoonProvider,
    plg: Any,
) -> bool:
    """Send typhoon text, track image, and static context cloud image."""
    warnings: list[str] = []
    track_payload: tuple[TyphoonTrackImage, Path] | None = None
    try:
        track_payload = await _prepare_typhoon_track_image(query, provider, plg)
    except Exception as exc:
        _LOG.exception("AstroAssist typhoon track image download error")
        warnings.append(f"⚠️ 台风路径图下载失败: {exc}")

    if text is None and track_payload is None:
        return False

    sea_cloud_payload: tuple[str, Path] | None = None
    try:
        sea_cloud_payload = await _prepare_typhoon_context_cloud_image(
            query,
            provider,
            plg,
        )
    except Exception as exc:
        _LOG.exception("AstroAssist typhoon sea cloud image download error")
        warnings.append(f"⚠️ 海区云图下载失败: {exc}")

    if await _send_typhoon_forward_message(
        ctx,
        text,
        warnings,
        track_payload,
        sea_cloud_payload,
    ):
        return True

    if text:
        await ctx.send(text)
    for warning in warnings:
        await ctx.send(warning)

    if track_payload is not None:
        image, img_path = track_payload
        await ctx.send(_format_typhoon_track_caption(image))
        await ctx.send([MessageElement.img(str(img_path))])

    if sea_cloud_payload is not None:
        caption, img_path = sea_cloud_payload
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


async def _prepare_typhoon_context_cloud_image(
    query: str,
    provider: TyphoonProvider,
    plg: Any,
) -> tuple[str, Path]:
    """Download the best static cloud image for typhoon context."""
    try:
        frame = await _fetch_dapiya_floater_for_query(query, "VIS", provider)
        img_path = await _download_dapiya_floater_frame(
            plg,
            frame,
            prefix="typhoon_cloud",
        )
        return _format_typhoon_cloud_caption(frame, note="（台风环境参考）"), img_path
    except Exception:
        _LOG.exception("AstroAssist Dapiya typhoon cloud unavailable; falling back")

    return await _prepare_typhoon_sea_cloud_image(plg)


async def _prepare_typhoon_sea_cloud_image(plg: Any) -> tuple[str, Path]:
    """Download the latest static sea-area cloud image for typhoon context."""
    url, obs_time, label = await fetch_satellite("")
    img_path = await _download_sea_cloud_image(plg, url, prefix="typhoon_sea_cloud")
    return _format_sea_cloud_caption(label, obs_time, note="（台风环境参考）"), img_path


async def _fetch_dapiya_floater_for_query(
    query: str,
    product: str,
    provider: TyphoonProvider,
) -> DapiyaFloaterFrame:
    last_error: DapiyaFloaterError | None = None
    attempts = _dapiya_query_attempts(query)
    for attempt in attempts:
        try:
            return await fetch_dapiya_floater(attempt, product=product)
        except DapiyaFloaterError as exc:
            last_error = exc
            continue

    enriched_query = await _enrich_typhoon_cloud_query(query, provider)
    for attempt in _dapiya_query_attempts(enriched_query):
        if attempt in attempts:
            continue
        try:
            return await fetch_dapiya_floater(attempt, product=product)
        except DapiyaFloaterError as exc:
            last_error = exc
            continue

    raise last_error or DapiyaFloaterError(query or "默认热带气旋")


async def _fetch_dapiya_floater_gif_for_query(
    query: str,
    product: str,
    provider: TyphoonProvider,
) -> tuple[bytes, DapiyaFloaterFrame, DapiyaFloaterFrame]:
    last_error: DapiyaFloaterError | None = None
    attempts = _dapiya_query_attempts(query)
    for attempt in attempts:
        try:
            return await fetch_dapiya_floater_gif(attempt, product=product)
        except DapiyaFloaterError as exc:
            last_error = exc
            continue

    enriched_query = await _enrich_typhoon_cloud_query(query, provider)
    for attempt in _dapiya_query_attempts(enriched_query):
        if attempt in attempts:
            continue
        try:
            return await fetch_dapiya_floater_gif(attempt, product=product)
        except DapiyaFloaterError as exc:
            last_error = exc
            continue

    raise last_error or DapiyaFloaterError(query or "默认热带气旋")


async def _enrich_typhoon_cloud_query(query: str, provider: TyphoonProvider) -> str:
    text = query.strip()
    if not text or not hasattr(provider, "get_detail"):
        return text

    try:
        detail = await provider.get_detail(text)
    except Exception:
        return text
    if not isinstance(detail, TyphoonDetail):
        return text

    return _summary_cloud_query(detail.summary)


async def _download_sea_cloud_image(plg: Any, url: str, *, prefix: str) -> Path:
    suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".png"
    img_path = Path(plg.data_dir) / f"{prefix}_{uuid4().hex}{suffix}"
    await download_satellite_image(url, img_path)
    return img_path


async def _download_dapiya_floater_frame(
    plg: Any,
    frame: DapiyaFloaterFrame,
    *,
    prefix: str,
) -> Path:
    safe_storm = _safe_filename_piece(frame.storm_id or "storm")
    safe_product = _safe_filename_piece(frame.product or "VIS")
    suffix = Path(frame.url.split("?", 1)[0]).suffix.lower() or ".png"
    img_path = (
        Path(plg.data_dir)
        / f"{prefix}_{safe_storm}_{safe_product}_{uuid4().hex}{suffix}"
    )
    await download_dapiya_floater_image(frame.url, img_path)
    return img_path


async def _send_typhoon_forward_message(
    ctx: MessageContext,
    text: str | None,
    warnings: list[str],
    track_payload: tuple[TyphoonTrackImage, Path] | None,
    sea_cloud_payload: tuple[str, Path] | None,
) -> bool:
    """Try to send typhoon output as a collapsed chat-record message."""
    if not _supports_onebot_forward_message(ctx):
        return False

    text_factory = getattr(MessageElement, "text", None)
    message_factory = getattr(MessageElement, "message", None)
    forward_factory = getattr(MessageElement, "forward", None)
    if not (
        callable(text_factory)
        and callable(message_factory)
        and callable(forward_factory)
    ):
        return False

    nodes: list[Any] = []
    if text:
        nodes.append(message_factory([text_factory(text)], nickname="AstroAssist"))
    if warnings:
        nodes.append(
            message_factory([text_factory("\n".join(warnings))], nickname="AstroAssist")
        )
    if track_payload is not None:
        image, img_path = track_payload
        nodes.append(
            message_factory(
                [
                    text_factory(_format_typhoon_track_caption(image)),
                    MessageElement.img(str(img_path)),
                ],
                nickname="AstroAssist",
            )
        )
    if sea_cloud_payload is not None:
        caption, img_path = sea_cloud_payload
        nodes.append(
            message_factory(
                [text_factory(caption), MessageElement.img(str(img_path))],
                nickname="AstroAssist",
            )
        )
    if not nodes:
        return False
    try:
        await ctx.send([forward_factory(nodes)])
    except Exception:
        _LOG.exception("AstroAssist typhoon folded message send failed")
        return False
    return True


def _supports_onebot_forward_message(ctx: MessageContext) -> bool:
    adapter = getattr(ctx, "adapter", None)
    adapter_platform = str(getattr(adapter, "platform", "") or "").lower()
    if adapter_platform in {"onebot_v11", "onebot", "qq"}:
        return True

    adapter_type = type(adapter)
    adapter_type_name = adapter_type.__name__.lower()
    adapter_module = adapter_type.__module__.lower()
    return "onebot" in adapter_type_name or "shinbot_adapter_onebot_v11" in adapter_module


def _format_typhoon_track_caption(image: TyphoonTrackImage) -> str:
    display_label = image.name or "台风路径预报"
    msg = f"🌀 {display_label}路径预报图"
    if image.time:
        msg += f"  ({image.time})"
    return msg


def _parse_typhoon_cloud_args(raw: str) -> tuple[str, str]:
    args = raw.strip().split()
    if not args:
        return "", "VIS"

    for width in (2, 1):
        if len(args) < width:
            continue
        candidate = " ".join(args[-width:])
        try:
            product = normalize_dapiya_product(candidate)
        except DapiyaFloaterError:
            continue
        return " ".join(args[:-width]), product

    return raw.strip(), "VIS"


def _dapiya_query_attempts(query: str) -> list[str]:
    attempts: list[str] = []
    for item in query.split():
        _append_unique(attempts, item)
    _append_unique(attempts, query.strip())
    return attempts or [""]


def _summary_cloud_query(summary: TyphoonSummary) -> str:
    return " ".join(
        item
        for item in (summary.identifier, summary.name, summary.english_name)
        if item
    )


def _format_typhoon_cloud_caption(
    frame: DapiyaFloaterFrame,
    *,
    note: str = "",
    suffix: str = "",
) -> str:
    display_label = " ".join(item for item in (frame.storm_id, frame.name) if item)
    if not display_label:
        display_label = "热带气旋"
    msg = f"🛰️ {display_label}台风云图{suffix} {frame.product}{note}"
    if frame.time:
        msg += f"  ({frame.time})"
    msg += "  来源：Dapiya"
    return msg


def _format_sea_cloud_caption(label: str, obs_time: str, *, note: str = "") -> str:
    msg = f"🌊 {label}{note}"
    if obs_time:
        msg += f"  ({obs_time})"
    return msg


def _append_unique(items: list[str], value: str) -> None:
    text = value.strip()
    if text and text not in items:
        items.append(text)


def _safe_filename_piece(value: str) -> str:
    """Return a short filesystem-safe label for generated image files."""
    safe = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._-")
    return safe[:40] or "track"
