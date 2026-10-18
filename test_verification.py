#!/usr/bin/env python3
"""Jev 插件完整验证套件（mock 化，可离线重复跑，不烧 token）。

运行：python test_verification.py
所有在线路径通过 monkeypatch `_execute_system_one_with_timeout` 注入假响应，
保证 CI / 离线环境也能完整覆盖逻辑分支。
"""
import json
import sys
import types
from pathlib import Path

# 导入插件模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
import __init__ as jmod


# ---------- Mock 基础设施 ----------
def make_fake_response(nouls=None, choices=None, scores=None):
    """构造一个假的 system_one 返回值。"""
    ns = types.SimpleNamespace()
    ns.nouls = {k: types.SimpleNamespace(noul=v) for k, v in (nouls or {}).items()}
    ns.choices = {k: types.SimpleNamespace(choice=v) for k, v in (choices or {}).items()}
    ns.scores = {k: types.SimpleNamespace(score=v) for k, v in (scores or {}).items()}
    return ns


def patch_online(resp, side_effect=None):
    """临时替换在线执行函数；side_effect 可模拟超时/异常。"""
    def fake(client, state, questions, provider, timeout):
        if side_effect:
            side_effect()
        return resp
    orig = jmod._execute_system_one_with_timeout
    jmod._execute_system_one_with_timeout = fake
    return orig


def restore_online(orig):
    jmod._execute_system_one_with_timeout = orig


# ---------- 测试用例 ----------
def test_availability():
    """有 key + adapter 时装载可用；无 key 时工具不可用（调用时直接报错，不编造结论）。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake-key-for-test"
    assert jmod._jev_tool_available() is True
    jmod._get_agnes_key = lambda: ""
    assert jmod._jev_tool_available() is False
    assert "不要每轮调用" in jmod.JEV_SCHEMA["description"]
    assert "不会替你调用工具" in jmod.JEV_SCHEMA["description"]
    assert "无关个人内容" in jmod.JEV_SCHEMA["parameters"]["properties"]["state"]["description"]
    guard_description = jmod.JEV_GUARD_SCHEMA["description"]
    assert "普通低风险工具调用无需逐次运行 guard" in guard_description
    assert "不要当作放行凭据" in guard_description
    assert "移除密钥、令牌、密码" in jmod.JEV_GUARD_SCHEMA["parameters"]["properties"]["state"]["description"]
    print("Availability and on-demand routing guidance: PASSED")


def test_no_engine_returns_explicit_error():
    """无可用引擎（无 key / adapter 缺失）：jev 与 guard 都直接报错，不给 verdict、不编造结论。"""
    jmod._adapter_available = False
    jmod._get_agnes_key = lambda: ""
    assert jmod._active_engine() == "none"
    assert jmod._jev_tool_available() is False

    r = json.loads(jmod._handle_jev({
        "state": "无引擎测试",
        "noul": {"name": "q", "instructions": "是否？"},
        "no_cache": True,
    }))
    assert "error" in r and r["engine"] == "none", r
    assert "verdict" not in r and "answers" not in r, r

    rg = json.loads(jmod._handle_jev_guard({"state": "删除旧缓存文件", "no_cache": True}))
    assert "error" in rg and rg["engine"] == "none", rg
    assert "verdict" not in rg, rg  # 不再有本地离线兜底判定

    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    print("No engine -> explicit error (no local fallback): PASSED")


def test_guard_online_three_tiers():
    """在线 guard 三档：prob>=threshold→allow，中间→ask，<deny_threshold→deny。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"

    cases = [
        (0.90, "allow"),
        (0.50, "ask"),
        (0.10, "deny"),
    ]
    for prob, expect in cases:
        orig = patch_online(make_fake_response(nouls={"guard_q": prob}))
        r = json.loads(jmod._handle_jev_guard({
            "state": f"在线三档测试 prob={prob}",
            "guard": {"threshold": 0.75},
            "no_cache": True,
        }))
        restore_online(orig)
        assert r["verdict"] == expect, (prob, r)
        assert "deny_threshold" in r, r
    print("Guard online three-tier (allow/ask/deny): PASSED")


