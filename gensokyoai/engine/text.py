"""纯文本帮助函数 —— 从老 loop.py 平移而来，无状态。

包含事件的 wire/text 投影、把模型有时写成正文的"工具调用"抢救回正文的
`salvage`、以及一些防御性的类型转换。
"""

import json
import re

from ..room import Event


def segments_of(arguments: dict) -> list[dict]:
    """模型给的话 -> 段列表。目前只有文本，场景可以自己扩。"""
    text = str(arguments.get("text") or "").strip()
    return [{"type": "text", "text": text}] if text else []


def text_of(segments: list[dict]) -> str:
    """段列表里的纯文本部分。"""
    return "".join(
        str(segment.get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    ).strip()


def last_message(events: list[Event]) -> str:
    """最近一条别人的话（记忆召回/写入的话题锚点）。"""
    for event in reversed(events):
        if event.kind == "message":
            return event.text
    return ""


def salvage(text: str) -> str:
    """从"写成正文的工具调用"里抠出真正要说的话；抠不出来返回空串。

    认这些形状：`send_message text 你好`、`send_message("你好")`、裸 JSON、
    `<tool_call>{...}</tool_call>`。普通句子原样返回。
    """
    raw = text.strip()
    if not raw:
        return ""
    if re.fullmatch(r"(send_message|wait|read_events)", raw, re.I):
        return ""

    block = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw, re.S)
    if block:
        return _payload_of(block.group(1))
    if raw.startswith("{") and raw.endswith("}"):
        return _payload_of(raw)

    match = re.match(
        r"(?:send_message|wait|read_events)\s*(?P<open>[(（:：])?\s*"
        r"(?:text|message|content)?\s*[:=]?\s*(?P<body>.+)$",
        raw,
        re.S | re.I,
    )
    if match:
        body = match.group("body")
        if match.group("open") in ("(", "（"):
            body = body.rstrip(")）").rstrip()
        return _unquote(body)
    return raw


def images_of(shown: list[Event]) -> list[tuple[str, str]]:
    """把这一批里的图收集成 [(说明, data URL)]，顺序与 events 一致。"""
    found: list[tuple[str, str]] = []
    for event in shown:
        for index, url in enumerate(event.images, 1):
            found.append((f"{event.sender} 发的第 {index} 张图：", url))
    return found


def chance(value) -> float:
    """把概率夹到 [0, 1]；写错了当必醒。"""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def to_int(value, default: int) -> int:
    """把模型给的参数转成整数，转不动就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _payload_of(raw: str) -> str:
    """从 JSON 里取要说的话（认 text / message / content，或嵌在 arguments 里）。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("text", "message", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _unquote(value)
    arguments = data.get("arguments")
    if isinstance(arguments, dict):
        return _payload_of(json.dumps(arguments, ensure_ascii=False))
    if isinstance(arguments, str):
        return salvage(arguments)
    return ""


def _unquote(text: str) -> str:
    """去掉包裹的引号/括号，别把标点当内容。"""
    cleaned = text.strip()
    for left, right in (('"', '"'), ("'", "'"), ("「", "」"), ("“", "”"), ("`", "`")):
        if len(cleaned) > 1 and cleaned.startswith(left) and cleaned.endswith(right):
            cleaned = cleaned[1:-1].strip()
            break
    return cleaned
