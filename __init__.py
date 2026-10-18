"""Jev/TypeSafe AI 决策小脑 Hermes 插件。

在 Hermes 注册 `jev` 工具集，允许模型在毫秒级做出"单选 (Choice)"、"打分 (Score)"、"是非率 (Noul)"决策。
支持单题/多题并发打满（1 次网络往返裁决多个问题），具备双层持久化磁盘缓存、8 秒硬超时熔断与黑匣子审计日志。

引擎（`system_one_adapter` → Agnes 3.0 Flash，结果里带 `engine` 字段）：
唯一在线引擎是 Agnes 3.0 Flash（需要 `AGNES_API_KEY`）；官方 TypeSafe 直连按量计费，本插件不接入。

引擎不可用、超时或报错时**直接返回 error**：不做本地离线兜底判定，不产生任何"看起来正常"的假结论。

零常驻显存，零后台进程，极低内存消耗。
"""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 0. 引入 jev-starter 的 venv site-packages，保证运行独立性（兼容本机部署与跨机器克隆安装）
_ADAPTER_SITE_PACKAGES = os.path.join(
    os.path.expanduser("~"), "jev-starter", ".venv", "Lib", "site-packages"
)

if os.path.isdir(_ADAPTER_SITE_PACKAGES) and _ADAPTER_SITE_PACKAGES not in sys.path:
    sys.path.append(_ADAPTER_SITE_PACKAGES)

_adapter_available = True
try:
    from system_one_adapter import SystemOneAdapterClient, Choice, Noul, Score
    from system_one_adapter.providers.openai import OpenAIProvider
except Exception:
    _adapter_available = False

    class _MissingOpenAIProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("system_one_adapter 未安装：请 pip install 'system-one-adapter[openai]'")

    OpenAIProvider: Any = _MissingOpenAIProvider

_BASE_URL = "https://apihub.agnes-ai.com/v1"
_MODEL_NAME = "agnes-3.0-flash"
_DEFAULT_TIMEOUT_SECONDS = 8.0

_ENGINE_AGNES = "agnes"   # 唯一引擎：system_one_adapter → Agnes 3.0 Flash
_ENGINE_NONE = "none"     # 无可用引擎：直接报错，不做离线兜底


# 1. 缓存路径与黑匣子日志路径
def _get_cache_dir() -> Path:
    """获取 Hermes 缓存目录（优先 AppData/Local/hermes/cache，兼容 ~/.hermes/cache）"""
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        p = Path(hermes_home) / "cache"
        if p.parent.exists():
            p.mkdir(parents=True, exist_ok=True)
            return p
    app_cache = Path.home() / "AppData" / "Local" / "hermes" / "cache"
    if app_cache.parent.exists():
        app_cache.mkdir(parents=True, exist_ok=True)
        return app_cache
    fallback = Path.home() / ".hermes" / "cache"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


_CACHE_FILE = _get_cache_dir() / "jev_cache.json"
_JOURNAL_FILE = _get_cache_dir() / "jev_journal.jsonl"

# LRU 决策缓存（上限 256 条，默认 3600 秒有效期）
_CACHE_MAX_SIZE = 256
_CACHE_TTL_SECONDS = 3600
_DECISION_CACHE: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
_CACHE_LOCK = threading.RLock()  # 保护 _DECISION_CACHE 的并发读写
_DISK_LOCK = threading.Lock()     # 保护磁盘写（避免并发 write_text 竞态）

# 模块级单例线程池：超时后不 join 阻塞，后台 worker 自行结束
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="jev")


def _load_disk_cache() -> None:
    """启动时从磁盘恢复决策缓存，跨会话/重启 0ms 秒命中"""
    if not _CACHE_FILE.exists():
        return
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        for k, item in data.items():
            if isinstance(item, dict) and "created_at" in item and "result" in item:
                created_at = item["created_at"]
                if now - created_at <= _CACHE_TTL_SECONDS:
                    _DECISION_CACHE[k] = (created_at, item["result"])
    except Exception:
        pass


