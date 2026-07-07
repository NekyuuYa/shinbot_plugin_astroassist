"""Tests for AstroAssist typhoon framework helpers and command wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.commands import (
    register_commands,
)
from plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.storage import (
    LocationStore,
)
from plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.typhoon import (
    NmcTyphoonNewsProvider,
    TyphoonDetail,
    TyphoonSummary,
    TyphoonTrackImage,
    TyphoonTrackPage,
    TyphoonTrackPoint,
    TyphoonUnavailable,
    format_typhoon_detail,
    format_typhoon_help,
    format_typhoon_list,
    parse_nmc_typhoon_news_html,
    parse_nmc_typhoon_track_images_html,
    parse_nmc_typhoon_track_pages_html,
    parse_typhoon_args,
)


class _FakePlugin:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.commands: dict[str, dict[str, Any]] = {}

    def on_command(self, name: str, **kwargs: Any) -> Any:
        def decorator(func: Any) -> Any:
            self.commands[name] = {**kwargs, "handler": func}
            return func

        return decorator


class _Ctx:
    def __init__(self, *, adapter_platform: str = "onebot_v11") -> None:
        self.session_id = "session-1"
        self.adapter = SimpleNamespace(platform=adapter_platform)
        self.sent: list[Any] = []
        self.stopped = False

    async def send(self, content: Any) -> None:
        self.sent.append(content)

    def stop(self) -> None:
        self.stopped = True


_NMC_SAMPLE_HTML = """
<div id=text>
  <div class=title> 台风快讯 </div>
  <div class=number> 2026年总601期 </div>
  <div class=ctitle>
    <span>中国气象局中央气象台</span>
    <span>07月08日02时27分</span>
  </div>
  <table>
    <tbody>
      <tr><td>时&nbsp;&nbsp;&nbsp;&nbsp;间：</td><td> 08 日 02 时</td></tr>
      <tr><td>命&nbsp;&nbsp;&nbsp;&nbsp;名：</td><td> “巴威”，BAVI</td></tr>
      <tr><td>编&nbsp;&nbsp;&nbsp;&nbsp;号：</td><td> 2609 号</td></tr>
      <tr><td>中心位置：</td><td><span> 北纬16.8度、东经135.2度</span></td></tr>
      <tr><td>强度等级：</td><td><span> 超强台风级</span></td></tr>
      <tr><td>最大风力：</td><td><span> 17级， 58米/秒（约209公里/小时）</span></td></tr>
      <tr><td>中心气压：</td><td> 925 hPa</td></tr>
      <tr><td>参考位置：</td><td> 距离台湾基隆市东偏南方向约1680公里</td></tr>
      <tr>
        <td>风圈半径：</td>
        <td> 七级风圈半径 东北方向500公里<br> 十级风圈半径 东北方向300公里</td>
      </tr>
      <tr>
        <td>预报结论：</td>
        <td> “巴威”将以每小时20公里左右的速度向偏西方向移动<br>（下次更新时间为8日5时30分）</td>
      </tr>
    </tbody>
  </table>
</div>
"""

_NMC_TRACK_SAMPLE_HTML = """
<title>台风海洋_台风路径预报_美莎克</title>
<ul>
  <li><a href="/publish/typhoon/probability-img2.html" class=actived>美莎克</a></li>
  <li><a href="/publish/typhoon/probability-img1.html">巴威</a></li>
</ul>
<img id=imgpath
  data-time="07/06 17:00"
  src="https://image.nmc.cn/product/2026/07/06/TCBU/medium/latest.JPG?v=1">
<div class="col-xs-12 time actived"
  data-index=0
  data-img="https://image.nmc.cn/product/2026/07/06/TCBU/medium/latest.JPG?v=1"
  data-time="07/06 17:00"><div> 07/06 17:00 </div></div>
<div class="col-xs-12 time"
  data-index=1
  data-img="https://image.nmc.cn/product/2026/07/06/TCBU/medium/older.JPG?v=1"
  data-time="07/06 14:00"><div> 07/06 14:00 </div></div>
"""

_NMC_TRACK_BAVI_HTML = """
<title>台风海洋_台风路径预报_巴威</title>
<ul>
  <li><a href="/publish/typhoon/probability-img2.html">美莎克</a></li>
  <li><a href="/publish/typhoon/probability-img1.html" class=actived>巴威</a></li>
