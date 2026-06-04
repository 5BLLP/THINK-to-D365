from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import re
from typing import Any


HTTP_STATUS_PATTERN = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})")


def build_odata_filter(field_name: str, value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{field_name} eq {value}"
    escaped = str(value).replace("'", "''")
    return f"{field_name} eq '{escaped}'"


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def extract_http_status(http_payload: str) -> int:
    match = HTTP_STATUS_PATTERN.search(http_payload)
    if not match:
        raise ValueError(f"Unable to parse HTTP status from batch part: {http_payload[:200]}")
    return int(match.group(1))


def extract_http_body(http_payload: str) -> str:
    normalized = http_payload.replace("\r\n", "\n")
    marker_index = normalized.find("\n\n")
    if marker_index == -1:
        return ""
    return normalized[marker_index + 2 :].strip()


def parse_batch_http_parts(content_type: str, content: bytes) -> list[dict[str, Any]]:
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + content
    message = BytesParser(policy=default).parsebytes(raw)
    parts: list[dict[str, Any]] = []
    _collect_http_parts(message, parts)
    return parts


def _collect_http_parts(part: Any, output: list[dict[str, Any]]) -> None:
    if part.is_multipart():
        for subpart in part.iter_parts():
            _collect_http_parts(subpart, output)
        return
    if part.get_content_type() != "application/http":
        return
    payload_text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    output.append(
        {
            "content_id": part.get("Content-ID"),
            "status_code": extract_http_status(payload_text),
            "body": extract_http_body(payload_text),
        }
    )
