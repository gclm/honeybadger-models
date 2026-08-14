# honeybadger-models

HoneyBadger 模型元数据注册表（`hb-models/v2`）。**models.json 由 sync 自动生成，不手编**；
人工修正只写 `overrides.json`。

- 上游：[models.dev](https://models.dev/api.json)（单一来源：limit / effort 档位 / 能力 / 价格，价格单位 per_million USD）
- 消费方：蜜獾 daemon（`tools/genmodelmeta` 编译期拉取）、gclm-router（运行时/编译期）
- 维护：GitHub Actions 每日自动同步；`config.json` 圈选拉取哪些 provider

## config.json

```json
{
  "source": "https://models.dev/api.json",
  "provider_filter": {
    "zai": {"channel": "zhipuai"},
    "minimax": {"channel": "minimax", "key_prefix": "minimax/", "action": "both"}
  }
}
```

| 字段 | 语义 |
|---|---|
| key（如 `zai`） | models.dev 的 provider id（精确匹配） |
| `channel` | **渠道显示名**：输出到 `ports[].channel` 的名字（如 `zai → zhipuai`） |
| `action` | `exact`（默认）：只拉该 key；`both`：拉该 key + 全部 `<key>-*` 变体（`-cn` / `-coding-plan` / `-token-plan` 等，每个变体一个 port，via 用变体原名） |
| `key_prefix` | 该渠道在 gclm-router 上的模型名前缀 → 为该渠道每个模型生成带前缀的 alias（如 `minimax/MiniMax-M2`） |

## models.json（v2）结构

```json
{
  "schema": "hb-models/v2",
  "updated_at": "2026-08-15",
  "models": [
    {
      "id": "deepseek-v4-flash",
      "name": "DeepSeek V4 Flash",
      "kind": "chat",
      "limit": { "context": 1000000, "output": 384000 },
      "reasoning": { "supported": true, "efforts": ["low", "high", "max"] },
      "capabilities": { "chat": true, "vision": false, "tools": true },
      "ports": [
        { "channel": "deepseek", "price": { "currency": "USD", "unit": "per_million_tokens", "input": 0.14, "output": 0.28, "cache_read": 0.0028 } },
        { "channel": "alibaba-token-plan", "aliases": ["deepseek-v4-flash"], "price": { "...": "该渠道价" } }
      ]
    }
  ]
}
```

- **条目主键 = 模型 id**（跨渠道唯一）；同名模型出现在多个被圈选 provider → 合并为一条，每 provider 一个 **port**（渠道价差异所在层）
- **kind 类型**（从 `modalities.output` 推导，特定优先 image > audio > video > 纯 text）：`chat` 对话模型 / `image` 生图 / `audio` TTS·语音 / `video` 生视频。非 chat 模型同样一等公民（网关计费/能力路由都要用），`limit` 可选（生图/TTS 无上下文窗口概念）；**校验分级：chat 缺 context 视为上游数据异常（警告跳过），非 chat 不要求**
- **快照折叠**：同 provider 内 `xxx-20250929` / `xxx-0731` 型日期后缀，若去后缀存在同名模型 → 不产生结构，仅作为 alias 挂到该 port
- **reasoning.efforts**：取 models.dev `reasoning_options` 的 `effort` 型 values（`none`/`null`/`default` 剔除）；`toggle` 型 = supported 无档位；跨渠道不一致取并集
- **capabilities**：`vision` ← attachment、`tools` ← tool_call

## 匹配规则（消费方统一实现）

1. 精确 `id` 命中 → 取条目（价格默认第一个 port）
2. 精确 port `aliases` 命中 → 归并到所属条目 + 价格取该 port
3. **日期归一重试**：剥 `-YYYYMMDD` / `-YYMMDD` / `-MMDD` 尾巴后重走 1-2（隐式快照）
4. 未命中 → 消费方自有兜底（蜜獾：family 表 + 默认 context 128000）

## overrides.json（人工修正层）

浅合并优先于同步产物：键 = 模型 id 或 `"id#channel"`（改某个 port）；值 = 要覆盖的字段。
同步脚本每次重跑都会应用，人工修正不会被上游变更冲掉。

```json
{
  "glm-5.2": { "reasoning": { "supported": true, "efforts": ["low", "high"] } }
}
```

## 本地运行

```bash
python3 sync/sync.py        # 拉上游 → 归并 → 应用 overrides → 校验 → 写 models.json
```