</ul>
<div class="col-xs-12 time actived"
  data-index=0
  data-img="https://image.nmc.cn/product/2026/07/08/TCBU/medium/bavi.JPG?v=1"
  data-time="07/08 02:00"><div> 07/08 02:00 </div></div>
"""

_NMC_TRACK_ARBITRARY_IMG3_HTML = """
<title>台风海洋_台风路径预报_玲玲</title>
<div class=bgwhite>
  <div class=p-wrap>
    <div class="p-nav nav1">
      <div class=sl-key> 类型： </div>
      <ul>
        <li><a href="/publish/typhoon/probability-img3.html" class=actived>玲玲</a></li>
        <li><a href="/publish/typhoon/probability-img4.html">桦加沙</a></li>
      </ul>
    </div>
  </div>
</div>
<div class="col-xs-12 time actived"
  data-index=0
  data-img="https://image.nmc.cn/product/2026/08/01/TCBU/medium/lingling.JPG?v=1"
  data-time="08/01 08:00"><div> 08/01 08:00 </div></div>
"""

_NMC_TRACK_REALISTIC_NAV_HTML = """
<div class=header>
  <a href="/publish/typhoon/probability-img2.html" title="台风路径预报">台风路径预报</a>
</div>
<ol class="breadcrumb">
  <li><a href="/publish/typhoon/probability-img2.html">台风路径预报</a></li>
  <li class=active>巴威</li>
</ol>
<div class=bgwhite>
  <div class=p-wrap>
    <div class="p-nav nav1">
      <div class=sl-key> 类型： </div>
      <ul>
        <li><a href="/publish/typhoon/probability-img2.html">美莎克</a></li>
        <li><a href="/publish/typhoon/probability-img1.html" class=actived>巴威</a></li>
      </ul>
      <div class=morelist> 更多 </div>
    </div>
  </div>
