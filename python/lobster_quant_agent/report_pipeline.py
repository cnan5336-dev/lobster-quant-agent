#!/usr/bin/env python3
"""Structured analysis and fixed-template report pipeline."""

import json
from datetime import datetime

from channel_adapters import get_channel_adapter


SCHEMA_VERSION = "report-analysis/v1"


def _text(value, default="暂无数据"):
    if value is None or value == "":
        return default
    return " ".join(str(value).split())


def _number(value, suffix=""):
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return "暂无"


def _quote_line(row):
    if not isinstance(row, dict) or row.get("error"):
        return _text(row.get("error") if isinstance(row, dict) else row)
    name = row.get("名称") or row.get("观察池名称") or row.get("持仓名称") or row.get("股票") or "标的"
    code = row.get("代码") or row.get("symbol") or ""
    price = row.get("最新价") if row.get("最新价") is not None else row.get("收盘价")
    pct = row.get("涨跌幅%") if row.get("涨跌幅%") is not None else row.get("涨跌幅")
    return f"{name} {code}：{_number(price)}，涨跌幅 {_number(pct, '%')}"


def _list_items(value, formatter=None, limit=8):
    if isinstance(value, list):
        rows = value[:limit]
        return [(formatter(item) if formatter else _text(item)) for item in rows] or ["暂无数据"]
    if isinstance(value, dict):
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
    return (
        f"状态 {_text(value.get('状态'))}；刷新 {value.get('刷新频率_秒', '未知')} 秒；"
        f"渠道 {_text(value.get('提醒渠道'))}"
    )


def _base(report_type, variant, title, summary, sections, warnings=None, status="OK"):
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": report_type,
        "variant": variant,
        "title": title,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "summary": _text(summary),
        "sections": sections,
        "warnings": list(warnings or []),
    }


def build_morning_analysis(data, variant="full"):
    sections = (data or {}).get("sections", {}) if isinstance(data, dict) else {}
    warnings = []
    news = sections.get("隔夜/盘前重要消息")
    if not isinstance(news, list):
        warnings.append("盘前新闻数据缺失，已使用兜底内容。")
    result_sections = [
        {"id": "news", "title": "隔夜与盘前消息", "items": _list_items(news, _news_line, 8 if variant == "full" else 3)},
        {"id": "topics", "title": "热点题材", "items": _list_items(sections.get("热点题材方向"), limit=8 if variant == "full" else 4)},
        {"id": "watch", "title": "观察池预案", "items": _list_items(sections.get("观察池盘前预案"), _plan_line, 6 if variant == "full" else 3)},
        {"id": "holding", "title": "持仓池预案", "items": _list_items(sections.get("持仓池盘前预案"), _plan_line, 6 if variant == "full" else 3)},
    ]
    if variant == "full":
        result_sections.extend([
            {"id": "monitor", "title": "盯盘状态", "items": [_monitor_line(sections.get("实时盯盘状态"))]},
            {"id": "risks", "title": "风险清单", "items": _list_items(sections.get("需要回避的风险"), limit=8)},
        ])
    return _base(
        "morning_report", variant, "A股盘前报告",
        sections.get("今日盘前核心结论") or "盘前数据不完整，先控制仓位并等待开盘确认。",
        result_sections, warnings, "WARN" if warnings else "OK",
    )


def build_after_close_analysis(data, variant="full"):
    sections = (data or {}).get("sections", {}) if isinstance(data, dict) else {}
    overview = sections.get("全A市场概览")
    warnings = []
    if not isinstance(overview, dict) or overview.get("error"):
        warnings.append("全A行情数据缺失，市场宽度使用兜底说明。")
    width = overview.get("市场宽度", {}) if isinstance(overview, dict) else {}
    width_line = (
        f"上涨 {width.get('上涨家数', '暂无')} 家，下跌 {width.get('下跌家数', '暂无')} 家，"
        f"涨停约 {width.get('涨停数量_估算', '暂无')} 家，跌停约 {width.get('跌停数量_估算', '暂无')} 家"
    )
    index_rows = sections.get("指数概览") or sections.get("主要指数") or sections.get("指数表现")
    result_sections = [
        {"id": "indices", "title": "主要指数", "items": _list_items(index_rows, _quote_line, 6 if variant == "full" else 4)},
        {"id": "breadth", "title": "市场温度", "items": [width_line]},
        {"id": "watch", "title": "观察池", "items": _list_items(sections.get("观察池重点跟踪"), _quote_line, 6 if variant == "full" else 3)},
        {"id": "holding", "title": "持仓池", "items": _list_items(sections.get("持仓池重点跟踪"), _quote_line, 6 if variant == "full" else 3)},
    ]
    if variant == "full":
        lhb = sections.get("龙虎榜增强分析") or sections.get("龙虎榜摘要")
        result_sections.extend([
            {"id": "lhb", "title": "龙虎榜与资金", "items": _list_items(lhb, limit=8)},
            {"id": "monitor", "title": "盯盘状态", "items": [_monitor_line(sections.get("实时盯盘状态"))]},
        ])
    result_sections.append({
        "id": "tomorrow",
        "title": "下一交易日观察",
        "items": ["关注主线承接、指数与个股赚钱效应是否同步，避免高位题材冲高回落。"],
    })
    return _base(
        "after_close_report", variant, "A股盘后复盘",
        sections.get("简短判断") or "收盘数据不完整，暂不对市场强弱作确定判断。",
        result_sections, warnings, "WARN" if warnings else "OK",
    )


def fallback_analysis(report_type, variant="full", reason="结构化分析不可用"):
    title = "A股盘前报告" if report_type == "morning_report" else "A股盘后复盘"
    return _base(
        report_type, variant, title,
        "关键数据暂缺，本报告仅展示安全兜底信息。",
        [{"id": "fallback", "title": "当前状态", "items": ["数据采集或结构化分析失败，请稍后重试。"]}],
        [reason], "WARN",
    )


def parse_analysis_candidates(candidates, report_type, variant="full"):
    """Accept only valid structured model output; fall back after retries."""
    for candidate in candidates:
        try:
            payload = candidate if isinstance(candidate, dict) else json.loads(candidate)
            if (
                isinstance(payload, dict)
                and payload.get("schema_version") == SCHEMA_VERSION
                and payload.get("report_type") == report_type
                and isinstance(payload.get("sections"), list)
            ):
                return payload, {"ok": True, "fallback_used": False}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return fallback_analysis(report_type, variant, "模型 JSON 无效，已启用固定兜底模板。"), {
        "ok": False,
        "fallback_used": True,
    }


def render_report(data, report_type, variant="full", channel="telegram"):
    if report_type == "morning_report":
        analysis = build_morning_analysis(data, variant)
    elif report_type == "after_close_report":
        analysis = build_after_close_analysis(data, variant)
    else:
        analysis = fallback_analysis(report_type, variant, "未知报告类型。")
    text = get_channel_adapter(channel).render_report(analysis)
    return {"analysis": analysis, "channel": channel, "text": text}
