"""Kỷ luật token: chặn live, cache, ngân sách, đơn giá, record -> replay. Không gọi mạng."""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.llm.metered import LiveCallBlocked, MeteredProvider, RunState
from app.llm.pricing import Pricing
from app.llm.store import Ledger, request_digest
from app.providers.base import ExtractionRequest, ExtractionResult, PageInput
from app.providers.mock import MockProvider


class FakeLive(MockProvider):
    """Giả provider trả phí: is_live() = True vì name != 'mock'."""

    name = "fakelive"

    def __init__(self) -> None:
        super().__init__(responses={"GiayPhep": {"loai_van_ban": "KHAC"}})
        self.model = "m-cheap"

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        res = await super().extract(req)
        return res.model_copy(
            update={"provider": self.name, "model": "m-cheap", "input_tokens": 1000, "output_tokens": 500}
        )


def _settings(tmp_path: Path, **kw: object) -> Settings:
    pricing = tmp_path / "pricing.toml"
    if not pricing.exists():
        pricing.write_text('[models."m-cheap"]\ninput = 1.0\noutput = 2.0\n', encoding="utf-8")
    base: dict[str, object] = {
        "data_dir": tmp_path,
        "pricing_file": pricing,
        "llm_fixtures_dir": tmp_path / "fixtures",
        "allow_live_llm": True,
    }
    return Settings(**(base | kw))


def _req(text: str = "trang 1") -> ExtractionRequest:
    return ExtractionRequest(
        system_prompt="s", user_prompt="u", pages=[PageInput(page_no=1, text=text)], json_schema={}
    )


async def test_live_blocked_without_flag(tmp_path: Path) -> None:
    inner = FakeLive()
    p = MeteredProvider(inner, _settings(tmp_path, allow_live_llm=False))
    with pytest.raises(LiveCallBlocked, match="ALLOW_LIVE_LLM"):
        await p.extract(_req())
    assert inner.calls == []


async def test_cache_hit_avoids_second_call_and_cost_logged(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    inner = FakeLive()
    run = RunState()
    p = MeteredProvider(inner, s, run=run)
    await p.extract(_req())
    await p.extract(_req())
    assert len(inner.calls) == 1
    assert [r.cache_hit for r in run.records] == [False, True]
    assert run.cost_vnd == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)
    assert Ledger(s.ledger_dir).spent() == pytest.approx(0.002)
    # Cache hit vẫn dùng được khi đã tắt cờ live
    p2 = MeteredProvider(inner, s.model_copy(update={"allow_live_llm": False}))
    await p2.extract(_req())
    assert len(inner.calls) == 1


async def test_max_calls_per_run(tmp_path: Path) -> None:
    p = MeteredProvider(FakeLive(), _settings(tmp_path, llm_max_calls_per_run=1))
    await p.extract(_req("a"))
    with pytest.raises(LiveCallBlocked, match="LLM_MAX_CALLS_PER_RUN"):
        await p.extract(_req("b"))


async def test_daily_budget(tmp_path: Path) -> None:
    s = _settings(tmp_path, llm_daily_budget_vnd=0.003)
    p = MeteredProvider(FakeLive(), s)
    await p.extract(_req("a"))
    await p.extract(_req("b"))  # đã dùng 0.002 < 0.003 -> vẫn gọi
    with pytest.raises(LiveCallBlocked, match="LLM_DAILY_BUDGET_VND"):
        await p.extract(_req("c"))


async def test_unknown_price_blocked(tmp_path: Path) -> None:
    (tmp_path / "pricing.toml").write_text("[models]\n", encoding="utf-8")
    inner = FakeLive()
    with pytest.raises(LiveCallBlocked, match="đơn giá"):
        await MeteredProvider(inner, _settings(tmp_path, llm_require_pricing=True)).extract(_req())
    assert inner.calls == []
    # Không bắt buộc đơn giá: vẫn gọi, chi phí 0
    run = RunState()
    await MeteredProvider(inner, _settings(tmp_path), run=run).extract(_req())
    assert len(inner.calls) == 1 and run.cost_vnd == 0


def test_pricing_prefix_default_and_cache_rate(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text(
        '[models."gem-flash"]\ninput = 1\noutput = 4\ncached_input = 0.25\n'
        "[default]\ninput = 9\noutput = 9\n",
        encoding="utf-8",
    )
    pr = Pricing.load(f)
    assert pr.get("models/gem-flash-001") == pr.models["gem-flash"]
    assert pr.get("khac") == pr.default
    assert pr.cost("gem-flash", 1_000_000, 0, cached_input_tokens=400_000) == pytest.approx(0.6 + 0.1)


async def test_record_then_mock_replay(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rec = MeteredProvider(FakeLive(), s, record=True, record_label="BAO X/a.pdf")
    await rec.extract(_req())
    files = list((tmp_path / "fixtures").glob("GiayPhep_*.json"))
    assert len(files) == 1
    mock = MockProvider(fixtures_dir=tmp_path / "fixtures")
    res = await mock.extract(_req())
    assert mock.replayed == 1 and res.data == {"loai_van_ban": "KHAC"}


def test_digest_uses_image_id_not_bytes() -> None:
    a = _req()
    a.pages[0].image, a.pages[0].image_id = b"jpeg-1", "sha:1"
    b = a.model_copy(deep=True)
    b.pages[0].image = b"jpeg-2"  # Pillow khác phiên bản -> bytes khác
    assert request_digest(a) == request_digest(b)


def test_pricing_vnd_default_usd_conversion_provider_key_and_min_request(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    body = """
[models."router/gem"]
input = 400
output = 1600
min_per_request = 3

[models."gem"]
currency = "USD"
input = 1
output = 2
"""
    f.write_text(body, encoding="utf-8")
    pr = Pricing.load(f)
    assert pr.get("gem", "gemini") is None  # giá USD thiếu vnd_per_usd -> bỏ qua
    # giá đồng (mặc định): 1M in + 1M out = 2000 đ; request nhỏ -> tối thiểu 3 đ
    assert pr.cost("gem", 1_000_000, 1_000_000, provider="router") == pytest.approx(2000)
    assert pr.cost("gem", 10, 10, provider="router") == pytest.approx(3)
    f.write_text("vnd_per_usd = 25000" + body, encoding="utf-8")
    pr = Pricing.load(f)
    assert pr.cost("gem", 1_000_000, 1_000_000, provider="gemini") == pytest.approx(3 * 25000)