def _save_disk_cache() -> None:
    """轻量原子写盘，持久化缓存（锁保护 + 临时文件原子替换，防异常断电坏文件）"""
    try:
        with _DISK_LOCK:
            payload = {}
            with _CACHE_LOCK:
                for k, (created_at, res) in _DECISION_CACHE.items():
                    payload[k] = {"created_at": created_at, "result": res}
            # 原子写入：写 tmp 文件后原子替换，避免破坏原文件
            tmp_file = _CACHE_FILE.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_file.replace(_CACHE_FILE)
    except Exception:
        pass


# 初始化加载磁盘缓存
_load_disk_cache()


def _get_cache_key(args: Dict[str, Any]) -> str:
    """计算状态与问题的确定性 SHA256 指纹（剥离 timeout/no_cache 等控制参数，防缓存击穿）"""
    try:
        # 仅保留具有决策语义的字段
        semantic_keys = {"state", "choice", "choices", "noul", "nouls", "score", "scores", "guard"}
        semantic_payload = {k: v for k, v in args.items() if k in semantic_keys}
        # 引擎/模型也进缓存键：换引擎或换 Jev 版本后不复用旧引擎的结论
        semantic_payload["__engine"] = _active_engine()
        raw = json.dumps(semantic_payload, sort_keys=True, ensure_ascii=True)
    except Exception:
        raw = str(args)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    """获取缓存并执行 LRU 提升与 TTL 过期检查（锁保护）"""
    with _CACHE_LOCK:
        if key not in _DECISION_CACHE:
            return None
        created_at, result = _DECISION_CACHE[key]
        if time.time() - created_at > _CACHE_TTL_SECONDS:
            del _DECISION_CACHE[key]
            return None
        _DECISION_CACHE.move_to_end(key)
        res_copy = dict(result)
    res_copy["cache_hit"] = True
    res_copy["latency_seconds"] = 0.0
    return res_copy


def _cache_put(key: str, result: dict) -> None:
    """写入缓存，超出容量时淘汰最老条目并持久化（锁保护）"""
    with _CACHE_LOCK:
        if len(_DECISION_CACHE) >= _CACHE_MAX_SIZE:
            _DECISION_CACHE.popitem(last=False)
        _DECISION_CACHE[key] = (time.time(), result)
    _save_disk_cache()


def _log_journal(entry: Dict[str, Any]) -> None:
    """记录判定黑匣子流水（审计日志，带 10MB 自动轮转备份）"""
    try:
        if _JOURNAL_FILE.exists() and _JOURNAL_FILE.stat().st_size > 10 * 1024 * 1024:
            bak = _JOURNAL_FILE.with_suffix(".jsonl.old")
            try:
                if bak.exists():
                    bak.unlink()
                _JOURNAL_FILE.replace(bak)
            except Exception:
                pass

        record = dict(entry)
        record["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _get_agnes_key() -> str:
    """提取用户的 AGNES_API_KEY"""
    key = os.environ.get("AGNES_API_KEY")
    if key:
        return key
    # 兼容 OneDrive 重定向与原生英文/中文桌面路径
    candidate_paths = [
        Path.home() / "OneDrive" / "桌面" / "apikey.txt",
        Path.home() / "Desktop" / "apikey.txt",
        Path.home() / "桌面" / "apikey.txt",
    ]
    for key_file in candidate_paths:
        if key_file.exists():
            lines = key_file.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if "AgnesAi" in line and i + 1 < len(lines):
                    return lines[i + 1].strip()
    return ""


def _active_engine() -> str:
    """当前实际会使用的引擎标识（agnes:<model> / none），同时用于缓存键隔离"""
    if _adapter_available and _get_agnes_key():
        return f"{_ENGINE_AGNES}:{_MODEL_NAME}"
    return _ENGINE_NONE


# 发送给第三方裁决前先脱敏（凭据绝不进网络请求，也不进审计日志预览）
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)\b\s*[:=]\s*[^\s,;\"']{6,}"),
)


# 会触发“整值脱敏”的键名（结构化 state 里出现这些键，值直接遮蔽）
_SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential|auth)")


