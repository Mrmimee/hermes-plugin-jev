# hermes-plugin-jev 🧠

[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-Plugin-blue.svg)](https://hermes-agent.nousresearch.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Powered by](https://img.shields.io/badge/Powered_by-TypeSafe_AI_%26_Agnes_AI-purple.svg)](https://typesafe.ai)

**Jev (TypeSafe AI) System One 决策小脑插件** —— 专为 **Hermes Agent** 打造的极速结构化决策与智能路由扩展。

---

## 🌟 为什么需要 Jev？

传统的 Agent 在面对多步骤工作流时，往往每一步都要调用庞大且昂贵的生成式大模型去判断“下一步该干嘛”或者“有没有安全风险”。这导致了严重的延迟累积和 Token 浪费。

**Jev 并非生成式聊天模型，而是“决策小脑”（System One Model）：**
* **零废话**：不进行流式长文本输出，输入当前状态，直接输出确定性的结构化判定。
* **极速响应**：基于 `system_one_adapter` 接入云端 Agnes 3.0 Flash，1 秒内完成判定。
* **零本地负载**：完全不占用本地 GPU 显存（VRAM: 0MB），纯轻量 HTTP 交互，无后台常驻进程。

---

## 🧩 核心裁决原语

| 原语 | 类别 | 典型应用场景 |
| :--- | :--- | :--- |
| **`Choice`** | 单选题 | 从多个候选执行器或工具中做路由跳转（如：Coder vs Searcher vs Reviewer） |
| **`Noul`** | 是非题 | 布尔事件判定或发生概率计算（如：是否属于高危破坏性操作、是否需要联网） |
| **`Score`** | 打分题 | 离散或连续多梯度评估（如：任务复杂度 0~2 分、相关度评分） |
| **`Guard`** 🛡️ | 动作闸门 | 执行动作前的 allow/ask/deny 二次判定（`jev_guard` 工具），在线走 Jev 概率裁决，无 key 或失败时自动降级本地离线规则（结果标 `advisory=true`，fail closed） |

### `jev_guard` 动作闸门

对标开源生态的 guard 类设计（allow/ask/deny 三档 + 置信度阈值 + 离线降级）：

```json
{
  "state": "执行 rm -rf /data/old_cache",
  "guard": {
    "instructions": "该动作是否可安全执行？",
    "threshold": 0.75
  }
}
```

- 有 `AGNES_API_KEY`：在线走 Jev 概率裁决，`probability >= threshold` 判 `allow`，否则 `ask`
- 无 key / adapter 缺失 / 在线调用失败：自动降级本地破坏性关键词规则（`rm -rf`、`format`、`drop database` 等 → `deny`；`delete`/`删除`/`overwrite` 等 → `ask`；其余 → `allow`），结果带 `advisory=true` 标记，调用方须把 advisory 放行当建议而非许可

---

## 🚀 安装与接入

### 方式一：直接克隆到 Hermes 插件目录

```bash
cd ~/.hermes/plugins
git clone https://github.com/Mrmimee/hermes-plugin-jev.git jev
```

### 方式二：启用插件

在 `~/.hermes/config.yaml` 中添加启用项：

```yaml
plugins:
  enabled:
    - jev
```

### 方式三：环境要求

确保系统已设置环境变量 `AGNES_API_KEY`（或在桌面的 `apikey.txt` 中配置）：
```bash
export AGNES_API_KEY="sk-..."
```

---

## 🛠️ 在 Hermes 中使用

新会话启动后，Hermes 会自动加载 `jev` 工具集。

### 自动化场景（Hermes 自主感知）
* 遇到复杂的多步骤或多工具任务分流时，Hermes 会在后台自主调用 Jev 完成并行裁决。
* 遇到系统关键变更或文件批量清理时，自动发起 Noul 风险判定。

### 显式对话示例
> “帮我用 Jev 评估一下当前需求应该调用哪个工具，以及是否存在破坏性操作。”

---

## 📂 项目结构

```text
hermes-plugin-jev/
├── plugin.yaml       # Hermes 插件声明配置
├── __init__.py       # 插件入口与 Jev/jev_guard 工具注册实现
├── test_verification.py  # 验证测试（可用性 + 离线规则 + 在线路径）
├── README.md         # 项目文档
├── .gitignore        # Git 忽略配置
└── sync.py           # 一键更新与 GitHub 同步工具
```

---

## 📄 License

MIT License.
