import pytest

from wlcodex.auto_digest_llm import (
    DeepSeekDigestConfig,
    DeepSeekDigestCompletion,
    DeepSeekDigestUsage,
    render_auto_draft_digest_with_llm,
)


def test_deepseek_digest_config_requires_explicit_enable_for_generic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WLCODEX_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-generic")
    monkeypatch.delenv("WLCODEX_AUTO_DIGEST_LLM", raising=False)

    config = DeepSeekDigestConfig.from_env()

    assert config.api_key == "sk-generic"
    assert config.enabled is False


def test_deepseek_digest_config_auto_enables_scoped_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WLCODEX_DEEPSEEK_API_KEY", "sk-wlcodex")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-generic")
    monkeypatch.delenv("WLCODEX_AUTO_DIGEST_LLM", raising=False)

    config = DeepSeekDigestConfig.from_env()

    assert config.api_key == "sk-wlcodex"
    assert config.enabled is True


@pytest.mark.asyncio
async def test_llm_digest_uses_flash_first() -> None:
    calls: list[str] = []

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        calls.append(model)
        return (
            '{"title":"方案摘要","primary_label":"方案","primary":"在 README 新增 Documentation Map。",'
            '"evidence_label":"依据","evidence_items":["README 是入口文档。"],'
            '"risk_label":"风险","risk":"低，只改文档。",'
            '"next_label":"下一步","next":"可以交给开发工程师执行。"}'
        )

    digest = await render_auto_draft_digest_with_llm(
        "最终方案：我会先用 GitNexus 看文档结构，然后改 README。",
        digest_kind="design",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert calls == ["deepseek-v4-flash"]
    assert digest.startswith("方案摘要：")
    assert "方案：在 README 新增 Documentation Map。" in digest
    assert "我会先用 GitNexus" not in digest


@pytest.mark.asyncio
async def test_llm_digest_prompt_includes_full_source_by_default() -> None:
    prompts: list[str] = []
    tail_marker = "PROMPT_FULL_SOURCE_TAIL_AFTER_12000"

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        prompts.append(prompt)
        return (
            '{"title":"方案摘要","primary_label":"方案","primary":"保留完整原文后再摘要。",'
            '"evidence_label":"依据","evidence_items":["原文尾部进入 DeepSeek prompt。"],'
            '"risk_label":"风险","risk":"低。",'
            '"next_label":"下一步","next":"可以交给开发工程师执行。"}'
        )

    source = "最终方案：长文摘要。\n" + ("很长的背景。" * 1800) + tail_marker

    digest = await render_auto_draft_digest_with_llm(
        source,
        digest_kind="design",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert digest.startswith("方案摘要：")
    assert prompts
    assert tail_marker in prompts[0]


@pytest.mark.asyncio
async def test_llm_digest_real_call_uses_explicit_config_not_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_call(
        *,
        model: str,
        prompt: str,
        timeout_seconds: float,
        config: DeepSeekDigestConfig,
    ) -> DeepSeekDigestCompletion:
        captured.update(
            {
                "model": model,
                "timeout_seconds": timeout_seconds,
                "api_key": config.api_key,
                "base_url": config.base_url,
            }
        )
        return DeepSeekDigestCompletion(
            content=(
                '{"title":"方案摘要","primary_label":"方案","primary":"使用显式配置调用 DeepSeek。",'
                '"evidence_label":"依据","evidence_items":["测试注入了专用 base_url。"],'
                '"risk_label":"风险","risk":"低。",'
                '"next_label":"下一步","next":"可以继续。"}'
            ),
            usage=DeepSeekDigestUsage(model=model, input_tokens=10, output_tokens=5, total_tokens=15),
        )

    monkeypatch.setenv("WLCODEX_DEEPSEEK_API_KEY", "sk-env")
    monkeypatch.setattr(
        "wlcodex.auto_digest_llm._call_deepseek",
        fake_call,
        raising=False,
    )

    digest = await render_auto_draft_digest_with_llm(
        "最终方案：使用显式 DeepSeek 配置。",
        digest_kind="design",
        config=DeepSeekDigestConfig(
            enabled=True,
            api_key="sk-config",
            base_url="https://config.deepseek.test",
            flash_model="deepseek-config-flash",
            timeout_seconds=1.5,
        ),
    )

    assert digest.startswith("方案摘要：")
    assert captured["model"] == "deepseek-config-flash"
    assert captured["api_key"] == "sk-config"
    assert captured["base_url"] == "https://config.deepseek.test"
    assert captured["timeout_seconds"] == 1.5


@pytest.mark.asyncio
async def test_llm_digest_falls_back_to_pro_when_flash_output_is_rejected() -> None:
    calls: list[str] = []

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        calls.append(model)
        if model == "deepseek-v4-flash":
            return (
                '{"title":"执行摘要","primary_label":"结果",'
                '"primary":"我会用 GitNexus 和 writing-plans 处理。",'
                '"evidence_label":"改动","evidence_items":["team_artifact=17"],'
                '"risk_label":"验证","risk":"未知",'
                '"next_label":"下一步","next":"继续。"}'
            )
        return (
            '{"title":"执行摘要","primary_label":"结果","primary":"文档-only小任务已完成，测试通过。",'
            '"evidence_label":"改动","evidence_items":["在 README.md 新增 Documentation Map。"],'
            '"risk_label":"验证","risk":"pytest tests/test_telegram_digest.py -q 通过。",'
            '"next_label":"下一步","next":"可以结束任务，或继续补充。"}'
        )

    digest = await render_auto_draft_digest_with_llm(
        "开发完成，测试通过。\n结论：我会用 GitNexus 和 writing-plans 处理。",
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert calls == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert digest.startswith("执行摘要：")
    assert "结果：文档-only小任务已完成，测试通过。" in digest
    assert "我会用 GitNexus" not in digest
    assert "team_artifact" not in digest


@pytest.mark.asyncio
async def test_llm_digest_uses_local_template_title_when_model_title_varies() -> None:
    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        return (
            '{"title":"LightFee 本地状态残留 ALTUSDT 持仓","primary_label":"结论",'
            '"primary":"本地状态仍显示 ALTUSDT 有 1 笔 open position，真实交易所没有非零持仓。",'
            '"evidence_label":"依据","evidence_items":["Binance 非零持仓为空","Bybit 开放订单为 0"],'
            '"risk_label":"风险","risk":"高。状态未收敛会影响风控判断。",'
            '"next_label":"下一步","next":"排查状态收敛和本地持仓清理路径。"}'
        )

    digest = await render_auto_draft_digest_with_llm(
        "结论：本地状态仍显示 ALTUSDT 有 1 笔 open position。",
        digest_kind="diagnosis",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert digest.startswith("关键摘要：")
    assert "LightFee 本地状态残留" not in digest
    assert "Binance 非零持仓为空" in digest


@pytest.mark.asyncio
async def test_llm_digest_falls_back_to_rules_when_models_fail() -> None:
    calls: list[str] = []

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        calls.append(model)
        return "not json"

    digest = await render_auto_draft_digest_with_llm(
        "开发完成，测试通过。\n"
        "结论：我会用 GitNexus 先理解项目，然后只改文档。\n"
        "依据：在 README.md 新增 Documentation Map。\n"
        "风险：未明确风险。",
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert calls == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert digest.startswith("执行摘要：")
    assert "结果：" in digest
    assert "我会用 GitNexus" not in digest


@pytest.mark.asyncio
async def test_llm_digest_records_deepseek_token_usage_for_long_to_short_summary() -> None:
    usage_records: list[DeepSeekDigestUsage] = []

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> DeepSeekDigestCompletion:
        return DeepSeekDigestCompletion(
            content=(
                '{"title":"执行摘要","primary_label":"结果","primary":"文档-only小任务已完成，测试通过。",'
                '"evidence_label":"改动","evidence_items":["在 README.md 新增 Documentation Map。"],'
                '"risk_label":"验证","risk":"pytest tests/test_telegram_digest.py -q 通过。",'
                '"next_label":"下一步","next":"可以结束任务。"}'
            ),
            usage=DeepSeekDigestUsage(
                model=model,
                digest_kind="implementation",
                input_tokens=1234,
                output_tokens=88,
                total_tokens=1322,
                latency_ms=456,
            ),
        )

    source = "开发完成，测试通过。\n" + ("很长的执行正文。" * 400)

    digest = await render_auto_draft_digest_with_llm(
        source,
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
        usage_recorder=usage_records.append,
    )

    assert digest.startswith("执行摘要：")
    assert len(digest) < len(source)
    assert len(usage_records) == 1
    assert usage_records[0].model == "deepseek-v4-flash"
    assert usage_records[0].input_tokens == 1234
    assert usage_records[0].output_tokens == 88
    assert usage_records[0].total_tokens == 1322
    assert usage_records[0].source_chars == len(source)
    assert usage_records[0].digest_chars == len(digest)
    assert usage_records[0].status == "accepted"


@pytest.mark.asyncio
async def test_llm_digest_completes_missing_implementation_change_files_from_source() -> None:
    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        return (
            '{"title":"执行摘要","primary_label":"结果","primary":"文档-only小任务已完成，测试通过。",'
            '"evidence_label":"改动",'
            '"evidence_items":["更新验证策略文档：docs/testing-validation-strategy.md，新增 Documentation-Only Changes 小节。"],'
            '"risk_label":"验证","risk":"运行 smoke profile 验证通过：compileall 通过，git diff --check 通过，Validation passed。",'
            '"next_label":"下一步",'
            '"next":"可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。"}'
        )

    source = (
        "开发完成，测试通过。\n\n"
        "生成并落地了最终方案：[2026-05-26-doc-only-validation-guidance-implementation-plan.md]"
        "(/media/wl/新加卷/codex/LightFeeV2/docs/superpowers/plans/"
        "2026-05-26-doc-only-validation-guidance-implementation-plan.md:1)\n\n"
        "同时更新了验证策略文档：[docs/testing-validation-strategy.md]"
        "(/media/wl/新加卷/codex/LightFeeV2/docs/testing-validation-strategy.md:43)，"
        "新增 `Documentation-Only Changes` 小节。\n\n"
        "当前变更范围：\n"
        "```text\n"
        " M docs/testing-validation-strategy.md\n"
        "?? docs/superpowers/plans/2026-05-26-doc-only-validation-guidance-implementation-plan.md\n"
        "```\n"
    )

    digest = await render_auto_draft_digest_with_llm(
        source,
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert "docs/testing-validation-strategy.md" in digest
    assert "docs/superpowers/plans/2026-05-26-doc-only-validation-guidance-implementation-plan.md" in digest
    assert "交给 DeepSeek 开发工程师" not in digest
    assert "处理上述问题" not in digest
    assert "下一步：可以结束任务，或继续补充。" in digest


@pytest.mark.asyncio
async def test_llm_digest_prioritizes_source_change_files_over_extra_model_items() -> None:
    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        return (
            '{"title":"执行摘要","primary_label":"结果","primary":"文档-only小任务已完成，测试通过。",'
            '"evidence_label":"改动",'
            '"evidence_items":["补充说明 A。","补充说明 B。","补充说明 C。","补充说明 D。","补充说明 E。"],'
            '"risk_label":"验证","risk":"Validation passed。",'
            '"next_label":"下一步","next":"可以结束任务，或继续补充。"}'
        )

    digest = await render_auto_draft_digest_with_llm(
        "开发完成，测试通过。\n当前变更范围：\n"
        " M docs/testing-validation-strategy.md\n"
        "?? docs/superpowers/plans/doc-only-plan.md\n",
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
    )

    assert "docs/testing-validation-strategy.md" in digest
    assert "docs/superpowers/plans/doc-only-plan.md" in digest
    assert digest.count("- ") <= 5


@pytest.mark.asyncio
async def test_llm_digest_preserves_failed_attempt_status_in_usage() -> None:
    usage_records: list[DeepSeekDigestUsage] = []

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> DeepSeekDigestCompletion:
        if model == "deepseek-v4-flash":
            return DeepSeekDigestCompletion(
                content="",
                usage=DeepSeekDigestUsage(
                    model=model,
                    status="request_failed",
                    latency_ms=321,
                ),
            )
        return DeepSeekDigestCompletion(
            content=(
                '{"title":"执行摘要","primary_label":"结果","primary":"文档-only小任务已完成，测试通过。",'
                '"evidence_label":"改动","evidence_items":["在 README.md 新增 Documentation Map。"],'
                '"risk_label":"验证","risk":"pytest tests/test_telegram_digest.py -q 通过。",'
                '"next_label":"下一步","next":"可以结束任务。"}'
            ),
            usage=DeepSeekDigestUsage(
                model=model,
                status="api_ok",
                input_tokens=100,
                output_tokens=40,
                total_tokens=140,
            ),
        )

    digest = await render_auto_draft_digest_with_llm(
        "开发完成，测试通过。\n改动：README.md。",
        digest_kind="implementation",
        config=DeepSeekDigestConfig(enabled=True),
        client=client,
        usage_recorder=usage_records.append,
    )

    assert digest.startswith("执行摘要：")
    assert [record.model for record in usage_records] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert usage_records[0].status == "request_failed"
    assert usage_records[0].failure_reason == "request_failed"
    assert usage_records[1].status == "accepted"
