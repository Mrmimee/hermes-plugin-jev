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
    """有 key + adapter 时装载可用；无 key 时离线降级但不崩。"""
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
    assert "不能作为放行凭据" in guard_description
    assert "移除密钥、令牌、密码" in jmod.JEV_GUARD_SCHEMA["parameters"]["properties"]["state"]["description"]
    print("Availability and on-demand routing guidance: PASSED")


def test_guard_offline_rules():
    """离线降级规则单元：高危 deny、破坏性词 ask、普通文本 allow。"""
    r = jmod._local_guard_verdict({"state": "执行 rm -rf / 清空所有数据"})
    assert r["verdict"] == "deny" and r["advisory"] is True, r
    r2 = jmod._local_guard_verdict({"state": "删除旧缓存文件"})
    assert r2["verdict"] == "ask" and r2["advisory"] is True, r2
    r3 = jmod._local_guard_verdict({"state": "读取 config.yaml 配置文件"})
    assert r3["verdict"] == "allow" and r3["advisory"] is True, r3
    print("Guard offline rules: PASSED")


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
        assert r["advisory"] is False, r
        assert "deny_threshold" in r, r
    print("Guard online three-tier (allow/ask/deny): PASSED")


def test_guard_online_timeout_fallback():
    """在线 guard 超时 → 熔断降级本地 fail-closed 规则。"""
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
    assert r["advisory"] is True, r
    assert r["verdict"] == "deny", r  # 离线 fail-closed
    assert "超时" in r.get("reason", ""), r
    print("Guard online timeout fallback (fail-closed): PASSED")


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
    assert r["advisory"] is False, r
    print(f"Multi-question batching (mock): PASSED ({len(r['answers'])} answers)")


def test_jev_timeout_circuit_breaker():
    """jev 主工具超时熔断（mock）：超时必然触发 advisory 降级。"""
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
    assert r.get("advisory") is True, r
    assert "超时" in r.get("reason", ""), r
    print("Timeout circuit breaker: PASSED")


def test_jev_error_fallback():
    """jev 在线异常（mock）：非超时异常也降级 advisory。"""
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
    assert r.get("advisory") is True, r
    assert "boom" in r.get("reason", ""), r
    print("Online error fallback: PASSED")


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


def test_offline_result_cached():
    """验证：offline advisory 结果会被缓存（设计如此，1h 内不反复降级）。"""
    jmod._adapter_available = False
    jmod._get_agnes_key = lambda: ""
    args = {"state": "offline-cache-test", "noul": {"name": "oc", "instructions": "测试？"}, "timeout_seconds": 1.0}
    key = jmod._get_cache_key(args)
    jmod._DECISION_CACHE.pop(key, None)
    r = json.loads(jmod._handle_jev(args))
    assert r["advisory"] is True, r
    assert key in jmod._DECISION_CACHE, "offline advisory should be cached"
    jmod._DECISION_CACHE.pop(key, None)
    print("Offline advisory caching (by design): PASSED")


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

# ---------- TypeSafe 官方直连（2026-10-18 迭代）Mock 基础设施 ----------
import contextlib
import os as _os


@contextlib.contextmanager
def typesafe_env(key: "str | None" = "test-typesafe-key", model=None):
    """临时注入 TYPESAFE_API_KEY / JEV_MODEL，退出时精确还原（避免污染其它用例）。"""
    touched = ("TYPESAFE_API_KEY", "JEV_API_KEY", "JEV_MODEL")
    old = {k: _os.environ.get(k) for k in touched}
    if key is None:
        _os.environ.pop("TYPESAFE_API_KEY", None)
        _os.environ.pop("JEV_API_KEY", None)
    else:
        _os.environ["TYPESAFE_API_KEY"] = key
        _os.environ.pop("JEV_API_KEY", None)
    if model:
        _os.environ["JEV_MODEL"] = model
    else:
        _os.environ.pop("JEV_MODEL", None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


@contextlib.contextmanager
def patch_http(fn):
    """替换底层 HTTP POST（离线跑直连路径，不触网、不烧 token）。"""
    orig = jmod._http_post_json
    jmod._http_post_json = fn
    try:
        yield
    finally:
        jmod._http_post_json = orig


def typesafe_payload(answers):
    return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 321, "output_tokens": 12}}