</div>
"""


def _register(tmp_path: Path, *, typhoon_provider: Any | None = None) -> _FakePlugin:
    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=""),
        LocationStore(tmp_path),
        tmp_path / "template.html",
        typhoon_provider=typhoon_provider,
    )
    return plugin


@pytest.mark.parametrize(
    ("raw", "action", "query"),
    [
        ("", "list", ""),
        ("   ", "list", ""),
        ("help", "help", ""),
        ("帮助", "help", ""),
        ("-h", "help", ""),
        ("list", "list", ""),
        ("活跃", "list", ""),
        ("2501", "detail", "2501"),
        ("蝴蝶 2501", "detail", "蝴蝶 2501"),
    ],
)
def test_parse_typhoon_args(raw: str, action: str, query: str) -> None:
    parsed = parse_typhoon_args(raw)

    assert parsed.action == action
    assert parsed.query == query


def test_format_typhoon_help_mentions_framework_status() -> None:
    text = format_typhoon_help()

    assert "!台风" in text
    assert "!typhoon" in text
    assert "中央气象台" in text


def test_format_typhoon_unavailable_list() -> None:
    text = format_typhoon_list(TyphoonUnavailable())

    assert "台风数据源暂未接入" in text
    assert "!台风 list" in text


def test_format_typhoon_empty_list() -> None:
    assert "当前没有活跃台风" in format_typhoon_list([])


def test_format_typhoon_list_with_summaries() -> None:
    text = format_typhoon_list(
        [
            TyphoonSummary(
                identifier="2501",
                name="蝴蝶",
                english_name="Wutip",
                status="热带风暴",
                updated_at="2026-07-08 08:00",
            )
        ]
    )

    assert "2501 蝴蝶 (Wutip)" in text
    assert "热带风暴" in text
    assert "2026-07-08 08:00" in text


def test_format_typhoon_detail_with_track_and_forecast() -> None:
    detail = TyphoonDetail(
        summary=TyphoonSummary(
            identifier="2501",
            name="蝴蝶",
            status="强热带风暴",
            updated_at="2026-07-08 08:00",
            center_lat=18.4,
            center_lon=122.1,
            wind_speed=30,
            pressure=980,
        ),
        track=[
            TyphoonTrackPoint("07-08 02:00", 17.8, 121.4, level="热带风暴"),
            TyphoonTrackPoint("07-08 08:00", 18.4, 122.1, level="强热带风暴"),
        ],
        forecast=[TyphoonTrackPoint("07-08 14:00", 19.2, 123.0, movement="向西北移动")],
    )

    text = format_typhoon_detail(detail)

    assert "2501 蝴蝶" in text
    assert "中心位置：18.4, 122.1" in text
    assert "近中心最大风速：30 m/s" in text
    assert "最近路径" in text
    assert "路径预报" in text


def test_parse_nmc_typhoon_news_html_extracts_quick_bulletin() -> None:
    detail = parse_nmc_typhoon_news_html(_NMC_SAMPLE_HTML)

    assert detail.summary.identifier == "2609"
    assert detail.summary.name == "巴威"
    assert detail.summary.english_name == "BAVI"
    assert detail.summary.status == "超强台风级"
    assert detail.summary.updated_at == "07月08日02时27分"
    assert detail.summary.center_lat == 16.8
    assert detail.summary.center_lon == 135.2
    assert detail.summary.wind_speed == 58
    assert detail.summary.pressure == 925
    assert detail.issue_number == "2026年总601期"
    assert detail.source == "中国气象局中央气象台"
    assert detail.observation_time == "08 日 02 时"
    assert "七级风圈半径" in detail.wind_circle
    assert "下次更新时间" in detail.forecast_conclusion


def test_parse_nmc_typhoon_track_images_html_extracts_frames() -> None:
    frames = parse_nmc_typhoon_track_images_html(_NMC_TRACK_SAMPLE_HTML)

    assert len(frames) == 2
    assert frames[0].name == "美莎克"
    assert frames[0].time == "07/06 17:00"
    assert frames[0].url.endswith("latest.JPG?v=1")
    assert frames[1].time == "07/06 14:00"


def test_parse_nmc_typhoon_track_pages_html_extracts_multiple_pages() -> None:
    pages = parse_nmc_typhoon_track_pages_html(_NMC_TRACK_SAMPLE_HTML)

    assert pages == [
        TyphoonTrackPage(
            name="美莎克",
            url="https://www.nmc.cn/publish/typhoon/probability-img2.html",
            active=True,
        ),
        TyphoonTrackPage(
            name="巴威",
            url="https://www.nmc.cn/publish/typhoon/probability-img1.html",
            active=False,
        ),
    ]


def test_parse_nmc_typhoon_track_pages_html_ignores_page_chrome_links() -> None:
    pages = parse_nmc_typhoon_track_pages_html(_NMC_TRACK_REALISTIC_NAV_HTML)

    assert pages == [
        TyphoonTrackPage(
            name="美莎克",
            url="https://www.nmc.cn/publish/typhoon/probability-img2.html",
            active=False,
        ),
        TyphoonTrackPage(
            name="巴威",
            url="https://www.nmc.cn/publish/typhoon/probability-img1.html",
            active=True,
        ),
    ]


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_returns_latest_summary() -> None:
    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html)
    result = await provider.list_active()

    assert isinstance(result, list)
    assert result[0].identifier == "2609"
    assert result[0].name == "巴威"


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_matches_name_and_identifier() -> None:
    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html)

    by_name = await provider.get_detail("巴威")
    by_identifier = await provider.get_detail("2609")
    missing = await provider.get_detail("不存在")

    assert isinstance(by_name, TyphoonDetail)
    assert isinstance(by_identifier, TyphoonDetail)
    assert isinstance(missing, TyphoonUnavailable)
    assert "当前快讯" in missing.hint


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_returns_matching_track_image() -> None:
    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    async def fetch_track_html(url: str) -> str:
        if url.endswith("probability-img1.html"):
            return _NMC_TRACK_BAVI_HTML
        return _NMC_TRACK_SAMPLE_HTML

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html, fetch_track_html=fetch_track_html)

    image = await provider.get_track_image("美莎克")
    bavi = await provider.get_track_image("巴威")
    missing = await provider.get_track_image("不存在")

    assert isinstance(image, TyphoonTrackImage)
    assert image.url.endswith("latest.JPG?v=1")
    assert isinstance(bavi, TyphoonTrackImage)
    assert bavi.name == "巴威"
    assert bavi.url.endswith("bavi.JPG?v=1")
    assert isinstance(missing, TyphoonUnavailable)


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_falls_back_to_probability_img1_seed() -> None:
    requested_urls: list[str] = []

    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    async def fetch_track_html(url: str) -> str:
        requested_urls.append(url)
        if url.endswith("probability-img2.html"):
            raise RuntimeError("img2 unavailable")
        if url.endswith("probability-img1.html"):
            return _NMC_TRACK_BAVI_HTML
        raise AssertionError(f"unexpected url: {url}")

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html, fetch_track_html=fetch_track_html)

    image = await provider.get_track_image("巴威")

    assert isinstance(image, TyphoonTrackImage)
    assert image.name == "巴威"
    assert image.url.endswith("bavi.JPG?v=1")
    assert requested_urls == [
        "https://www.nmc.cn/publish/typhoon/probability-img2.html",
        "https://www.nmc.cn/publish/typhoon/probability-img1.html",
    ]


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_finds_arbitrary_name_from_numbered_seed() -> None:
    requested_urls: list[str] = []

    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    async def fetch_track_html(url: str) -> str:
        requested_urls.append(url)
        if url.endswith("probability-img3.html"):
            return _NMC_TRACK_ARBITRARY_IMG3_HTML
        raise RuntimeError("seed unavailable")

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html, fetch_track_html=fetch_track_html)

    image = await provider.get_track_image("玲玲")

    assert isinstance(image, TyphoonTrackImage)
    assert image.name == "玲玲"
    assert image.url.endswith("lingling.JPG?v=1")
    assert requested_urls == [
        "https://www.nmc.cn/publish/typhoon/probability-img2.html",
        "https://www.nmc.cn/publish/typhoon/probability-img1.html",
        "https://www.nmc.cn/publish/typhoon/probability-img3.html",
    ]


@pytest.mark.asyncio
async def test_nmc_typhoon_provider_stops_after_valid_navigation_miss() -> None:
    requested_urls: list[str] = []

    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    async def fetch_track_html(url: str) -> str:
        requested_urls.append(url)
        return _NMC_TRACK_REALISTIC_NAV_HTML

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html, fetch_track_html=fetch_track_html)

    image = await provider.get_track_image("不存在")

    assert isinstance(image, TyphoonUnavailable)
    assert requested_urls == ["https://www.nmc.cn/publish/typhoon/probability-img2.html"]


def test_register_commands_declares_typhoon_command(tmp_path: Path) -> None:
    plugin = _register(tmp_path)

    assert "台风" in plugin.commands
    assert plugin.commands["台风"]["aliases"] == ["typhoon"]
    assert "名称或编号" in plugin.commands["台风"]["usage"]


@pytest.mark.asyncio
async def test_typhoon_command_returns_help(tmp_path: Path) -> None:
    plugin = _register(tmp_path)
    ctx = _Ctx()

    await plugin.commands["台风"]["handler"](ctx, "help")

    assert ctx.stopped
    assert "台风路径查询" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_typhoon_command_default_provider_reports_nmc_summary(tmp_path: Path) -> None:
    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    plugin = _register(tmp_path, typhoon_provider=NmcTyphoonNewsProvider(fetch_html=fetch_html))
    ctx = _Ctx()

    await plugin.commands["台风"]["handler"](ctx, "")

    assert ctx.stopped
    assert "NMC 最新台风快讯" in ctx.sent[-1]
    assert "2609 巴威 (BAVI)" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_typhoon_command_uses_injected_provider_for_detail(tmp_path: Path) -> None:
    class Provider:
        async def list_active(self) -> list[TyphoonSummary]:
            return []

        async def get_detail(self, query: str) -> TyphoonDetail:
            return TyphoonDetail(
                summary=TyphoonSummary(identifier=query, name="测试台风", wind_speed=25)
            )

    plugin = _register(tmp_path, typhoon_provider=Provider())
    ctx = _Ctx()

    await plugin.commands["台风"]["handler"](ctx, "2501")

    assert ctx.stopped
    assert "2501 测试台风" in ctx.sent[-1]
    assert "25 m/s" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_typhoon_command_sends_track_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.commands as commands

    async def fake_download(url: str, dest: str) -> None:
        Path(dest).write_bytes(url.encode())

    class Provider:
        async def list_active(self) -> list[TyphoonSummary]:
            return []

        async def get_detail(self, query: str) -> TyphoonDetail:
            return TyphoonDetail(summary=TyphoonSummary(identifier=query, name="测试台风"))

        async def get_track_image(self, query: str = "") -> TyphoonTrackImage:
            assert query == "测试台风"
            return TyphoonTrackImage(
                url="https://image.nmc.cn/product/typhoon.JPG?v=1",
                time="07/06 17:00",
                name="测试台风",
            )

    monkeypatch.setattr(commands, "download_typhoon_track_image", fake_download)
    plugin = _register(tmp_path, typhoon_provider=Provider())
    ctx = _Ctx()

    await plugin.commands["台风"]["handler"](ctx, "2501")

    assert ctx.stopped
    folded = ctx.sent[-1][0]
    assert folded["type"] == "message"
    assert folded["attrs"] == {"forward": "true"}
    assert "2501 测试台风" in folded["children"][0]["children"][0]["attrs"]["content"]
    assert "测试台风路径预报图" in folded["children"][1]["children"][0]["attrs"]["content"]
    first_src = folded["children"][1]["children"][1]["attrs"]["src"]
    assert Path(first_src).name.startswith("typhoon_track_")
    assert Path(first_src).suffix == ".jpg"

    second_ctx = _Ctx()
    await plugin.commands["台风"]["handler"](second_ctx, "2501")
    second_src = second_ctx.sent[-1][0]["children"][1]["children"][1]["attrs"]["src"]

    assert first_src != second_src
    assert Path(second_src).name.startswith("typhoon_track_")
    assert Path(second_src).suffix == ".jpg"


@pytest.mark.asyncio
async def test_typhoon_command_sends_track_image_when_bulletin_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.commands as commands

    async def fake_download(url: str, dest: str) -> None:
        Path(dest).write_bytes(url.encode())

    async def fetch_html() -> str:
        return _NMC_SAMPLE_HTML

    async def fetch_track_html(url: str) -> str:
        if url.endswith("probability-img1.html"):
            return _NMC_TRACK_BAVI_HTML
        return _NMC_TRACK_SAMPLE_HTML

    provider = NmcTyphoonNewsProvider(fetch_html=fetch_html, fetch_track_html=fetch_track_html)
    monkeypatch.setattr(commands, "download_typhoon_track_image", fake_download)
    plugin = _register(tmp_path, typhoon_provider=provider)
    ctx = _Ctx()

    await plugin.commands["台风"]["handler"](ctx, "美莎克")

    assert ctx.stopped
    folded = ctx.sent[-1][0]
    assert folded["type"] == "message"
    assert folded["attrs"] == {"forward": "true"}
    assert len(folded["children"]) == 1
    assert "美莎克路径预报图" in folded["children"][0]["children"][0]["attrs"]["content"]
    image_src = folded["children"][0]["children"][1]["attrs"]["src"]
    assert Path(image_src).name.startswith("typhoon_track_")
    assert Path(image_src).suffix == ".jpg"


@pytest.mark.asyncio
async def test_typhoon_command_falls_back_without_onebot_forward_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.shinbot_plugin_astroassist.shinbot_plugin_astroassist.commands as commands

    async def fake_download(url: str, dest: str) -> None:
        Path(dest).write_bytes(url.encode())

    class Provider:
        async def list_active(self) -> list[TyphoonSummary]:
            return []

        async def get_detail(self, query: str) -> TyphoonDetail:
            return TyphoonDetail(summary=TyphoonSummary(identifier=query, name="测试台风"))

        async def get_track_image(self, query: str = "") -> TyphoonTrackImage:
            assert query == "测试台风"
            return TyphoonTrackImage(
                url="https://image.nmc.cn/product/typhoon.JPG?v=1",
                time="07/06 17:00",
                name="测试台风",
            )

    monkeypatch.setattr(commands, "download_typhoon_track_image", fake_download)
    plugin = _register(tmp_path, typhoon_provider=Provider())
    ctx = _Ctx(adapter_platform="satori")

    await plugin.commands["台风"]["handler"](ctx, "2501")

    assert ctx.stopped
    assert len(ctx.sent) == 3
    assert "2501 测试台风" in ctx.sent[0]
    assert "测试台风路径预报图" in ctx.sent[1]
    assert ctx.sent[2][0]["type"] == "img"
    image_src = ctx.sent[2][0]["attrs"]["src"]
    assert Path(image_src).name.startswith("typhoon_track_")
    assert Path(image_src).suffix == ".jpg"
