#!/usr/bin/env python3
"""Evidence-preserving structured analysis and fixed-template reports."""

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from channel_adapters import get_channel_adapter

SCHEMA_VERSION = "report-analysis/v1"


def _text(value, default="暂无数据"):
    if value is None or value == "":
        return default
    return " ".join(str(value).split())


def _number(value, suffix=""):
    try:
        number = float(value)
        return f"{number:.2f}{suffix}" if math.isfinite(number) else "暂无"
    except (TypeError, ValueError):
        return "暂无"


def _quote_line(row):
    if not isinstance(row, dict):
        return _text(row)
    name = row.get("名称") or row.get("观察池名称") or row.get("持仓名称") or row.get("股票") or "标的"
    code = row.get("代码") or row.get("symbol") or ""
    if row.get("error"):
        return f"{name} {code}：{_text(row['error'])}"
    price = row.get("最新价") if row.get("最新价") is not None else row.get("收盘价")
    pct = row.get("涨跌幅%") if row.get("涨跌幅%") is not None else row.get("涨跌幅")
    stamp = row.get("数据时间") or row.get("时间") or "日期未提供"
    source = row.get("数据源") or row.get("数据来源") or "来源未提供"
    return f"{name} {code}：{_number(price)}，涨跌幅 {_number(pct, '%')}｜{stamp}｜{source}"


def _list_items(value, formatter=None, limit=8):
    if isinstance(value, list):
        rows = value[:limit]
        return [(formatter(item) if formatter else _text(item)) for item in rows] or ["暂无数据"]
    if isinstance(value, dict):
        if value.get("error"):
            return [_text(value["error"])]
        return [f"{_text(key)}：{_text(item)}" for key, item in list(value.items())[:limit]] or ["暂无数据"]
    return [_text(value)]


def _news_line(item):
    if not isinstance(item, dict):
        return _text(item)
    title = item.get("新闻标题") or item.get("title") or "未命名消息"
    topics = item.get("命中的题材") or item.get("命中题材") or "暂无题材映射"
    if isinstance(topics, list):
        topics = "、".join(map(str, topics)) or "暂无题材映射"
    return f"{_text(title)}｜题材：{_text(topics)}"


def _plan_line(item):
    if not isinstance(item, dict):
        return _text(item)
    stock = item.get("股票") or "未命名标的"
    focus = item.get("盘前看点") or item.get("处理建议") or item.get("备注") or "按计划观察"
    return f"{_text(stock)}：{_text(focus)}"


def _monitor_line(value):
    if not isinstance(value, dict):
        return _text(value)
    return f"状态 {_text(value.get('状态'))}；刷新 {value.get('刷新频率_秒', '未知')} 秒；渠道 {_text(value.get('提醒渠道'))}"


def _base(report_type, variant, title, summary, sections, warnings=None, status="OK", date=None):
    return {"schema_version": SCHEMA_VERSION, "report_type": report_type, "variant": variant,
            "title": title, "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "report_date": date, "status": status, "summary": _text(summary), "sections": sections,
            "warnings": list(dict.fromkeys(warnings or []))}


def _sections(data):
    value = data.get("sections") if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def build_morning_analysis(data, variant="full"):
    sections = _sections(data)
    warnings = list(data.get("warnings") or []) if isinstance(data, dict) else []
    news = sections.get("隔夜/盘前重要消息")
    if not isinstance(news, list) or not news:
        warnings.append("盘前新闻数据缺失，无法给出新闻驱动结论。")
    elif any(not isinstance(item, dict) or not item.get("发布时间") for item in news):
        warnings.append("新闻仅为网页标题，未提供可核验发布时间，不能确认属于隔夜或盘前。")
    result_sections = [
        {"id": "news", "title": "新闻线索（时间须核验）", "items": _list_items(news, _news_line, 8 if variant == "full" else 3)},
        {"id": "topics", "title": "关键词映射题材（非强弱排名）", "items": _list_items(sections.get("热点题材方向"), limit=8 if variant == "full" else 4)},
        {"id": "watch", "title": "观察池通用预案", "items": _list_items(sections.get("观察池盘前预案"), _plan_line, 6 if variant == "full" else 3)},
        {"id": "holding", "title": "持仓池通用预案", "items": _list_items(sections.get("持仓池盘前预案"), _plan_line, 6 if variant == "full" else 3)},
    ]
    if variant == "full":
        result_sections.extend([
            {"id": "monitor", "title": "盯盘状态", "items": [_monitor_line(sections.get("实时盯盘状态"))]},
            {"id": "risks", "title": "风险清单", "items": _list_items(sections.get("需要回避的风险"))},
        ])
    summary = sections.get("今日盘前核心结论") if not warnings else "盘前证据不完整；以下新闻线索和通用预案需核验，暂不形成确定的方向判断。"
    return _base("morning_report", variant, "A股盘前报告", summary, result_sections, warnings,
                 "WARN" if warnings else "OK", (data or {}).get("date") if isinstance(data, dict) else None)


