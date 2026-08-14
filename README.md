# honeybadger-models

HoneyBadger 模型元数据注册表（`honeybadger-models/v1`）。**models.json 由 sync 自动生成，不手编**；
人工修正只写 `overrides.json`。

- 上游：[models.dev](https://models.dev/api.json)（单一来源：limit / effort 档位 / 能力 / 价格，价格单位 per_million USD）
- 消费方：蜜獾 daemon（`tools/genmodelmeta` 编译期拉取）、gclm-router（运行时/编译期）
- 维护：GitHub Actions 每日自动同步；`config.json` 圈选 provider 与自家模型

## config.json

```json
{
  "source": "https://models.dev/api.json",
  "provider_filter": {
    "zai": {"channel": "zhipuai", "native": ["glm*"]},
    "minimax": {"channel": "minimax", "key_prefix": "minimax/", "action": "both", "native": ["MiniMax*"]}
  }
}
```

| 字段 | 语义 |
|---|---|
| key | models.dev 的 provider id（精确匹配） |
| `channel` | 来源渠道名（输出的 `channel` 字段；如 `zai → zhipuai`） |
| `native` | **自家模型 glob**（大小写不敏感）：官方渠道只取自家模型，cross-listing 三方模型滤掉 + stderr 报告被滤清单（新模型漏配可见） |
| `action` | `exact`（默认）：只拉该 key；`both`：拉该 key + 全部 `<key>-*` 变体端点（覆盖变体独有模型；与主渠道同名的模型以主渠道为准，静默跳过） |
| `key_prefix` | 该渠道在 gclm-router 上的模型名前缀 → 生成 alias（如 `minimax/MiniMax-M2`） |
| `include` / `exclude` | 模型级 glob 过滤（native 之外的补充手段） |

## models.json（v1）结构——扁平单值

```json
{
  "schema": "honeybadger-models/v1",
  "updated_at": "2026-08-15",
  "models": [
    {
      "id": "deepseek-v4-flash",
      "name": "DeepSeek V4 Flash",
      "kind": "chat",
      "channel": "deepseek",
      "limit": { "context": 1000000, "output": 384000 },
      "capabilities": {
        "vision": false,
        "tools": true,
        "reasoning": { "supported": true, "efforts": ["low", "high", "max"] }
      },
      "price": { "currency": "USD", "unit": "per_million_tokens", "input": 0.14, "output": 0.28, "cache_read": 0.0028 },
      "aliases": ["deepseek-v4-flash-0731"]
    }
  ]
}
```

- **扁平单值**：每模型一条 price/limit/capabilities（消费方「默认填充 + 用户手动编辑」直接成立）
- **kind**：`chat` / `image` / `audio` / `video`（从 `modalities.output` 推导，特定优先 image > audio > video > 纯 text）；非 chat 同为一等公民（网关计费/能力路由），`limit` 可省略；**校验分级：chat 缺 context 视为上游异常（警告跳过）**
- **reasoning 归位 capabilities**（能力维度；efforts 为该能力参数）
- **aliases**：同渠道日期快照（`-20250929` 等）与网关前缀名（`minimax/...`）折叠为别名
- **跨厂商同名**（native 过滤后理论归零）：取 config 顺序首个 + stderr 冲突报告

## 匹配规则（消费方统一实现）

1. 精确 `id` 命中
2. 精确 `aliases` 命中
3. **日期归一重试**：剥 `-YYYYMMDD` / `-YYMMDD` / `-MMDD` 尾巴后重走 1-2（隐式快照）
4. 未命中 → 消费方自有兜底

## overrides.json（人工修正层）

浅+一层递归合并，优先于同步产物；键 = 模型 id 或 `"id#channel"`。同步不冲掉。

```json
{
  "glm-5.2": { "capabilities": { "reasoning": { "supported": true, "efforts": ["low", "high"] } } }
}
```

## 本地运行

```bash
python3 sync/sync.py        # 拉上游 → native 过滤 → 归并 → overrides → 校验 → 写 models.json
```