def test_guard_online_timeout_stops_clean():
    """在线 guard 超时 → 直接返回 error，不做本地兜底判定。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    import concurrent.futures
    def raise_timeout():
        raise concurrent.futures.TimeoutError()
    orig = patch_online(None, side_effect=raise_timeout)
    r = json.loads(jmod._handle_jev_guard({
        "state": "执行 rm -rf / 清空所有数据",
        "no_cache": True,
        "timeout_seconds": 0.5,
    }))
    restore_online(orig)
    assert "error" in r and "超时" in r["error"], r
    assert "verdict" not in r, r
    print("Guard online timeout (explicit error, no fallback): PASSED")


def test_guard_threshold_clamp():
    """threshold 越界 clamp：5.0→1.0，-1→0.0，\"abc\"→默认 0.75。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.9}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": 5.0}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 1.0, r

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.5}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": -1}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 0.0, r

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.5}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": "abc"}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 0.75, r
    print("Guard threshold clamp: PASSED")


def test_jev_multi_question_mock():
    """多题并发打满（mock）：3 题一次往返全返回。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    orig = patch_online(make_fake_response(
        nouls={"q1": 1.0, "q2": 0.0, "q3": 0.5},
        choices={"route": "coder"},
        scores={"complexity": "高"},
    ))
    r = json.loads(jmod._handle_jev({
        "state": "批量评估测试",
        "nouls": [
            {"name": "q1", "instructions": "是否安全？"},
            {"name": "q2", "instructions": "是否可逆？"},
        ],
        "noul": {"name": "q3", "instructions": "是否需要确认？"},
        "choice": {"name": "route", "instructions": "优先调用哪个专家？", "criteria": {"coder": "写代码", "searcher": "查资料"}},
        "score": {"name": "complexity", "instructions": "复杂度？", "criteria": ["低", "中", "高"]},
        "no_cache": True,
    }))
    restore_online(orig)
    assert "answers" in r, r
    assert r["answers"]["q1"] == 1.0 and r["answers"]["q2"] == 0.0 and r["answers"]["q3"] == 0.5, r["answers"]
    assert r["answers"]["route"] == "coder", r["answers"]
    assert r["answers"]["complexity"] == "高", r["answers"]
    assert r["choice_answer"] == "coder", r
    assert r["noul_answer"] == 0.5, r
    assert r["score_answer"] == "高", r
    print(f"Multi-question batching (mock): PASSED ({len(r['answers'])} answers)")


def test_jev_timeout_circuit_breaker():
    """jev 主工具超时熔断（mock）：超时直接返回 error，不编造结论。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    import concurrent.futures
    def raise_timeout():
        raise concurrent.futures.TimeoutError()
    orig = patch_online(None, side_effect=raise_timeout)
    r = json.loads(jmod._handle_jev({
        "state": "超时熔断测试",
        "noul": {"name": "t", "instructions": "是否触发？"},
        "no_cache": True,
        "timeout_seconds": 0.5,
    }))
    restore_online(orig)
    assert "error" in r and "超时" in r["error"], r
    assert "answers" not in r, r
    print("Timeout circuit breaker: PASSED")


