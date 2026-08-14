#!/usr/bin/env python3
"""honeybadger-models 同步：models.dev → models.json（hb-models/v2）。

规则见 README：provider_filter 圈选（exact/both）、跨渠道同名归并为 ports、
同渠道日期快照折叠为 alias、efforts 并集、overrides.json 人工修正层。
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


def model_efforts(m):
    for opt in m.get("reasoning_options") or []:
        if opt.get("type") == "effort":
            vals = opt.get("values") or []
            return [v for v in vals if v not in ("", "none", "null", "default")]
    return []


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

    entries = {}  # canonical model id -> entry
    # 预扫每个 provider 的模型 id 集合（快照折叠要查同 provider 是否存在 base）
    provider_ids = {pk: set(dev[pk].get("models", {})) for pk in dev}

    for key, conf in cfg["provider_filter"].items():
        # 渠道集合：exact = 主 key；both = 主 key + 全部 <key>-* 变体（变体字典序）
        if conf.get("action") == "both":
            provs = [key] + sorted(k for k in dev if k.startswith(key + "-"))
        else:
            provs = [key]
        for missing in [k for k in provs if k not in dev]:
            print(f"警告: provider {missing} 不在 models.dev，跳过", file=sys.stderr)
        provs = [k for k in provs if k in dev]

        include, exclude = conf.get("include"), conf.get("exclude")
        prefix = conf.get("key_prefix", "")

        for pk in provs:
            # 主渠道用 config 的 channel 名；变体渠道用 models.dev 原始 id（保留区分度）
            channel = conf.get("channel", pk) if pk == key else pk
            models = dev[pk].get("models", {})

            for mid, m in models.items():
                if include and not any(fnmatch.fnmatch(mid, g) for g in include):
                    continue
                if exclude and any(fnmatch.fnmatch(mid, g) for g in exclude):
                    continue

                # 快照折叠：日期后缀剥掉后同 provider 存在 base → 折叠为 alias
                canon, snap_alias = mid, None
                stripped = DATE_SUFFIX.sub("", mid)
                if stripped != mid and stripped in provider_ids[pk]:
                    canon, snap_alias = stripped, mid

                base_m = models.get(canon, m)  # 快照的 name/limit 取 base
                kind = derive_kind(base_m)
                e = entries.get(canon)
                if e is None:
                    e = entries[canon] = {
                        "id": canon,
                        "name": base_m.get("name") or canon,
                        "kind": kind,
                        "capabilities": {
                            "chat": kind == "chat",
                            "vision": bool(base_m.get("attachment")),
                            "tools": bool(base_m.get("tool_call")),
                        },
                    }
                    ctx = (base_m.get("limit") or {}).get("context", 0)
                    out_ = (base_m.get("limit") or {}).get("output", 0)
                    if ctx > 0 or out_ > 0:  # 非 chat 模型可能无 limit（生图/TTS）
                        e["limit"] = {"context": ctx, "output": out_}
                    if base_m.get("reasoning"):
                        e["reasoning"] = {"supported": True}
                    if model_efforts(base_m):
                        e["reasoning"] = {"supported": True, "efforts": model_efforts(base_m)}

                # efforts 并集（跨渠道不一致宁多勿漏）
                if model_efforts(m):
                    r = e.setdefault("reasoning", {"supported": True})
                    r["supported"] = True
                    merged = set(r.get("efforts", [])) | set(model_efforts(m))
                    r["efforts"] = sort_efforts(merged)
                if m.get("reasoning"):
                    e.setdefault("reasoning", {})["supported"] = True
                if m.get("attachment"):
                    e["capabilities"]["vision"] = True

                # port（每渠道一个；重复渠道幂等跳过）
                port = next((p for p in e.setdefault("ports", []) if p["channel"] == channel), None)
                if port is None:
                    port = {"channel": channel}
                    e["ports"].append(port)
                price = norm_price(m.get("cost"))
                if price and "price" not in port:
                    port["price"] = price
                aliases = port.setdefault("aliases", [])
                for cand in {snap_alias, prefix + mid if prefix else None}:
                    if cand and cand != canon and cand not in aliases:
                        aliases.append(cand)

    # 清理：空 aliases / 无价格的 port 保留（价格缺失是真缺数据）；空 reasoning.efforts 移除
    for e in entries.values():
        if not e.get("reasoning", {}).get("supported", False):
            e.pop("reasoning", None)
        else:
            e["reasoning"].pop("efforts", None) if not e["reasoning"].get("efforts") else None
        for p in e.get("ports", []):
            if not p.get("aliases"):
                p.pop("aliases", None)

    # overrides 人工修正层（键 = 模型 id 或 "id#channel"）
    if os.path.exists("overrides.json"):
        ov = load_json("overrides.json")
        for k, patch in ov.items():
            if "#" in k:
                mid, ch = k.split("#", 1)
                for p in entries.get(mid, {}).get("ports", []):
                    if p["channel"] == ch:
                        merge_dict(p, patch)
            elif k in entries:
                merge_dict(entries[k], patch)

    # 校验分级：chat 模型必须有 context（缺失=上游数据异常，警告跳过）；
    # image/audio/video 模型 context 可选（生图/TTS 无上下文窗口概念）。
    skipped = []
    for k, e in list(entries.items()):
        if not e["id"]:
            print(f"校验失败（空 id）: {k}", file=sys.stderr)
            sys.exit(1)
        if e.get("kind") == "chat" and e.get("limit", {}).get("context", 0) <= 0:
            print(f"跳过（chat 模型缺 context，上游数据异常）: {k}", file=sys.stderr)
            skipped.append(k)
            del entries[k]
    # 非 chat 且无 limit 的条目正常保留（价格/能力仍有效）

    out = {
        "schema": "hb-models/v2",
        "updated_at": date.today().isoformat(),
        "models": [entries[k] for k in sorted(entries)],
    }
    with open("models.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    n_ports = sum(len(e.get("ports", [])) for e in entries.values())
    kinds = {}
    for e in entries.values():
        kinds[e.get("kind", "chat")] = kinds.get(e.get("kind", "chat"), 0) + 1
    print(f"OK: {len(entries)} 模型 / {n_ports} 渠道 → models.json；类型分布: {kinds}")


if __name__ == "__main__":
    main()