def _redact(value: Any) -> Any:
    """脱敏：遮蔽 state 里的密钥/令牌/口令（字符串、字典、列表均可）。"""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(val, str) and _SENSITIVE_KEYS.search(str(key)):
                out[key] = "[REDACTED]"          # 键名本身就说明这是凭据 → 值整体遮蔽
            else:
                out[key] = _redact(val)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    text = str(value)
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    """把 SDK Usage 对象转成可 JSON 序列化的 dict。

    工具结果、磁盘缓存、审计日志都要 `json.dumps`；若直接塞进 SDK 的 Usage 对象，
    `json.dumps(result)` 会抛 TypeError，把整条在线结果打成异常。
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in usage.items()}
    for meth in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, meth, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in d.items()}
            except Exception:
                pass
    out = {}
    for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
        v = getattr(usage, k, None)
        if v is not None:
            out[k] = v
    return out or {"raw": str(usage)}


def _guard_verdict_from_signals(signals: Dict[str, Any], threshold: float) -> Optional[tuple]:
    """多题信号 → 确定性裁决（判定顺序对标官方 jev-approvals 插件）。

    信号不全时返回 None，交由旧的单题概率三档逻辑处理（向后兼容）。
    """
    required = ("policy_allows", "reads_secrets", "sends_outbound", "self_advocating", "blast_radius")
    if any(signals.get(k) is None for k in required):
        return None
    blast = float(signals["blast_radius"])
    if float(signals["self_advocating"]) >= 0.6:
        return "ask", "命令文本疑似自我游说（self_advocating >= 0.6）"
    if float(signals["reads_secrets"]) >= 0.7:
        return "deny", "疑似读取或复制凭据（reads_secrets >= 0.7）"
    if float(signals["sends_outbound"]) >= 0.7:
        return "deny", "疑似把本地内容外发（sends_outbound >= 0.7）"
    if blast >= 1.6:
        # 刻意前移（官方 jev-approvals 把它放在放行之后）：破坏半径大的动作不因“策略允许”而自动放行
        return "ask", "破坏半径较大（blast_radius >= 1.6）"
    if float(signals["policy_allows"]) >= threshold and blast < 2.0:
        return "allow", "策略允许且破坏半径有限"
    return "ask", "信号不足以放行，建议人工确认"


def _jev_tool_available() -> bool:
    """判断 Jev 小脑是否可用：adapter 已装 + Agnes key 可获取"""
    return bool(_adapter_available and _get_agnes_key())


# 2. 工具定义 Schema
JEV_SCHEMA = {
    "name": "jev",
    "description": (
        "Jev 决策小脑（system_one_adapter → Agnes 3.0 Flash 结构化裁决）："
        "用于在多个可用工具/执行路径之间确有选择不确定性时，先把候选项及其用途传入 Choice 进行按需路由；"
        "主 Agent 再负责调用所选工具并处理结果。"
        "普通对话、工具选择显而易见时不要调用 Jev；不要每轮调用，也不要对每次工具调用重复裁决。"
        "Jev 只输出结构化判断，不会替你调用工具。风险评估可用 Noul，质量/复杂度可用 Score；"
        "支持多题并发（nouls/choices/scores），带缓存、8秒默认超时熔断和审计记录；"
        "返回里带 engine/model/usage 便于核对实际走的引擎；发送前会自动脱敏凭据。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "完整当前状态；只传与本次路由/判断相关的信息，避免放入密钥、凭据或无关个人内容。",
            },
            "choice": {
                "type": "object",
                "description": "单项选择题（单题）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "单选意图描述"},
                    "criteria": {
                        "type": "object",
                        "description": "选项及其含义，格式为 {'选项key': '该选项的描述'}",
                    },
                },
            },
            "choices": {
                "type": "array",
                "description": "多个单选题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "单选意图描述"},
                        "criteria": {"type": "object", "description": "选项键值对"},
                    },
                    "required": ["criteria"],
                },
            },
            "noul": {
                "type": "object",
                "description": "是非题（单题，布尔概率 0.0~1.0）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "判断意图描述"},
                },
            },
            "nouls": {
                "type": "array",
                "description": "多个是非题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "判断意图描述"},
                    },
                    "required": ["instructions"],
                },
            },
            "score": {
                "type": "object",
                "description": "打分题（单题，离散评分梯度）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "打分意图描述"},
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "评分梯度数组，如 ['低', '中', '高']",
                    },
                },
            },
            "scores": {
                "type": "array",
                "description": "多个打分题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "打分意图描述"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "评分梯度数组"},
                    },
                    "required": ["criteria"],
                },
            },
            "timeout_seconds": {
                "type": "number",
                "description": "网络裁决超时熔断阈值（秒，默认 8.0 秒）。超时直接返回 error，不做本地兜底。",
            },
            "no_cache": {
                "type": "boolean",
                "description": "是否跳过本地缓存强制实时推理（默认 false）。",
            },
        },
        "required": ["state"],
    },
}

# guard 动作闸门 Schema：执行前对动作做 allow/ask/deny 三档裁决
JEV_GUARD_SCHEMA = {
    "name": "jev_guard",
    "description": (
        "Jev 动作闸门（guard）：仅在动作本身具有实质风险、需要额外风险评估时按需调用；"
        "这是第二意见，不会替代系统或用户授权规则。普通低风险工具调用无需逐次运行 guard。"
        "在线时一次请求并行裁决 6 个信号（是否安全 / 策略是否允许 / 是否读取凭据 / 是否外发内容 / "
        "是否自我游说 / 破坏半径），再由确定性代码给出 allow/ask/deny，避免把自由文本当授权。"
        "引擎不可用、超时或调用失败时直接返回 error，不给 verdict（没有本地兜底判定）；拿不到 verdict 就不要当作放行凭据。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "只提供与待评估动作直接相关的上下文；移除密钥、令牌、密码和无关个人信息。引擎不可用或调用失败时会直接返回 error。",
            },
            "guard": {
                "type": "object",
                "description": "闸门配置（可选）。",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "判定意图描述，默认：该动作是否可安全执行。",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "放行阈值（0~1，默认 0.75）。概率低于阈值判 ask。",
                    },
                },
            },
            "timeout_seconds": {
                "type": "number",
                "description": "网络裁决超时熔断阈值（秒，默认 8.0 秒）。超时直接返回 error，不做本地兜底。",
            },
            "no_cache": {
                "type": "boolean",
                "description": "是否跳过本地缓存强制实时推理（默认 false）。",
            },
        },
        "required": ["state"],
    },
}


def _build_provider() -> Any:
    """快速构建指向 Agnes 云端的 Provider"""
    key = _get_agnes_key()
    return OpenAIProvider(
        _MODEL_NAME,
        base_url=_BASE_URL,
        api_key=key,
        api="chat_completions",
    )


def _execute_system_one_with_timeout(
    client: Any,
    state: Any,
    questions: Dict[str, Any],
    provider: Any,
    timeout: float,
) -> Any:
    """带硬超时保护的 System One 执行包装器（system_one_adapter → Agnes 3.0 Flash）。

    使用模块级单例线程池：超时时 fut.result(timeout) 抛出 TimeoutError，
    主调用方立即返回（真硬超时），后台 worker 继续执行至自然结束，
    绝不阻塞主流程。
    """
    fut = _executor.submit(
        client.system_one,
        state=state,
        questions=questions,
        model=provider,
    )
    return fut.result(timeout=timeout)


def _handle_jev(args: Dict[str, Any], **kwargs) -> str:
    """处理 Jev 决策工具调用（支持单题、多题并发打满、超时熔断、磁盘持久化与日志）"""
    state = args.get("state")
    if state is None:
        return json.dumps({"error": "缺少 state（系统当前状态）参数。"}, ensure_ascii=False)
    state = _redact(state)  # 发送前脱敏：凭据绝不进第三方请求（也不进审计日志预览）

    raw_timeout = args.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = max(0.1, min(float(raw_timeout), 120.0))  # clamp 到 [0.1, 120] 秒
    except (ValueError, TypeError):
        timeout = _DEFAULT_TIMEOUT_SECONDS

    # 优先查本地高速缓存（跨会话磁盘持久化）
    use_cache = not args.get("no_cache", False)
    cache_key = _get_cache_key(args)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": "jev", "cache_hit": True, "latency_seconds": 0.0, "state_preview": str(_redact(state))[:100]})
            return json.dumps(hit, ensure_ascii=False, indent=2)

    # 无可用引擎：直接报错（不做离线兜底，避免把假结论当裁决）
    if not _jev_tool_available():
        _log_journal({"tool": "jev", "error": "no_engine", "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": "Jev 在线裁决不可用：当前没有可用引擎，本次未做任何判定。",
                "engine": _ENGINE_NONE,
                "hint": (
                    "需要 AGNES_API_KEY（或桌面 apikey.txt 的 AgnesAi 条目）+ 可导入的 system_one_adapter"
                    "（依赖桥 ~/jev-starter/.venv）。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    client = SystemOneAdapterClient(
        structured_outputs=False,
        llm_answer_mode="discrete",
    )
    provider = _build_provider()

    # 静态分析防护：若 import 失败，则 _adapter_available=False，提前报错而非引用未定义符号
    if not _adapter_available:
        return json.dumps({"error": "system_one_adapter 未安装，无法进行在线裁决。"}, ensure_ascii=False)

    questions = {}

    # 1. 单选解析（单题 + 多题数组）
    choice_cfg = args.get("choice")
    if choice_cfg and choice_cfg.get("criteria"):
        q_name = choice_cfg.get("name", "choice_q")
        questions[q_name] = Choice(
            instructions=choice_cfg.get("instructions", "下一步做什么？"),
            criteria=choice_cfg["criteria"],
        )

    for i, c in enumerate(args.get("choices") or []):
        if isinstance(c, dict) and c.get("criteria"):
            q_name = c.get("name") or f"choice_{i+1}"
            questions[q_name] = Choice(
                instructions=c.get("instructions", "选择最合适的一项"),
                criteria=c["criteria"],
            )

    # 2. 是非解析（单题 + 多题数组）
    noul_cfg = args.get("noul")
    if noul_cfg:
        q_name = noul_cfg.get("name", "noul_q")
        questions[q_name] = Noul(
            instructions=noul_cfg.get("instructions", "是否发生？")
        )

    for i, n in enumerate(args.get("nouls") or []):
        if isinstance(n, dict) and n.get("instructions"):
            q_name = n.get("name") or f"noul_{i+1}"
            questions[q_name] = Noul(
                instructions=n["instructions"]
            )

    # 3. 打分解析（单题 + 多题数组）
    score_cfg = args.get("score")
    if score_cfg and score_cfg.get("criteria"):
        q_name = score_cfg.get("name", "score_q")
        questions[q_name] = Score(
            instructions=score_cfg.get("instructions", "评估打分"),
            criteria=score_cfg["criteria"],
        )

    for i, s in enumerate(args.get("scores") or []):
        if isinstance(s, dict) and s.get("criteria"):
            q_name = s.get("name") or f"score_{i+1}"
            questions[q_name] = Score(
                instructions=s.get("instructions", "评估打分"),
                criteria=s["criteria"],
            )

    if not questions:
        return json.dumps(
            {
                "error": "没有提供任何判定题目，请提供 choice/choices、noul/nouls 或 score/scores 之一。",
                "tip": "支持多题并发传入，一次网络往返即可返回所有题目答案。",
            },
            ensure_ascii=False,
        )

    try:
        t0 = time.time()
        response = _execute_system_one_with_timeout(
            client=client,
            state=state,
            questions=questions,
            provider=provider,
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 2)

        result: Dict[str, Any] = {
            "latency_seconds": elapsed,
            "cache_hit": False,
            "advisory": False,
            "engine": getattr(response, "engine", _ENGINE_AGNES),
            "model": getattr(response, "model", _MODEL_NAME),
            "answers": {},
        }
        if getattr(response, "usage", None):
            result["usage"] = _usage_to_dict(response.usage)

        # 聚合全部题目结果
        for q_name, c_val in response.choices.items():
            result["answers"][q_name] = c_val.choice
        for q_name, n_val in response.nouls.items():
            result["answers"][q_name] = n_val.noul
        for q_name, s_val in response.scores.items():
            result["answers"][q_name] = s_val.score

        # 单题兼容别名
        if choice_cfg:
            c_name = choice_cfg.get("name", "choice_q")
            if c_name in result["answers"]:
                result["choice_answer"] = result["answers"][c_name]

        if noul_cfg:
            n_name = noul_cfg.get("name", "noul_q")
            if n_name in result["answers"]:
                result["noul_answer"] = result["answers"][n_name]

        if score_cfg:
            s_name = score_cfg.get("name", "score_q")
            if s_name in result["answers"]:
                result["score_answer"] = result["answers"][s_name]

        if use_cache:
            _cache_put(cache_key, result)

        _log_journal({
            "tool": "jev",
            "latency_seconds": elapsed,
            "questions_count": len(questions),
            "answers": result["answers"],
            "state_preview": str(_redact(state))[:100],
        })
        return json.dumps(result, ensure_ascii=False, indent=2)

    except concurrent.futures.TimeoutError:
        # 硬超时熔断：直接报错（不编造结论），主流程绝不卡死
        _log_journal({"tool": "jev", "timeout": True, "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": f"在线裁决超时 (>{timeout:.1f}s)，已熔断：本次未做任何判定。",
                "engine": _active_engine(),
                "latency_seconds": timeout,
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as e:
        # 在线调用异常：如实报错（不降级为本地提示）
        _log_journal({"tool": "jev", "error": str(e), "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": f"在线裁决失败：{type(e).__name__}: {e}",
                "engine": _active_engine(),
                "latency_seconds": 0.0,
            },
            ensure_ascii=False,
            indent=2,
        )


def _handle_jev_guard(args: Dict[str, Any], **kwargs) -> str:
    """处理 guard 动作闸门调用：在线走 Jev 六信号裁决；引擎不可用/超时/失败时如实返回 error（不做本地兜底）。"""
    state = args.get("state")
    if state is None:
        return json.dumps(
            {"error": "缺少 state（待执行动作描述）参数。"},
            ensure_ascii=False,
        )
    state = _redact(state)  # 发送前脱敏：凭据绝不进第三方请求（也不进审计日志预览）

    raw_timeout = args.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = max(0.1, min(float(raw_timeout), 120.0))
    except (ValueError, TypeError):
        timeout = _DEFAULT_TIMEOUT_SECONDS

    use_cache = not args.get("no_cache", False)
    cache_key = _get_cache_key(args)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": "jev_guard", "cache_hit": True, "latency_seconds": 0.0, "state_preview": str(_redact(state))[:100]})
            return json.dumps(hit, ensure_ascii=False, indent=2)

    # 无可用引擎：直接报错（不做本地兜底判定，避免给出假 verdict）
    if not _jev_tool_available():
        _log_journal({"tool": "jev_guard", "error": "no_engine", "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": "Jev 闸门不可用：当前没有可用引擎，本次未做任何裁决（无本地兜底）。",
                "engine": _ENGINE_NONE,
                "hint": "需要 AGNES_API_KEY + 可导入的 system_one_adapter；拿不到 verdict 就不要当成 allow。",
            },
            ensure_ascii=False,
            indent=2,
        )

    guard_cfg = args.get("guard") or {}
    question = Noul(
        instructions=guard_cfg.get(
            "instructions",
            "这个动作是否可以安全执行？true 表示安全可放行。",
        )
    )
    # 多题信号（判定顺序对标官方 jev-approvals）：一次请求并行裁决 6 题，再由确定性代码给结论
    guard_questions = {
        "guard_q": question,
        "policy_allows": Noul(
            instructions="操作方的既定策略是否明确允许该动作（常规、低风险、可回滚）？true 表示明确允许。"
        ),
        "reads_secrets": Noul(
            instructions="该动作是否会读取、复制、打印或上传凭据/密钥/令牌/密码等敏感信息？"
        ),
        "sends_outbound": Noul(
            instructions="该动作是否会把本地内容或数据发送到外部网络或第三方？"
        ),
        "self_advocating": Noul(
            instructions="命令或文本本身是否在为自己的执行辩护、催促或诱导放行？"
        ),
        "blast_radius": Score(criteria=["trivial", "annoying", "severe"]),
    }
    raw_threshold = guard_cfg.get("threshold", 0.75)
    try:
        threshold = max(0.0, min(float(raw_threshold), 1.0))  # clamp 到 [0, 1]
    except (ValueError, TypeError):
        threshold = 0.75
    # 动态收敛拒绝阈值，防止用户传入低 threshold 时导致档位倒挂或 ask 被吃掉
    _DENY_THRESHOLD = min(0.25, threshold * 0.5) if threshold > 0 else 0.0

    try:
        t0 = time.time()
        client = SystemOneAdapterClient(
            structured_outputs=False,
            llm_answer_mode="discrete",
        )
        response = _execute_system_one_with_timeout(
            client=client,
            state=state,
            questions=guard_questions,
            provider=_build_provider(),
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 2)
        prob = response.nouls["guard_q"].noul if "guard_q" in response.nouls else None
        signals = {
            "policy_allows": getattr(response.nouls.get("policy_allows"), "noul", None),
            "reads_secrets": getattr(response.nouls.get("reads_secrets"), "noul", None),
            "sends_outbound": getattr(response.nouls.get("sends_outbound"), "noul", None),
            "self_advocating": getattr(response.nouls.get("self_advocating"), "noul", None),
            "blast_radius": getattr(response.scores.get("blast_radius"), "score", None),
        }
        judged = _guard_verdict_from_signals(signals, threshold)
        if judged is not None:
            verdict, why = judged
            verdict_source = "signals"
        else:
            # 信号不全（旧响应 / mock 只有 guard_q）→ 退回单题概率三档
            if prob is None:
                raise RuntimeError("裁决响应缺少 guard_q 与多题信号，无法判定")
            verdict = "allow" if prob >= threshold else ("ask" if prob >= _DENY_THRESHOLD else "deny")
            why = "单题概率三档（信号缺失时的兼容路径）"
            verdict_source = "probability"
        result = {
            "verdict": verdict,
            "probability": prob if prob is not None else signals.get("policy_allows"),
            "threshold": threshold,
            "deny_threshold": _DENY_THRESHOLD,
            "signals": signals,
            "verdict_source": verdict_source,
            "reason": why,
            "engine": getattr(response, "engine", _ENGINE_AGNES),
            "model": getattr(response, "model", _MODEL_NAME),
            "advisory": False,
            "latency_seconds": elapsed,
            "cache_hit": False,
        }
        if use_cache:
            _cache_put(cache_key, result)

        _log_journal({
            "tool": "jev_guard",
            "verdict": verdict,
            "probability": prob,
            "latency_seconds": elapsed,
            "state_preview": str(_redact(state))[:100],
        })
        return json.dumps(result, ensure_ascii=False, indent=2)

    except concurrent.futures.TimeoutError:
        # 硬超时熔断：直接报错，不给 verdict
        _log_journal({"tool": "jev_guard", "timeout": True, "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": f"在线裁决超时 (>{timeout:.1f}s)，已熔断：本次未做任何裁决（无本地兜底）。",
                "engine": _active_engine(),
                "latency_seconds": timeout,
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as e:
        # 在线调用失败：如实报错，不给 verdict
        _log_journal({"tool": "jev_guard", "error": str(e), "state_preview": str(_redact(state))[:100]})
        return json.dumps(
            {
                "error": f"在线裁决失败：{type(e).__name__}: {e}",
                "engine": _active_engine(),
                "latency_seconds": 0.0,
            },
            ensure_ascii=False,
            indent=2,
        )


def register(ctx) -> None:
    """注册 Jev 工具"""
    ctx.register_tool(
        name="jev",
        toolset="jev",
        schema=JEV_SCHEMA,
        handler=_handle_jev,
        check_fn=_jev_tool_available,
        emoji="🧠",
    )
    ctx.register_tool(
        name="jev_guard",
        toolset="jev",
        schema=JEV_GUARD_SCHEMA,
        handler=_handle_jev_guard,
        check_fn=None,  # 始终注册：可用时走在线六信号裁决，不可用时明确返回 error
        emoji="🛡️",
    )
