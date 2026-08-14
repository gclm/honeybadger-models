#!/usr/bin/env python3
"""honeybadger-models 同步：models.dev → models.json（honeybadger-models/v1）。

规则见 README：
- provider_filter 圈选（exact/both）+ native 自家模型过滤（官方渠道只取自家模型）
- 快照折叠（同渠道日期后缀 → alias）
- 扁平单值（无 ports：每模型一条 price/limit/capabilities；同名多渠道取 config
  顺序首个 + 冲突报告兜底——native 过滤后冲突理论归零）
- reasoning 归位 capabilities；kind 从 modalities.output 推导
- overrides.json 人工修正层（浅+一层递归合并，不被同步冲掉）
幂等可重跑（排序输出，Actions 只在 diff 时 commit）。
"""
import fnmatch
import json
import os
import re
import sys
import urllib.request
from datetime import date

DATE_SUFFIX = re.compile(r"-(\d{8}|\d{6}|\d{4})$")
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]
SCHEMA = "honeybadger-models/v1"


def load_json(path_or_url):
    if path_or_url.startswith("http"):
        req = urllib.request.Request(path_or_url, headers={"User-Agent": "honeybadger-models-sync/1.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    with open(path_or_url) as f:
        return json.load(f)


def norm_price(cost):
    if not cost:
        return None
    p = {"currency": "USD", "unit": "per_million_tokens"}
    for k in ("input", "output", "cache_read", "cache_write", "reasoning"):
        if cost.get(k) is not None:
            p[k] = cost[k]
    return p


def sort_efforts(efforts):
    known = [e for e in EFFORT_ORDER if e in efforts]
    extra = sorted(e for e in efforts if e not in EFFORT_ORDER)
    return known + extra


def model_efforts(m):
    for opt in m.get("reasoning_options") or []:
        if opt.get("type") == "effort":
            vals = opt.get("values") or []
            return [v for v in vals if v not in ("", "none", "null", "default")]
    return []


def derive_kind(m):
    """输出模态推导类型：特定优先（image > audio > video > 纯 text=chat）。

    chatgpt-image-latest 输出 [text,image] → image（图像生成模型，非对话）。
    无 modalities 的条目默认 chat（上游数据缺失时的保守归类）。
    """
    out = ((m.get("modalities") or {}).get("output")) or []
    for mod, kind in (("image", "image"), ("audio", "audio"), ("video", "video")):
        if mod in out:
            return kind
    return "chat"


def matches_any(name, patterns):
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def merge_dict(base, patch):
    """浅+一层递归合并（overrides 用；嵌套 dict 递归，其余整体替换）。"""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge_dict(base[k], v)
        else:
            base[k] = v


def main():
    cfg = load_json("config.json")
    dev = load_json(cfg["source"])

    entries = {}  # canonical id -> entry（扁平单值）
    conflicts = []  # (model, 保留渠道, 被舍渠道) 兜底报告
    filtered = []  # native 过滤清单（渠道遗漏配置可见）
    provider_ids = {pk: set(dev[pk].get("models", {})) for pk in dev}

    for key, conf in cfg["provider_filter"].items():
        if conf.get("action") == "both":
            provs = [key] + sorted(k for k in dev if k.startswith(key + "-"))
        else:
            provs = [key]
        for missing in [k for k in provs if k not in dev]:
            print(f"警告: provider {missing} 不在 models.dev，跳过", file=sys.stderr)
        provs = [k for k in provs if k in dev]

        native = conf.get("native") or []
        include, exclude = conf.get("include"), conf.get("exclude")
        prefix = conf.get("key_prefix", "")

        for pk in provs:
            channel = conf.get("channel", pk) if pk == key else pk
            models = dev[pk].get("models", {})

            for mid, m in models.items():
                # native：官方渠道只取自家模型（cross-listing 三方模型滤掉）
                if native and not matches_any(mid, native):
                    filtered.append(f"{channel}: {mid}")
                    continue
                if include and not any(fnmatch.fnmatch(mid, g) for g in include):
                    continue
                if exclude and any(fnmatch.fnmatch(mid, g) for g in exclude):
                    continue

                # 快照折叠：日期后缀剥掉后同 provider 存在 base → 折叠为 alias
                canon, snap_alias = mid, None
                stripped = DATE_SUFFIX.sub("", mid)
                if stripped != mid and stripped in provider_ids[pk]:
                    canon, snap_alias = stripped, mid

                base_m = models.get(canon, m)
                e = entries.get(canon)
                if e is None:
                    e = entries[canon] = {
                        "id": canon,
                        "name": base_m.get("name") or canon,
                        "kind": derive_kind(base_m),
                        "channel": channel,
                        "capabilities": {
                            "vision": bool(base_m.get("attachment")),
                            "tools": bool(base_m.get("tool_call")),
                        },
                    }
                    ctx = (base_m.get("limit") or {}).get("context", 0)
                    out_ = (base_m.get("limit") or {}).get("output", 0)
                    if ctx > 0 or out_ > 0:
                        e["limit"] = {"context": ctx, "output": out_}
                    price = norm_price(base_m.get("cost"))
                    if price:
                        e["price"] = price
                elif e is not None and pk == key and e["channel"] != channel:
                    # 跨厂商同名（native 过滤后理论归零）：保留首个 + 报告兜底
                    conflicts.append((canon, e["channel"], channel))
                elif e is not None:
                    # 同渠道重复（快照折叠 base 后到）或 both 变体端点同名：
                    # 数据已定（快照先建时取的就是 base 数据；变体以主渠道为准），
                    # 静默跳过（reasoning/alias 已在建条目时处理）
                    continue

                # reasoning 归位 capabilities；efforts 并集（渠道间不一致宁多勿漏）
                if m.get("reasoning") or model_efforts(m):
                    r = e["capabilities"].setdefault("reasoning", {"supported": True})
                    r["supported"] = True
                    if model_efforts(m):
                        merged = set(r.get("efforts", [])) | set(model_efforts(m))
                        r["efforts"] = sort_efforts(merged)
                if m.get("attachment"):
                    e["capabilities"]["vision"] = True

                aliases = e.setdefault("aliases", [])
                for cand in (snap_alias, prefix + mid if prefix else None):
                    if cand and cand != canon and cand not in aliases:
                        aliases.append(cand)

    for model, keep, drop in conflicts:
        print(f"渠道冲突: {model} 保留 {keep} 舍 {drop}（建议 config native/include 消歧）", file=sys.stderr)
    for item in filtered:
        print(f"native 过滤（非自家模型）: {item}", file=sys.stderr)

    # 清理空集合
    for e in entries.values():
        if not e["capabilities"].get("reasoning", {}).get("supported", False):
            e["capabilities"].pop("reasoning", None)
        elif not e["capabilities"]["reasoning"].get("efforts"):
            e["capabilities"]["reasoning"].pop("efforts", None)
        if not e.get("aliases"):
            e.pop("aliases", None)

    # overrides 人工修正层（键 = 模型 id 或 "id#channel"）
    if os.path.exists("overrides.json"):
        ov = load_json("overrides.json")
        for k, patch in ov.items():
            if "#" in k:
                mid, ch = k.split("#", 1)
                if mid in entries and entries[mid].get("channel") == ch:
                    merge_dict(entries[mid], patch)
            elif k in entries:
                merge_dict(entries[k], patch)

    # 校验分级：chat 必须 context（缺失=上游异常警告跳过）；非 chat 不要求
    for k in list(entries):
        e = entries[k]
        if not e["id"]:
            print(f"校验失败（空 id）: {k}", file=sys.stderr)
            sys.exit(1)
        if e.get("kind") == "chat" and e.get("limit", {}).get("context", 0) <= 0:
            print(f"跳过（chat 模型缺 context，上游数据异常）: {k}", file=sys.stderr)
            del entries[k]

    out = {
        "schema": SCHEMA,
        "updated_at": date.today().isoformat(),
        "models": [entries[k] for k in sorted(entries)],
    }
    with open("models.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    kinds = {}
    for e in entries.values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"OK: {len(entries)} 模型 → models.json；类型分布: {kinds}")


if __name__ == "__main__":
    main()