def _lhb_items(value, limit=5):
    if not isinstance(value, dict):
        return _list_items(value, limit=limit)
    if value.get("error"):
        return [value["error"]]
    stats = value.get("龙虎榜完整统计") or {}
    items = [f"明细 {stats.get('原始记录数', '暂无')} 条；按日期与代码去重 {stats.get('去重后日期个股数', stats.get('去重后个股数', '暂无'))} 组；缺失或冲突 {stats.get('金额缺失或冲突数', '暂无')} 组。"]
    conclusion = (value.get("龙虎榜资金方向判断") or {}).get("结论")
    if conclusion:
        items.append(conclusion)
    for row in (value.get("龙虎榜净买入前列") or [])[:limit]:
        items.append(f"{row.get('股票') or row.get('代码')} {row.get('日期', '')}：净买额 {_number(row.get('龙虎榜净买额'))} 元")
    return items


def build_after_close_analysis(data, variant="full"):
    data = data if isinstance(data, dict) else {}
    sections = _sections(data)
    warnings = list(data.get("warnings") or [])
    overview = sections.get("全A市场概览")
    if not isinstance(overview, dict) or overview.get("error"):
        warnings.append("全A行情数据缺失，无法确认市场宽度。")
    elif overview.get("使用缓存"):
        warnings.append(f"全A行情使用缓存，获取于 {overview.get('cached_at', '未知')}；不视为最新行情。")
    if isinstance(overview, dict) and overview.get("时效说明"):
        warnings.append(overview["时效说明"])
    width = overview.get("市场宽度") if isinstance(overview, dict) else {}
    width = width if isinstance(width, dict) and isinstance(overview, dict) and not overview.get("error") else {}
    if not width or width.get("上涨家数") is None or width.get("下跌家数") is None:
        warnings.append("市场宽度缺失。")
    if width.get("涨跌幅缺失家数"):
        warnings.append(f"全A涨跌幅缺失 {width['涨跌幅缺失家数']} 家，宽度仅统计有效样本。")
    width_line = f"上涨 {_text(width.get('上涨家数'), '暂无')} 家，下跌 {_text(width.get('下跌家数'), '暂无')} 家，平盘 {_text(width.get('平盘家数'), '暂无')} 家；涨幅≥9.8% {_text(width.get('涨停数量_估算'), '暂无')} 家，跌幅≤-9.8% {_text(width.get('跌停数量_估算'), '暂无')} 家（非交易所涨跌停统计）"
    index_rows = sections.get("指数概览") or sections.get("主要指数") or sections.get("指数表现")
    if not isinstance(index_rows, list) or not index_rows or not any(isinstance(row, dict) and not row.get("error") and _number(row.get("涨跌幅%")) != "暂无" for row in index_rows):
        warnings.append("指数数据不足，无法确认指数强弱。")
    for key in ("指数概览", "观察池重点跟踪", "持仓池重点跟踪"):
        rows = sections.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("error"):
                    warnings.append(f"{row.get('代码') or row.get('symbol') or key}：{row['error']}")
    target = sections.get("观察对象") or {}
    target_row = target.get("行情") if isinstance(target, dict) else None
    holding_only = data.get("review_scope") == "holding_pool"
    stock_only = data.get("symbol") or (isinstance(target, dict) and target.get("symbol") not in (None, "未指定"))
    title = "A股持仓复盘" if holding_only else f"个股盘后复盘 {data.get('symbol') or target.get('symbol')}" if stock_only else "A股盘后复盘"
    result_sections = []
    if stock_only:
        result_sections.append({"id": "stock", "title": "指定标的", "items": [_quote_line(target_row)]})
        if not isinstance(target_row, dict) or target_row.get("error"):
            warnings.append("指定标的行情缺失，无法完成个股复盘。")
    if holding_only:
        result_sections.append({"id": "holding", "title": "持仓池", "items": _list_items(sections.get("持仓池重点跟踪"), _quote_line, 12)})
    result_sections.extend([
        {"id": "indices", "title": "主要指数", "items": _list_items(index_rows, _quote_line, 6 if variant == "full" else 4)},
        {"id": "breadth", "title": "市场温度", "items": [width_line]},
    ])
    if not stock_only and not holding_only:
        result_sections.extend([
            {"id": "watch", "title": "观察池", "items": _list_items(sections.get("观察池重点跟踪"), _quote_line, 6 if variant == "full" else 3)},
            {"id": "holding", "title": "持仓池", "items": _list_items(sections.get("持仓池重点跟踪"), _quote_line, 6 if variant == "full" else 3)},
        ])
    lhb = sections.get("龙虎榜增强分析") or sections.get("龙虎榜摘要")
    if isinstance(lhb, dict):
        warnings.extend(lhb.get("数据提示") or [])
        if lhb.get("error"):
            warnings.append(lhb["error"])
        stats = lhb.get("龙虎榜完整统计") or {}
        if stats.get("金额缺失或冲突数"):
            warnings.append("龙虎榜资金统计包含缺失或冲突记录，仅展示有效样本。")
    lhb_meta = sections.get("龙虎榜")
    if isinstance(lhb_meta, dict) and lhb_meta.get("error"):
        warnings.append(lhb_meta["error"])
    if variant == "full":
        result_sections.extend([
            {"id": "lhb", "title": "龙虎榜与资金", "items": _lhb_items(lhb)},
            {"id": "monitor", "title": "盯盘状态", "items": [_monitor_line(sections.get("实时盯盘状态"))]},
        ])
    result_sections.append({"id": "tomorrow", "title": "下一交易日通用观察项", "items": ["核验主线承接、指数与个股表现是否同步；此项为观察清单，不是已发生的市场事实。"]})
    summary = sections.get("简短判断") or "收盘数据不完整，暂不对市场强弱作确定判断。"
    if stock_only:
        summary = _quote_line(target_row) + "。此报告覆盖日线行情与龙虎榜，未包含财报、行业比较或交易归因。"
    elif holding_only:
        summary = "以持仓名单逐项核对当日行情；未提供交易记录与历史仓位，不能据此计算组合收益或交易归因。"
    if warnings:
        summary = "数据不完整，以下结论仅基于可用样本。" + summary
    return _base("after_close_report", variant, title, summary, result_sections, warnings,
                 "WARN" if warnings else "OK", data.get("date"))


