#!/usr/bin/env python3
"""Extract visible messages from a public ChatGPT share page as Markdown."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ALLOWED_HOST = "chatgpt.com"
MAX_INPUT_BYTES = 50 * 1024 * 1024
ENQUEUE_RE = re.compile(
    r'(?:window\.__reactRouterContext\.streamController\.)?'
    r'enqueue\(\s*"((?:[^"\\]|\\.)*)"\s*\)'
)

POWERSHELL_HINT = """可选的 Windows 回退方案：
  Invoke-WebRequest -Uri "<share-url>" -UseBasicParsing -TimeoutSec 40 -UserAgent "Mozilla/5.0" |
    Select-Object -ExpandProperty Content | Out-File share.html -Encoding utf8
  然后运行: python extract_chatgpt_share.py share.html -o conversation.md
请只使用当前环境允许的网络方式，不要提供 Cookie、令牌或尝试绕过访问控制。
"""


class ExtractionError(Exception):
    """A safe, stage-labelled error suitable for CLI output."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message

    def __str__(self) -> str:
        return f"{self.stage}: {self.message}"


def _validate_remote_target(url: str) -> None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ExtractionError("URL validation", "the link is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise ExtractionError("URL validation", "only HTTPS links are supported")
    if parsed.username or parsed.password:
        raise ExtractionError("URL validation", "the link must not contain credentials")
    if (hostname or "").lower() != ALLOWED_HOST:
        raise ExtractionError("URL validation", f"only {ALLOWED_HOST} links are supported")
    if port not in (None, 443):
        raise ExtractionError("URL validation", "non-default ports are not supported")


def validate_share_url(url: str) -> str:
    """Validate and normalize a public chatgpt.com share URL."""
    _validate_remote_target(url)
    parsed = urlsplit(url)
    if not re.fullmatch(r"/share/[^/]+/?", parsed.path):
        raise ExtractionError(
            "URL validation", "expected a chatgpt.com/share/<id> link"
        )
    return urlunsplit(("https", ALLOWED_HOST, parsed.path, parsed.query, ""))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_remote_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str) -> str:
    """Fetch a validated public share page with bounded memory use."""
    normalized = validate_share_url(url)
    request = urllib.request.Request(normalized, headers={"User-Agent": DEFAULT_UA})
    opener = urllib.request.build_opener(_SafeRedirectHandler())

    try:
        with opener.open(request, timeout=30) as response:
            validate_share_url(response.geturl())
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ExtractionError(
                    "fetch", f"unexpected response content type: {content_type}"
                )
            body = response.read(MAX_INPUT_BYTES + 1)
            if len(body) > MAX_INPUT_BYTES:
                raise ExtractionError(
                    "fetch", f"response exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB limit"
                )
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace")
    except ExtractionError:
        raise
    except urllib.error.HTTPError as exc:
        raise ExtractionError("fetch", f"server returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else "network error"
        raise ExtractionError("fetch", f"network request failed ({reason_name})") from exc
    except TimeoutError as exc:
        raise ExtractionError("fetch", "network request timed out") from exc


def read_html_file(path: str | Path) -> str:
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise ExtractionError(
                "file input", f"file exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB limit"
            )
        return file_path.read_text(encoding="utf-8-sig", errors="replace")
    except ExtractionError:
        raise
    except OSError as exc:
        raise ExtractionError("file input", "could not read the HTML file") from exc


def extract_enqueue_payloads(html: str) -> list[str]:
    """Return double-quoted React Router enqueue payloads in document order."""
    return ENQUEUE_RE.findall(html)


def decode_payload(raw: str):
    """Decode the JavaScript string literal and its JSON payload."""
    try:
        unescaped = json.loads('"' + raw + '"')
        return json.loads(unescaped)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionError("payload decoding", "invalid serialized JSON") from exc


def _is_reference(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def resolve(data):
    """Resolve the flattened React Router reference serialization."""
    if not isinstance(data, list) or not data:
        raise ExtractionError("payload decoding", "the serialized root is not a non-empty array")

    cache = {}
    visiting = set()

    def walk(reference):
        if _is_reference(reference) and reference < 0:
            return None
        if not _is_reference(reference):
            return reference
        if reference < 0 or reference >= len(data):
            raise ExtractionError(
                "payload decoding", f"reference index {reference} is outside the payload"
            )
        if reference in cache:
            return cache[reference]
        if reference in visiting:
            raise ExtractionError("payload decoding", "cyclic reference detected")

        visiting.add(reference)
        try:
            value = data[reference]
            if isinstance(value, dict):
                output = {}
                for encoded_key, child in value.items():
                    match = re.fullmatch(r"_(\d+)", encoded_key)
                    if not match:
                        raise ExtractionError(
                            "payload decoding", "encountered an invalid object-key reference"
                        )
                    key_index = int(match.group(1))
                    if key_index >= len(data) or not isinstance(data[key_index], str):
                        raise ExtractionError(
                            "payload decoding", "object-key reference does not point to a string"
                        )
                    output[data[key_index]] = walk(child)
            elif isinstance(value, list):
                if (
                    len(value) == 2
                    and value[0] == "P"
                    and _is_reference(value[1])
                ):
                    output = walk(value[1])
                else:
                    output = [walk(item) if _is_reference(item) else item for item in value]
            else:
                output = value
            cache[reference] = output
            return output
        finally:
            visiting.discard(reference)

    return walk(0)


def _known_conversation_data(root):
    if not isinstance(root, dict):
        return None
    loader_data = root.get("loaderData")
    if not isinstance(loader_data, dict):
        return None
    route = loader_data.get("routes/share.$shareId.($action)")
    if not isinstance(route, dict):
        return None
    response = route.get("serverResponse")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    return data if isinstance(data, dict) else None


def _conversation_score(value) -> int:
    if not isinstance(value, dict) or not isinstance(value.get("mapping"), dict):
        return 0
    score = 1
    linear = value.get("linear_conversation")
    if isinstance(linear, list):
        score += 3
    mapping = value["mapping"]
    if any(isinstance(node, dict) and "message" in node for node in mapping.values()):
        score += 2
    return score


def find_conversation_data(root):
    """Find conversation data by known path, then by semantic structure."""
    known = _known_conversation_data(root)
    if _conversation_score(known):
        return known

    best = None
    best_score = 0
    stack = [root]
    seen = set()
    inspected = 0
    while stack and inspected < 100_000:
        current = stack.pop()
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
        inspected += 1

        score = _conversation_score(current)
        if score > best_score:
            best = current
            best_score = score
            if score >= 6:
                break

        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return best


def extract_messages_from_data(data) -> list[tuple[str, str]]:
    mapping = data.get("mapping") or {}
    if not isinstance(mapping, dict):
        raise ExtractionError("message extraction", "mapping is not an object")

    linear = data.get("linear_conversation")
    if linear is None:
        linear = list(mapping.values())
    if not isinstance(linear, list):
        raise ExtractionError("message extraction", "linear_conversation is not a list")

    messages = []
    for entry in linear:
        node = mapping.get(entry) if isinstance(entry, str) else entry
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get(
            "is_visually_hidden_from_conversation"
        ):
            continue

        content = message.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        text = "".join(
            part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
            for part in parts
        ).strip()
        if not text:
            continue
        author = message.get("author") or {}
        role = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
        messages.append((role, text))
    return messages


def extract_messages(html: str) -> list[tuple[str, str]]:
    """Extract visible non-empty messages from the first semantic conversation payload."""
    payloads = extract_enqueue_payloads(html)
    if not payloads:
        raise ExtractionError(
            "page recognition",
            "no React Router enqueue payloads were found; the input may be an error, login, or changed share page",
        )

    decoded_count = 0
    conversation_count = 0
    for raw in payloads:
        try:
            root = resolve(decode_payload(raw))
        except ExtractionError:
            continue
        decoded_count += 1
        conversation_data = find_conversation_data(root)
        if conversation_data is None:
            continue
        conversation_count += 1
        messages = extract_messages_from_data(conversation_data)
        if messages:
            return messages

    if conversation_count:
        raise ExtractionError(
            "message extraction", "the share contains no visible non-empty messages"
        )
    if decoded_count:
        raise ExtractionError(
            "conversation discovery",
            f"decoded {decoded_count} payload(s), but none contained a recognizable conversation",
        )
    raise ExtractionError(
        "payload decoding",
        f"found {len(payloads)} enqueue payload(s), but none could be decoded",
    )


def to_markdown(messages, roles: bool = True, bold: bool = False) -> str:
    role_names = {
        "user": "用户",
        "assistant": "ChatGPT",
        "system": "系统",
        "tool": "工具",
    }
    output = []
    for role, message_text in messages:
        if roles:
            name = role_names.get(role, role)
            label = ("**%s**：" if bold else "%s：") % name
            output.append(label + "\n" + message_text + "\n")
        else:
            output.append(message_text + "\n")
    return "\n".join(output).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="提取公开 ChatGPT 分享对话并输出为 Markdown"
    )
    parser.add_argument("input", help="https://chatgpt.com/share/... 链接或已保存的 HTML 文件")
    parser.add_argument("-o", "--output", help="输出 .md 路径；默认打印到标准输出")
    parser.add_argument("--no-roles", action="store_true", help="不输出说话人标记")
    parser.add_argument("--bold-roles", action="store_true", help="加粗说话人标记")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    source = args.input

    try:
        if "://" in source:
            html = fetch_html(source)
        else:
            html = read_html_file(source)
        messages = extract_messages(html)
        markdown = to_markdown(
            messages, roles=not args.no_roles, bold=args.bold_roles
        )
        if args.output:
            try:
                Path(args.output).write_text(markdown, encoding="utf-8")
            except OSError as exc:
                raise ExtractionError("output", "could not write the Markdown file") from exc
            print(f"已写入 {args.output}（共 {len(messages)} 条消息）")
        else:
            sys.stdout.write(markdown)
        return 0
    except ExtractionError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        if exc.stage == "fetch":
            print(POWERSHELL_HINT, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