def ts_answers(choice=None, nouls=None, scores=None):
    """按官方响应形状构造 answers"""
    out = {}
    if choice:
        out[choice[0]] = {"type": "choice", "choice": choice[1], "probabilities": {choice[1]: 0.9}, "confidence": 0.9}
    for k, v in (nouls or {}).items():
        out[k] = {"type": "noul", "noul": v}
    for k, v in (scores or {}).items():
        out[k] = {"type": "score", "score": v, "legend": {}, "confidence": 0.8}
    return out


def test_typesafe_direct_payload_and_parse():
    """官方直连：payload 结构正确（model/state/questions），响应解析为统一 answers 形状。"""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return typesafe_payload(ts_answers(
            choice=("route", "coder"),
            nouls={"q1": 1.0, "q2": 0.0, "q3": 0.5},
            scores={"complexity": 2.0},
        ))

    with typesafe_env(model="jev-1.14.0"), patch_http(fake_post):
        r = json.loads(jmod._handle_jev({
            "state": "多题直连测试",
            "nouls": [{"name": "q1", "instructions": "是否安全？"}, {"name": "q2", "instructions": "是否可逆？"}],
            "noul": {"name": "q3", "instructions": "是否需要确认？"},
            "choice": {"name": "route", "instructions": "优先调用谁？", "criteria": {"coder": "写代码", "searcher": "查资料"}},
            "score": {"name": "complexity", "instructions": "复杂度？", "criteria": ["低", "中", "高"]},
            "no_cache": True,
        }))

    assert captured["url"] == jmod._TYPESAFE_URL, captured["url"]
    assert captured["payload"]["model"] == "jev-1.14.0", captured["payload"]["model"]
    assert captured["payload"]["state"] == "多题直连测试"
    qs = captured["payload"]["questions"]
    assert qs["route"]["type"] == "choice" and qs["route"]["criteria"] == {"coder": "写代码", "searcher": "查资料"}, qs
    assert qs["q1"]["type"] == "noul" and "instructions" in qs["q1"], qs
    assert qs["complexity"]["type"] == "score" and qs["complexity"]["criteria"] == ["低", "中", "高"], qs
    assert captured["headers"]["Authorization"].startswith("Bearer "), captured["headers"]
    assert r["engine"] == "typesafe" and r["model"] == "jev-1.14.0", r
    assert r["answers"]["route"] == "coder" and r["answers"]["q1"] == 1.0 and r["answers"]["complexity"] == 2.0, r
    assert r["choice_answer"] == "coder" and r["noul_answer"] == 0.5, r
    assert r["usage"] == {"input_tokens": 321, "output_tokens": 12}, r
    print("TypeSafe direct: payload shape + answer parsing: PASSED")


def test_typesafe_redaction():
    """凭据脱敏：state 里的 key/token/password 不会原样发给第三方。"""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return typesafe_payload(ts_answers(nouls={"q": 1.0}))

    secret_state = "curl -H 'Authorization: Bearer sk-abcdef1234567890' && export GITHUB_TOKEN=github_pat_ABCDEFGHIJKLMNOPQRSTUV && password=hunter2secret"
    with typesafe_env(), patch_http(fake_post):
        jmod._handle_jev({"state": secret_state, "noul": {"name": "q", "instructions": "安全？"}, "no_cache": True})

    sent = captured["payload"]["state"]
    assert "sk-abcdef1234567890" not in sent, sent
    assert "github_pat_ABCDEFGHIJKLMNOPQRSTUV" not in sent, sent
    assert "hunter2secret" not in sent, sent
    assert "[REDACTED]" in sent, sent
    # 结构型 state：按键名脱敏（api_key / token / secret / password …）
    structured = jmod._redact({"cmd": "run", "api_key": "abcdef1234567890", "nested": {"authorization": "xyz"}})
    assert "abcdef1234567890" not in json.dumps(structured, ensure_ascii=False), json.dumps(structured, ensure_ascii=False)
    assert "xyz" not in json.dumps(structured, ensure_ascii=False), json.dumps(structured, ensure_ascii=False)
    assert "run" in json.dumps(structured, ensure_ascii=False), "非敏感字段必须保留"
    print("Secret redaction before online judgment: PASSED")