def fallback_analysis(report_type, variant="full", reason="结构化分析不可用"):
    title = "A股盘前报告" if report_type == "morning_report" else "A股盘后复盘"
    return _base(report_type, variant, title, "关键数据暂缺，本报告仅展示安全兜底信息。",
                 [{"id": "fallback", "title": "当前状态", "items": ["数据采集或结构化分析失败，请稍后重试。"]}], [reason], "WARN")


def parse_analysis_candidates(candidates, report_type, variant="full"):
    """Validate nested types before accepting a model response into a renderer."""
    if not isinstance(candidates, (list, tuple)):
        candidates = []
    for candidate in candidates:
        try:
            payload = candidate if isinstance(candidate, dict) else json.loads(candidate)
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION or payload.get("report_type") != report_type or payload.get("variant") != variant:
                continue
            if payload.get("status") not in {"OK", "WARN"}:
                continue
            if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in ("title", "summary", "generated_at")):
                continue
            if not isinstance(payload.get("warnings"), list) or any(not isinstance(item, str) for item in payload["warnings"]):
                continue
            sections = payload.get("sections")
            if not isinstance(sections, list) or not sections or len(sections) > 24:
                continue
            if any(not isinstance(section, dict) or not isinstance(section.get("id"), str) or not isinstance(section.get("title"), str) or not isinstance(section.get("items"), list) or any(not isinstance(item, str) for item in section["items"]) for section in sections):
                continue
            return payload, {"ok": True, "fallback_used": False}
        except (TypeError, ValueError):
            continue
    return fallback_analysis(report_type, variant, "模型 JSON 无效，已启用固定兜底模板。"), {"ok": False, "fallback_used": True}


def render_report(data, report_type, variant="full", channel="telegram"):
    if report_type == "morning_report":
        analysis = build_morning_analysis(data, variant)
    elif report_type == "after_close_report":
        analysis = build_after_close_analysis(data, variant)
    else:
        analysis = fallback_analysis(report_type, variant, "未知报告类型。")
    text = get_channel_adapter(channel).render_report(analysis)
    return {"analysis": analysis, "channel": channel, "text": text}
