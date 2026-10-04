#!/usr/bin/env python3
"""Presentation-only adapters for stock assistant channels."""

from abc import ABC, abstractmethod


def _clean(value):
    text = " ".join(str(value if value is not None else "").split())
    return text or "暂无数据"


def _short(value, limit=180):
    text = _clean(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ChannelAdapter(ABC):
    name = "base"

    @abstractmethod
    def render_report(self, analysis):
        raise NotImplementedError


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def render_report(self, analysis):
        lines = [
            f"# {_clean(analysis.get('title'))}",
            f"状态：{_clean(analysis.get('status'))}",
            f"生成时间：{_clean(analysis.get('generated_at'))}",
            f"报告日期：{_clean(analysis.get('report_date'))}",
            "",
            f"核心结论：{_clean(analysis.get('summary'))}",
        ]
        for section in analysis.get("sections", []):
            lines.extend(["", f"## {_clean(section.get('title'))}"])
            items = section.get("items") or ["暂无数据"]
            lines.extend(f"- {_clean(item)}" for item in items)
        warnings = analysis.get("warnings") or []
        if warnings:
            lines.extend(["", "## 数据提示"])
            lines.extend(f"- {_clean(item)}" for item in warnings)
        lines.extend(["", "仅供研究参考，不构成投资建议。"])
        return "\n".join(lines).strip()


class WeixinAdapter(ChannelAdapter):
    name = "weixin"

    def render_report(self, analysis):
        lines = [
            _clean(analysis.get("title")),
            f"生成于 {_clean(analysis.get('generated_at'))}｜{_clean(analysis.get('status'))}",
            f"报告日期：{_clean(analysis.get('report_date'))}",
            "",
            "【先看结论】",
            _short(analysis.get("summary"), 240),
        ]
        for section in analysis.get("sections", []):
            lines.extend(["", f"【{_clean(section.get('title'))}】"])
            items = section.get("items") or ["暂无数据"]
            lines.extend(f"• {_short(item)}" for item in items)
        warnings = analysis.get("warnings") or []
        if warnings:
            lines.extend(["", "【数据提示】"])
            lines.extend(f"• {_short(item)}" for item in warnings)
        lines.extend(["", "仅供研究参考，不构成投资建议。"])
        return "\n".join(lines).strip()


class QQAdapter(WeixinAdapter):
    """Compact plain-text output compatible with the OpenClaw qqbot extension."""

    name = "qq"


def get_channel_adapter(channel):
    normalized = str(channel or "telegram").strip().lower()
    if normalized in {"weixin", "wechat", "微信"}:
        return WeixinAdapter()
    if normalized in {"telegram", "tg"}:
        return TelegramAdapter()
    if normalized in {"qq", "qqbot"}:
        return QQAdapter()
    raise ValueError(f"不支持的通道：{channel}")