def test_jev_error_fallback():
    """jev 在线异常（mock）：如实报错，不降级为本地提示。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    def raise_err():
        raise RuntimeError("boom")
    orig = patch_online(None, side_effect=raise_err)
    r = json.loads(jmod._handle_jev({
        "state": "异常降级测试",
        "noul": {"name": "e", "instructions": "是否异常？"},
        "no_cache": True,
    }))
    restore_online(orig)
    assert "error" in r and "boom" in r["error"], r
    assert "answers" not in r, r
    print("Online error reporting (no local fallback): PASSED")


def test_disk_persistent_cache():
    """磁盘持久化缓存（纯本地，不触网）：写盘 → 重新加载 → 0ms 命中。"""
    args = {"state": "磁盘缓存验证-mock", "noul": {"name": "persist", "instructions": "是否持久化？"}, "timeout_seconds": 30.0}
    key = jmod._get_cache_key(args)
    jmod._DECISION_CACHE.pop(key, None)

    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    orig = patch_online(make_fake_response(nouls={"persist": 1.0}))
    r1 = json.loads(jmod._handle_jev(args))
    restore_online(orig)
    assert r1["cache_hit"] is False, r1
    # 磁盘写已移到线程池：等它落盘（最多 2s）
    import time as _time
    for _ in range(40):
        if jmod._CACHE_FILE.exists() and jmod._CACHE_FILE.stat().st_size > 0:
            break
        _time.sleep(0.05)
    assert jmod._CACHE_FILE.exists() and jmod._CACHE_FILE.stat().st_size > 0

    import importlib
    importlib.reload(jmod)
    import __init__ as reloaded
    r2 = json.loads(reloaded._handle_jev(args))
    assert r2["cache_hit"] is True, r2
    assert r2["latency_seconds"] == 0.0
    print("Disk persistent cache: PASSED (0ms hit after reload)")


def test_journal_log():
    """审计黑匣子日志：每次判定追加 JSONL 流水。"""
    from __init__ import _JOURNAL_FILE
    journal = _JOURNAL_FILE
    before = 0 if not journal.exists() else len(journal.read_text(encoding="utf-8").splitlines())
    jmod._log_journal({"tool": "jev", "test_entry": True})
    after = len(journal.read_text(encoding="utf-8").splitlines())
    assert after == before + 1, (before, after)
    last = json.loads(journal.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert last.get("test_entry") is True
    assert "timestamp" in last
    print(f"Journal log: PASSED ({after} total entries)")


def test_no_engine_error_not_cached():
    """无引擎的 error 结果不写缓存（避免把失败固化，恢复后仍要走真实裁决）。"""
    jmod._adapter_available = False
    jmod._get_agnes_key = lambda: ""
    args = {"state": "no-engine-cache-test", "noul": {"name": "oc", "instructions": "测试？"}, "timeout_seconds": 1.0}
    key = jmod._get_cache_key(args)
    jmod._DECISION_CACHE.pop(key, None)
    r = json.loads(jmod._handle_jev(args))
    assert "error" in r, r
    assert key not in jmod._DECISION_CACHE, "无引擎的 error 不应被缓存"
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    print("No-engine error not cached: PASSED")


def test_semantic_cache_key_isolation():
    """语义缓存键防污染测试：改变 timeout_seconds 不影响 cache key"""
    args1 = {"state": "同语义状态", "noul": {"name": "q", "instructions": "测试"}, "timeout_seconds": 5.0}
    args2 = {"state": "同语义状态", "noul": {"name": "q", "instructions": "测试"}, "timeout_seconds": 15.0}
    args3 = {"state": "同语义状态", "noul": {"name": "q", "instructions": "测试"}, "extra_ctrl": "dummy"}
    key1 = jmod._get_cache_key(args1)
    key2 = jmod._get_cache_key(args2)
    key3 = jmod._get_cache_key(args3)
    assert key1 == key2 == key3, f"Cache keys should match: {key1} vs {key2}"
    print("Semantic cache key isolation: PASSED")


def test_guard_low_threshold_dynamic_deny():
    """在线 guard 激进超低阈值动态收缩测试：threshold=0.2 时 deny_threshold 动态变为 0.1"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"

    # prob=0.15: 介于 0.1 与 0.2 之间，必须判定为 ask，而绝不能被吃掉或误判为 deny/allow
    orig = patch_online(make_fake_response(nouls={"guard_q": 0.15}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "低阈值边界判定",
        "guard": {"threshold": 0.2},
        "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 0.2, r
    assert r["deny_threshold"] == 0.1, r
    assert r["verdict"] == "ask", r
    print("Guard low threshold dynamic deny: PASSED")

# ---------- 捕获型 mock：校验发往在线引擎的 state 已脱敏 ----------
import contextlib


@contextlib.contextmanager
def capture_online(resp):
    """替换在线执行函数并记录传入参数（离线跑，不触网、不烧 token）。"""
    captured = {}

    def fake(client, state, questions, provider, timeout):
        captured["state"] = state
        captured["questions"] = questions
        return resp

    orig = jmod._execute_system_one_with_timeout
    jmod._execute_system_one_with_timeout = fake
    try:
        yield captured
    finally:
        jmod._execute_system_one_with_timeout = orig


def test_redaction_before_online_judgment():
    """凭据脱敏：state 里的 key/token/password 不会原样发给在线引擎。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    secret_state = (
        "curl -H 'Authorization: Bearer sk-abcdefghijklmnop' "
        "&& export GITHUB_TOKEN=github_pat_abcdefghijklmnopqrst && password=hunter2secret"
    )
    with capture_online(make_fake_response(nouls={"q": 1.0})) as captured:
        jmod._handle_jev({"state": secret_state, "noul": {"name": "q", "instructions": "安全？"}, "no_cache": True})

    sent = captured["state"]
    blob = json.dumps(sent, ensure_ascii=False)
    assert "sk-abcdefghijklmnop" not in blob, blob
    assert "github_pat_abcdefghijklmnopqrst" not in blob, blob
    assert "hunter2secret" not in blob, blob
    assert "[REDACTED]" in blob, blob

    # 结构型 state：按键名脱敏（api_key / token / secret / password …）
    structured = jmod._redact({"cmd": "run", "api_key": "abcdef1234567890", "nested": {"authorization": "xyz"}})
    dumped = json.dumps(structured, ensure_ascii=False)
    assert "abcdef1234567890" not in dumped, dumped
    assert "xyz" not in dumped, dumped
    assert "run" in dumped, "非敏感字段必须保留"
    print("Secret redaction before online judgment: PASSED")


def test_guard_multi_signal_verdict():
    """guard 六信号确定性裁决：读凭据→deny、自我游说→ask、策略允许+低破坏→allow、破坏大→ask。"""
    cases = [
        ({"policy_allows": 0.9, "reads_secrets": 0.8, "sends_outbound": 0.1, "self_advocating": 0.1, "blast_radius": 1.0}, "deny"),
        ({"policy_allows": 0.9, "reads_secrets": 0.1, "sends_outbound": 0.1, "self_advocating": 0.7, "blast_radius": 0.0}, "ask"),
        ({"policy_allows": 0.9, "reads_secrets": 0.1, "sends_outbound": 0.1, "self_advocating": 0.1, "blast_radius": 0.0}, "allow"),
        ({"policy_allows": 0.9, "reads_secrets": 0.1, "sends_outbound": 0.1, "self_advocating": 0.1, "blast_radius": 1.8}, "ask"),
        ({"policy_allows": 0.2, "reads_secrets": 0.1, "sends_outbound": 0.1, "self_advocating": 0.1, "blast_radius": 0.0}, "ask"),
    ]
    for signals, expect in cases:
        got = jmod._guard_verdict_from_signals(signals, 0.75)
        assert got is not None and got[0] == expect, (signals, got, expect)
    # 信号不全 → 返回 None（交回旧三档）
    assert jmod._guard_verdict_from_signals({"guard_q": 0.9}, 0.75) is None

    # 端到端（mock 在线响应带 6 信号）
    sig = {"policy_allows": 0.95, "reads_secrets": 0.05, "sends_outbound": 0.05, "self_advocating": 0.05, "blast_radius": 0.0}
    resp = make_fake_response(
        nouls={k: v for k, v in sig.items() if k != "blast_radius"} | {"guard_q": 0.9},
        scores={"blast_radius": sig["blast_radius"]},
    )
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    orig = patch_online(resp)
    r = json.loads(jmod._handle_jev_guard({"state": "多信号 guard", "no_cache": True}))
    restore_online(orig)
    assert r["verdict"] == "allow", r
    assert r["verdict_source"] == "signals", r
    assert r["signals"]["blast_radius"] == 0.0, r
    print("Guard multi-signal deterministic verdict: PASSED")


def test_jev_usage_serialization():
    """回归：SDK Usage 对象必须转 dict，否则 json.dumps 崩掉整条结果、在线降 advisory（2026-10-18 实弹发现）。"""
    class FakeUsage:
        input_tokens = 268
        output_tokens = 41
        def model_dump(self):
            return {"input_tokens": 268, "output_tokens": 41}

    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    resp = make_fake_response(choices={"route": "searcher"}, nouls={"risky": 0.2}, scores={"effort": 1.0})
    resp.usage = FakeUsage()
    resp.engine = "agnes"
    resp.model = "agnes-3.0-flash"
    args = {
        "state": "usage 序列化回归",
        "choice": {"name": "route", "criteria": {"searcher": "查资料", "coder": "写代码"}},
        "noul": {"name": "risky", "instructions": "有风险?"},
        "score": {"name": "effort", "criteria": ["low", "mid", "high"]},
        "no_cache": True,
    }
    orig = patch_online(resp)
    r = json.loads(jmod._handle_jev(args))  # Usage 对象混进来会在这里炸
    restore_online(orig)
    assert r["answers"]["route"] == "searcher" and r["answers"]["risky"] == 0.2
    assert r["usage"] == {"input_tokens": 268, "output_tokens": 41}, r.get("usage")
    print("Jev usage serialization (SDK object -> dict): PASSED")


def test_redaction_structured_and_new_patterns():
    """敏感键下的 dict/list 值、以及 Google/Slack/JWT/PEM 形态密钥都必须被遮蔽。"""
    structured = jmod._redact({
        "api_key": {"inner": "abc1234567890"},
        "token": ["x" * 20],
        "note": "ok",
    })
    blob = json.dumps(structured, ensure_ascii=False)
    assert "abc1234567890" not in blob and "xxxxxxxx" not in blob, blob
    assert structured["note"] == "ok", structured

    # 假样本在运行时拼接：避免仓库里出现可被 GitHub secret scanning 命中的字面量
    fakes = (
        "AIza" + "SyA" + "1" * 30,                                            # Google API key 形态
        "xox" + "b-" + "1" * 12 + "-" + "a" * 12,                             # Slack token 形态
        "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "a" * 16,  # JWT 形态
        "-----BEGIN " + "RSA PRIVATE KEY" + "-----",                          # PEM 私钥形态
    )
    for secret in fakes:
        out = jmod._redact(f"prefix {secret} suffix")
        assert "REDACTED" in out and secret not in out, (secret, out)
    print("Redaction: structured keys + Google/Slack/JWT/PEM: PASSED")


if __name__ == "__main__":
    test_availability()
    test_no_engine_returns_explicit_error()
    test_guard_online_three_tiers()
    test_guard_online_timeout_stops_clean()
    test_guard_threshold_clamp()
    test_jev_multi_question_mock()
    test_jev_timeout_circuit_breaker()
    test_jev_error_fallback()
    test_disk_persistent_cache()
    test_journal_log()
    test_no_engine_error_not_cached()
    test_semantic_cache_key_isolation()
    test_guard_low_threshold_dynamic_deny()
    test_redaction_before_online_judgment()
    test_guard_multi_signal_verdict()
    test_jev_usage_serialization()
    test_redaction_structured_and_new_patterns()
    print("ALL TESTS PASSED")