def test_typesafe_engine_priority():
    """引擎选择：有 TypeSafe key 时走直连，不再调用 adapter 的 client.system_one。"""
    calls = {"adapter": 0}

    class BoomClient:
        def system_one(self, **kwargs):
            calls["adapter"] += 1
            raise AssertionError("不应该走 adapter 分支")

    def fake_post(url, payload, headers, timeout):
        return typesafe_payload(ts_answers(nouls={"q": 1.0}))

    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake-agnes"
    with typesafe_env(), patch_http(fake_post):
        assert jmod._active_engine().startswith("typesafe:"), jmod._active_engine()
        fb = jmod._executor.submit(jmod._execute_system_one_with_timeout, BoomClient(), "s", {"q": jmod.Noul(instructions="x")}, None, 5.0)
        resp = fb.result(timeout=10)
    assert calls["adapter"] == 0, calls
    assert resp.nouls["q"].noul == 1.0, resp
    print("Engine priority (typesafe > agnes): PASSED")


def test_typesafe_http_error_fallback():
    """直连 401 / 网络错误：降级 advisory，不抛异常、不阻塞主流程。"""
    def fake_post(url, payload, headers, timeout):
        raise RuntimeError("TypeSafe HTTP 401: {\"detail\":\"invalid token\"}")

    with typesafe_env(), patch_http(fake_post):
        r = json.loads(jmod._handle_jev({
            "state": "401 降级测试", "noul": {"name": "q", "instructions": "是否？"}, "no_cache": True,
        }))
    assert r["advisory"] is True, r
    assert "401" in r["reason"], r
    print("TypeSafe HTTP error -> advisory fallback: PASSED")


def test_typesafe_429_retry_once():
    """429 限流：按官方建议退避重试一次即成功。"""
    calls = {"n": 0}

    def fake_post(url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("TypeSafe HTTP 429: rate limited")
        return typesafe_payload(ts_answers(choice=("route", "searcher")))

    with typesafe_env(), patch_http(fake_post):
        r = json.loads(jmod._handle_jev({
            "state": "429 重试测试",
            "choice": {"name": "route", "instructions": "选一个", "criteria": {"searcher": "查资料", "coder": "写代码"}},
            "no_cache": True,
            "timeout_seconds": 20.0,
        }))
    assert calls["n"] == 2, calls
    assert r["answers"]["route"] == "searcher", r
    print("TypeSafe 429 backoff retry (1x): PASSED")


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


def test_engine_isolated_cache_key():
    """缓存键含引擎标识：换引擎/换模型后不复用旧引擎结论。"""
    args = {"state": "引擎隔离", "noul": {"name": "q", "instructions": "测试"}}
    with typesafe_env(key=None):
        if _os.environ.get("AGNES_API_KEY"):
            k_online = jmod._get_cache_key(args)
        else:
            k_online = jmod._get_cache_key(args)
    with typesafe_env(model="jev-latest"):
        k_ts = jmod._get_cache_key(args)
    with typesafe_env(model="jev-1.14.0"):
        k_ts2 = jmod._get_cache_key(args)
    assert k_ts != k_ts2, "不同 Jev 模型必须产生不同缓存键"
    assert k_ts != k_online, "typesafe 引擎不得与其它引擎共用缓存键"
    print("Cache key isolation per engine/model: PASSED")


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
    assert r.get("advisory") is False, r
    assert r["answers"]["route"] == "searcher" and r["answers"]["risky"] == 0.2
    assert r["usage"] == {"input_tokens": 268, "output_tokens": 41}, r.get("usage")
    print("Jev usage serialization (SDK object -> dict): PASSED")


if __name__ == "__main__":
    test_availability()
    test_guard_offline_rules()
    test_guard_online_three_tiers()
    test_guard_online_timeout_fallback()
    test_guard_threshold_clamp()
    test_jev_multi_question_mock()
    test_jev_timeout_circuit_breaker()
    test_jev_error_fallback()
    test_disk_persistent_cache()
    test_journal_log()
    test_offline_result_cached()
    test_semantic_cache_key_isolation()
    test_guard_low_threshold_dynamic_deny()
    test_typesafe_direct_payload_and_parse()
    test_typesafe_redaction()
    test_typesafe_engine_priority()
    test_typesafe_http_error_fallback()
    test_typesafe_429_retry_once()
    test_guard_multi_signal_verdict()
    test_engine_isolated_cache_key()
    test_jev_usage_serialization()
    print("ALL TESTS PASSED")
