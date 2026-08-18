#!/usr/bin/env python3
"""Extract visible messages from a public ChatGPT share page as Markdown."""

import argparse
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ALLOWED_HOST = "chatgpt.com"
MAX_INPUT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ASSET_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_ASSET_BYTES = 100 * 1024 * 1024
DEFAULT_ASSET_HOSTS = (
    "chatgpt.com",
    "openai.com",
    "oaiusercontent.com",
    "oaistatic.com",
)
ENQUEUE_RE = re.compile(
    r'(?:window\.__reactRouterContext\.streamController\.)?'
    r'enqueue\(\s*"((?:[^"\\]|\\.)*)"\s*\)'
)
CONTROL_FRAME_RE = re.compile(r"^([A-Z])(\d+):(.*)\s*$", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(
    r'(!?)\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\s*\)'
)
ARCHIVABLE_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".m4a",
    ".md",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}

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


@dataclass
class AssetReference:
    """One visible image, audio item, or file referenced by a message."""

    kind: str
    filename: str
    message_index: int
    part_index: int
    url: str | None = None
    pointer: str | None = None
    mime_type: str | None = None
    alt_text: str = ""
    render_inline: bool = True
    local_path: Path | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    error: str | None = None


@dataclass
class RichMessage:
    """A visible message whose parts retain their public content types."""

    role: str
    parts: list[str | AssetReference] = field(default_factory=list)

    @property
    def assets(self) -> list[AssetReference]:
        return [part for part in self.parts if isinstance(part, AssetReference)]


@dataclass
class ControlFrame:
    """A structurally decoded React Router stream control frame."""

    kind: str
    target: int
    value: object


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


def decode_javascript_string(raw: str) -> str:
    """Decode one double-quoted JavaScript string without exposing its content."""
    try:
        decoded = json.loads('"' + raw + '"')
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionError(
            "JavaScript decoding", "invalid enqueue string escaping"
        ) from exc
    if not isinstance(decoded, str):
        raise ExtractionError(
            "JavaScript decoding", "the enqueue argument did not decode to text"
        )
    return decoded


def decode_serialized_enqueue(serialized: str):
    """Decode a JSON root or a typed React Router stream control frame."""
    try:
        return "json", json.loads(serialized)
    except json.JSONDecodeError as json_error:
        match = CONTROL_FRAME_RE.fullmatch(serialized)
        if not match:
            raise ExtractionError(
                "JSON decoding", "enqueue text is neither JSON nor a control frame"
            ) from json_error
        kind, target_text, body = match.groups()
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "control frame decoding", "control frame body is not valid JSON"
            ) from exc
        return "control", ControlFrame(kind, int(target_text), value)


def decode_enqueue_payload(raw: str):
    return decode_serialized_enqueue(decode_javascript_string(raw))


def decode_payload(raw: str):
    """Decode a legacy JSON enqueue payload while rejecting control frames."""
    kind, value = decode_enqueue_payload(raw)
    if kind != "json":
        raise ExtractionError("JSON decoding", "enqueue payload is a control frame")
    return value


def _is_reference(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def resolve(data, promise_values=None, stats=None):
    """Resolve a flattened reference graph, preserving legitimate back-references."""
    if not isinstance(data, list) or not data:
        raise ExtractionError(
            "reference resolution", "the serialized root is not a non-empty array"
        )

    promise_values = promise_values or {}
    stats = stats if stats is not None else {}
    for key in (
        "resolved_references",
        "container_back_references",
        "promise_markers",
        "resolved_promises",
        "unresolved_promises",
    ):
        stats.setdefault(key, 0)
    cache = {}
    active_containers = set()
    active_aliases = set()

    def walk(reference):
        if _is_reference(reference) and reference < 0:
            return None
        if not _is_reference(reference):
            return reference
        if reference < 0 or reference >= len(data):
            raise ExtractionError(
                "reference resolution",
                f"reference index {reference} is outside the payload",
            )
        if reference in cache:
            if reference in active_containers:
                stats["container_back_references"] += 1
            return cache[reference]
        if reference in active_aliases:
            raise ExtractionError(
                "reference resolution", "promise aliases contain an unresolvable cycle"
            )

        stats["resolved_references"] += 1
        value = data[reference]
        if isinstance(value, dict):
            output = {}
            cache[reference] = output
            active_containers.add(reference)
            try:
                for encoded_key, child in value.items():
                    match = re.fullmatch(r"_(\d+)", encoded_key)
                    if not match:
                        raise ExtractionError(
                            "reference resolution",
                            "encountered an invalid object-key reference",
                        )
                    key_index = int(match.group(1))
                    if key_index >= len(data) or not isinstance(data[key_index], str):
                        raise ExtractionError(
                            "reference resolution",
                            "object-key reference does not point to a string",
                        )
                    output[data[key_index]] = walk(child)
            except Exception:
                cache.pop(reference, None)
                raise
            finally:
                active_containers.discard(reference)
            return output

        if isinstance(value, list):
            if len(value) == 2 and value[0] == "P" and _is_reference(value[1]):
                stats["promise_markers"] += 1
                target = value[1]
                if target in promise_values:
                    output = promise_values[target]
                    stats["resolved_promises"] += 1
                elif target == reference:
                    output = None
                    stats["unresolved_promises"] += 1
                else:
                    active_aliases.add(reference)
                    try:
                        output = walk(target)
                    finally:
                        active_aliases.discard(reference)
                cache[reference] = output
                return output

            output = []
            cache[reference] = output
            active_containers.add(reference)
            try:
                output.extend(
                    walk(item) if _is_reference(item) else item for item in value
                )
            except Exception:
                cache.pop(reference, None)
                raise
            finally:
                active_containers.discard(reference)
            return output

        cache[reference] = value
        return value

    return walk(0)


def _empty_stream_report(payload_count):
    return {
        "enqueue_payloads": payload_count,
        "javascript_strings_decoded": 0,
        "javascript_decoding_failures": 0,
        "json_values": 0,
        "json_decoding_failures": 0,
        "control_frames": 0,
        "control_frame_kinds": {},
        "control_frame_decoding_failures": 0,
        "promise_frames": 0,
        "promise_frames_resolved": 0,
        "promise_frame_resolution_failures": 0,
        "root_candidates": 0,
        "root_array_sizes": [],
        "resolved_roots": 0,
        "reference_resolution_failures": 0,
        "resolved_references": 0,
        "container_back_references": 0,
        "promise_markers": 0,
        "resolved_promises": 0,
        "unresolved_promises": 0,
    }


def _resolved_payload_stream(html: str):
    payloads = extract_enqueue_payloads(html)
    report = _empty_stream_report(len(payloads))
    json_values = []
    control_frames = []

    for raw in payloads:
        try:
            serialized = decode_javascript_string(raw)
            report["javascript_strings_decoded"] += 1
        except ExtractionError:
            report["javascript_decoding_failures"] += 1
            continue
        try:
            kind, value = decode_serialized_enqueue(serialized)
        except ExtractionError as exc:
            if exc.stage == "control frame decoding":
                report["control_frame_decoding_failures"] += 1
            else:
                report["json_decoding_failures"] += 1
            continue
        if kind == "control":
            control_frames.append(value)
            report["control_frames"] += 1
            kinds = report["control_frame_kinds"]
            kinds[value.kind] = kinds.get(value.kind, 0) + 1
        else:
            json_values.append(value)
            report["json_values"] += 1

    promise_values = {}
    for frame in control_frames:
        if frame.kind != "P":
            continue
        report["promise_frames"] += 1
        try:
            value = (
                resolve(frame.value, promise_values=promise_values)
                if isinstance(frame.value, list)
                else frame.value
            )
            promise_values[frame.target] = value
            report["promise_frames_resolved"] += 1
        except ExtractionError:
            report["promise_frame_resolution_failures"] += 1

    roots = []
    for value in json_values:
        if not isinstance(value, list) or not value:
            continue
        report["root_candidates"] += 1
        report["root_array_sizes"].append(len(value))
        resolve_stats = {}
        try:
            root = resolve(
                value, promise_values=promise_values, stats=resolve_stats
            )
        except ExtractionError:
            report["reference_resolution_failures"] += 1
            continue
        roots.append(root)
        report["resolved_roots"] += 1
        for key in (
            "resolved_references",
            "container_back_references",
            "promise_markers",
            "resolved_promises",
            "unresolved_promises",
        ):
            report[key] += resolve_stats.get(key, 0)
    return roots, report


def _raise_payload_stream_failure(report):
    if report["enqueue_payloads"] == 0:
        raise ExtractionError(
            "page recognition",
            "no React Router enqueue payloads were found; the input may be an error, login, or changed share page",
        )
    if report["javascript_strings_decoded"] == 0:
        raise ExtractionError(
            "JavaScript decoding",
            f"none of {report['enqueue_payloads']} enqueue string(s) could be decoded",
        )
    if report["json_values"] == 0:
        if (
            report["control_frame_decoding_failures"]
            and report["json_decoding_failures"] == 0
        ):
            raise ExtractionError(
                "control frame decoding",
                "typed stream frames were found but their JSON bodies were invalid",
            )
        raise ExtractionError(
            "JSON decoding",
            "no serialized JSON root was found among the decoded enqueue values",
        )
    if report["root_candidates"] == 0:
        raise ExtractionError(
            "reference resolution", "no non-empty flattened root array was found"
        )
    raise ExtractionError(
        "reference resolution",
        f"none of {report['root_candidates']} flattened root candidate(s) could be resolved",
    )


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


def _ordered_conversation_nodes(data, mapping) -> list[dict]:
    """Return the explicit linear order or reconstruct the selected parent chain."""
    linear = data.get("linear_conversation")
    if linear is not None:
        if not isinstance(linear, list):
            raise ExtractionError(
                "message extraction", "linear_conversation is not a list"
            )
        return [
            mapping.get(entry) if isinstance(entry, str) else entry
            for entry in linear
            if isinstance(mapping.get(entry) if isinstance(entry, str) else entry, dict)
        ]

    def path_to(entry) -> list[dict]:
        path = []
        seen = set()
        current = entry
        while current is not None:
            if isinstance(current, str):
                if current in seen:
                    raise ExtractionError(
                        "message extraction", "conversation parent chain contains a cycle"
                    )
                seen.add(current)
                node = mapping.get(current)
            else:
                node = current
            if not isinstance(node, dict):
                break
            path.append(node)
            current = node.get("parent")
            if isinstance(current, dict):
                identity = id(current)
                if identity in seen:
                    raise ExtractionError(
                        "message extraction", "conversation parent chain contains a cycle"
                    )
                seen.add(identity)
        path.reverse()
        return path

    current = data.get("current_node") or data.get("current_node_id")
    if isinstance(current, (str, dict)):
        path = path_to(current)
        if path:
            return path

    candidates = []
    has_parent_links = False
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        if node.get("parent") is not None:
            has_parent_links = True
        candidates.append((len(path_to(node_id)), node_id))
    if has_parent_links and candidates:
        _, deepest = max(candidates, key=lambda item: item[0])
        return path_to(deepest)

    raise ExtractionError(
        "message extraction",
        "could not determine conversation order without linear_conversation or parent links",
    )


def _first_nested_string(value, keys, max_depth=6):
    stack = [(value, 0)]
    seen = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth or not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, dict):
            for key in keys:
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            stack.extend((child, depth + 1) for child in current.values())
        else:
            stack.extend((child, depth + 1) for child in current)
    return None


def _contains_nested_key(value, keys, max_depth=5):
    stack = [(value, 0)]
    seen = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth or not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, dict):
            if keys.intersection(current):
                return True
            stack.extend((child, depth + 1) for child in current.values())
        else:
            stack.extend((child, depth + 1) for child in current)
    return False


def _guess_asset_kind(content_type, mime_type, filename, url):
    hints = " ".join(
        value.lower()
        for value in (content_type, mime_type, filename, url)
        if isinstance(value, str)
    )
    if "image" in hints or re.search(r"\.(png|jpe?g|gif|webp|svg)(?:$|[?#])", hints):
        return "image"
    if "audio" in hints or re.search(r"\.(mp3|wav|m4a|ogg|flac)(?:$|[?#])", hints):
        return "audio"
    if "video" in hints or re.search(r"\.(mp4|mov|webm)(?:$|[?#])", hints):
        return "video"
    return "file"


def _filename_from_source(source):
    if not isinstance(source, str) or not source:
        return None
    parsed = urlsplit(source)
    candidate = unquote(Path(parsed.path).name)
    return candidate or None


def _asset_from_mapping(value, message_index, part_index, force=False):
    if not isinstance(value, dict):
        return None
    content_type = _first_nested_string(value, ("content_type", "type"), 2)
    mime_type = _first_nested_string(
        value, ("mime_type", "mime", "content_type_mime"), 3
    )
    if not mime_type and isinstance(content_type, str) and "/" in content_type:
        mime_type = content_type
    url = _first_nested_string(
        value,
        ("download_url", "asset_url", "image_url", "audio_url", "src", "url"),
        5,
    )
    pointer = _first_nested_string(
        value, ("asset_pointer", "file_pointer", "sandbox_path"), 4
    )
    filename = _first_nested_string(
        value, ("filename", "file_name", "name", "title"), 4
    )
    asset_keys = {
        "asset_pointer",
        "file_pointer",
        "download_url",
        "asset_url",
        "image_url",
        "audio_url",
        "filename",
        "file_name",
        "mime_type",
    }
    type_hint = (content_type or "").lower()
    has_asset_type = any(
        token in type_hint for token in ("image", "audio", "video", "file", "attachment")
    )
    if not (force or has_asset_type or _contains_nested_key(value, asset_keys)):
        return None

    if isinstance(url, str) and urlsplit(url).scheme.lower() not in {"http", "https"}:
        pointer = pointer or url
        url = None
    filename = filename or _filename_from_source(url) or _filename_from_source(pointer)
    kind = _guess_asset_kind(content_type, mime_type, filename, url)
    if not filename:
        guessed_extension = mimetypes.guess_extension(mime_type or "") or ""
        filename = f"{kind}-{message_index + 1:03d}-{part_index + 1:02d}{guessed_extension}"
    alt_text = _first_nested_string(value, ("alt_text", "alt", "caption"), 3)
    return AssetReference(
        kind=kind,
        filename=filename,
        message_index=message_index,
        part_index=part_index,
        url=url,
        pointer=pointer,
        mime_type=mime_type,
        alt_text=alt_text or filename,
    )


def _structured_text(value):
    if not isinstance(value, dict):
        return None
    for key in ("text", "caption", "transcript"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    content = value.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return None


def _markdown_asset_references(text, message_index, start_index):
    for offset, match in enumerate(MARKDOWN_LINK_RE.finditer(text)):
        image_marker, label, angle_target, plain_target = match.groups()
        target = angle_target or plain_target
        parsed = urlsplit(target)
        suffix = Path(parsed.path).suffix.lower()
        if not image_marker and not (
            parsed.scheme.lower() == "sandbox" or suffix in ARCHIVABLE_EXTENSIONS
        ):
            continue
        url = target if parsed.scheme.lower() in {"http", "https"} else None
        pointer = target if url is None else None
        filename = _filename_from_source(target) or label or "asset"
        kind = "image" if image_marker else _guess_asset_kind(None, None, filename, url)
        yield AssetReference(
            kind=kind,
            filename=filename,
            message_index=message_index,
            part_index=start_index + offset,
            url=url,
            pointer=pointer,
            alt_text=label or filename,
            render_inline=False,
        )


def _iter_attachment_mappings(message, content, metadata):
    seen = set()
    for container in (message, content, metadata):
        if not isinstance(container, dict):
            continue
        for key in ("attachments", "files", "images", "audio", "videos"):
            values = container.get(key)
            if isinstance(values, dict):
                if any(
                    marker in values
                    for marker in (
                        "url",
                        "download_url",
                        "asset_pointer",
                        "filename",
                        "file_name",
                    )
                ):
                    values = [values]
                else:
                    values = list(values.values())
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict) and id(value) not in seen:
                    seen.add(id(value))
                    yield value


def _asset_identity(asset):
    if asset.pointer:
        return ("pointer", asset.pointer)
    if asset.url:
        return ("url", asset.url)
    return ("file", asset.filename.casefold(), asset.mime_type or "")


def _message_is_visible(message, include_non_chat_roles=False):
    metadata = message.get("metadata") or {}
    if isinstance(metadata, dict) and any(
        metadata.get(key) is True
        for key in (
            "is_visually_hidden_from_conversation",
            "is_hidden",
            "hidden",
            "hide_in_conversation",
            "is_context_message",
        )
    ):
        return False
    author = message.get("author") or {}
    role = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
    return include_non_chat_roles or role in {"user", "assistant"}


def extract_rich_messages_from_data(
    data, include_non_chat_roles=False
) -> list[RichMessage]:
    mapping = data.get("mapping") or {}
    if not isinstance(mapping, dict):
        raise ExtractionError("message extraction", "mapping is not an object")
    ordered_nodes = _ordered_conversation_nodes(data, mapping)

    messages = []
    for node in ordered_nodes:
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict) or not _message_is_visible(
            message, include_non_chat_roles=include_non_chat_roles
        ):
            continue
        author = message.get("author") or {}
        role = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
        message_index = len(messages)
        content = message.get("content") or {}
        metadata = message.get("metadata") or {}
        raw_parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(raw_parts, list):
            raw_parts = []
            direct_text = content.get("text") if isinstance(content, dict) else None
            if isinstance(direct_text, str):
                raw_parts.append(direct_text)

        rendered_parts = []
        known_assets = set()
        text_sources = []
        markdown_assets = []
        for part_index, part in enumerate(raw_parts):
            if isinstance(part, str):
                if part.strip():
                    rendered_parts.append(part)
                    text_sources.append(part)
                    for asset in _markdown_asset_references(part, message_index, 0):
                        asset.part_index = len(raw_parts) + len(markdown_assets)
                        markdown_assets.append(asset)
                continue
            asset = _asset_from_mapping(part, message_index, part_index)
            if asset is not None:
                identity = _asset_identity(asset)
                if identity not in known_assets:
                    known_assets.add(identity)
                    rendered_parts.append(asset)
                continue
            text = _structured_text(part)
            if text:
                rendered_parts.append(text)
                text_sources.append(text)

        visible_text = "\n".join(text_sources)
        for asset in markdown_assets:
            identity = _asset_identity(asset)
            if identity not in known_assets:
                known_assets.add(identity)
                rendered_parts.append(asset)
        next_part_index = len(raw_parts) + len(markdown_assets)
        for attachment in _iter_attachment_mappings(message, content, metadata):
            asset = _asset_from_mapping(
                attachment, message_index, next_part_index, force=True
            )
            next_part_index += 1
            if asset is None or _asset_identity(asset) in known_assets:
                continue
            known_assets.add(_asset_identity(asset))
            sources = tuple(
                source for source in (asset.url, asset.pointer) if isinstance(source, str)
            )
            sandbox_source = f"sandbox:/mnt/data/{asset.filename}"
            asset.render_inline = not any(source in visible_text for source in sources) and (
                sandbox_source not in visible_text
            )
            rendered_parts.append(asset)

        if rendered_parts:
            messages.append(RichMessage(role=role, parts=rendered_parts))
    return messages


def _extract_rich_messages(html: str, include_non_chat_roles=False):
    roots, report = _resolved_payload_stream(html)
    if not roots:
        _raise_payload_stream_failure(report)
    conversation_count = 0
    last_message_error = None
    for root in roots:
        conversation_data = find_conversation_data(root)
        if conversation_data is None:
            continue
        conversation_count += 1
        try:
            messages = extract_rich_messages_from_data(
                conversation_data, include_non_chat_roles=include_non_chat_roles
            )
        except ExtractionError as exc:
            last_message_error = exc
            continue
        if messages:
            return messages

    if conversation_count:
        if last_message_error is not None:
            raise last_message_error
        raise ExtractionError(
            "message extraction", "the share contains no visible non-empty messages"
        )
    raise ExtractionError(
        "conversation discovery",
        f"resolved {len(roots)} root graph(s), but none contained a recognizable conversation",
    )


def diagnose_html(html: str, include_non_chat_roles=False):
    """Return structural diagnostics without URLs, payload text, or message content."""
    roots, report = _resolved_payload_stream(html)
    report.update(
        {
            "conversation_candidates": 0,
            "message_extraction_failures": 0,
            "visible_messages": 0,
            "asset_references": 0,
            "status": "error",
            "failure_stage": None,
        }
    )
    if not roots:
        try:
            _raise_payload_stream_failure(report)
        except ExtractionError as exc:
            report["failure_stage"] = exc.stage
        return report

    for root in roots:
        conversation_data = find_conversation_data(root)
        if conversation_data is None:
            continue
        report["conversation_candidates"] += 1
        try:
            messages = extract_rich_messages_from_data(
                conversation_data, include_non_chat_roles=include_non_chat_roles
            )
        except ExtractionError:
            report["message_extraction_failures"] += 1
            continue
        if messages and report["visible_messages"] == 0:
            report["visible_messages"] = len(messages)
            report["asset_references"] = sum(
                len(message.assets) for message in messages
            )

    if report["visible_messages"]:
        report["status"] = "ok"
    elif report["conversation_candidates"]:
        report["failure_stage"] = "message extraction"
    else:
        report["failure_stage"] = "conversation discovery"
    return report


def _relative_markdown_path(path, output_parent):
    return Path(os.path.relpath(path, output_parent)).as_posix()


def _markdown_target(value):
    return f"<{str(value).replace('>', '%3E')}>"


def _render_asset(asset, output_parent=None, assets_requested=False):
    label = (asset.alt_text or asset.filename).replace("[", "\\[").replace(
        "]", "\\]"
    )
    if asset.local_path is not None and output_parent is not None:
        target = _relative_markdown_path(asset.local_path, output_parent)
    elif asset.url and not assets_requested:
        target = asset.url
    elif asset.pointer and asset.pointer.startswith("sandbox:") and not assets_requested:
        target = asset.pointer
    else:
        reason = asset.error or "公开页面未提供可下载 URL"
        return f"[附件未归档：{asset.filename}（{reason}）]"
    if asset.kind == "image":
        return f"![{label}]({_markdown_target(target)})"
    return f"[{label}]({_markdown_target(target)})"


def render_rich_message(message, output_parent=None, assets_requested=False):
    replacements = []
    for asset in message.assets:
        if asset.local_path is None or output_parent is None:
            continue
        target = _relative_markdown_path(asset.local_path, output_parent)
        for source in (
            asset.url,
            asset.pointer,
            f"sandbox:/mnt/data/{asset.filename}",
        ):
            if isinstance(source, str) and source:
                replacements.append((source, target))

    chunks = []
    for part in message.parts:
        if isinstance(part, str):
            text = part
            for source, target in replacements:
                text = text.replace(source, target)
            if text.strip():
                chunks.append(text.strip())
        elif part.render_inline:
            chunks.append(
                _render_asset(
                    part,
                    output_parent=output_parent,
                    assets_requested=assets_requested,
                )
            )
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def extract_messages_from_data(data, include_non_chat_roles=False) -> list[tuple[str, str]]:
    rich_messages = extract_rich_messages_from_data(
        data, include_non_chat_roles=include_non_chat_roles
    )
    return [
        (message.role, render_rich_message(message))
        for message in rich_messages
        if render_rich_message(message)
    ]


def extract_messages(html: str, include_non_chat_roles=False) -> list[tuple[str, str]]:
    """Extract visible non-empty messages while keeping the legacy tuple API."""
    rich_messages = _extract_rich_messages(
        html, include_non_chat_roles=include_non_chat_roles
    )
    return [
        (message.role, render_rich_message(message))
        for message in rich_messages
        if render_rich_message(message)
    ]


def _host_is_allowed(hostname, allowed_hosts):
    host = (hostname or "").lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _normalized_asset_hosts(additional_hosts):
    hosts = list(DEFAULT_ASSET_HOSTS)
    for raw_host in additional_hosts:
        host = (raw_host or "").strip().lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
            raise ExtractionError("asset download", "an additional asset host is invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ExtractionError(
                "asset download", "literal IP addresses cannot be added as asset hosts"
            )
        if "." not in host:
            raise ExtractionError(
                "asset download", "an additional asset host must be a full domain name"
            )
        hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def _validate_asset_target(url, allowed_hosts):
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ExtractionError("asset download", "asset URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise ExtractionError("asset download", "asset URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ExtractionError("asset download", "asset URL must not contain credentials")
    if port not in (None, 443):
        raise ExtractionError("asset download", "asset URL uses a non-default port")
    if not _host_is_allowed(hostname, allowed_hosts):
        raise ExtractionError(
            "asset download", f"asset host is not allowed: {hostname or 'missing host'}"
        )


class _SafeAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_asset_target(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_asset(url, allowed_hosts):
    _validate_asset_target(url, allowed_hosts)
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    opener = urllib.request.build_opener(_SafeAssetRedirectHandler(allowed_hosts))
    return opener.open(request, timeout=30)


def _safe_asset_filename(name, mime_type=None):
    candidate = unquote(Path(name or "asset").name)
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate).strip(" .")
    if not candidate:
        candidate = "asset"
    stem = Path(candidate).stem[:100] or "asset"
    suffix = Path(candidate).suffix[:20]
    if not suffix and mime_type:
        suffix = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip()) or ""
    if stem.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        stem = "_" + stem
    return stem + suffix


def _unique_destination(directory, filename):
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
        counter += 1
    return candidate


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_asset_result(source, target):
    target.local_path = source.local_path
    target.sha256 = source.sha256
    target.size_bytes = source.size_bytes
    target.mime_type = source.mime_type
    target.error = source.error


def download_asset_references(
    messages,
    output_path,
    assets_dir=None,
    additional_hosts=(),
    max_asset_bytes=DEFAULT_MAX_ASSET_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_ASSET_BYTES,
    strict=False,
):
    assets = [asset for message in messages for asset in message.assets]
    if not assets:
        return 0, 0, None

    output_path = Path(output_path)
    output_parent = output_path.resolve().parent
    directory = Path(assets_dir).resolve() if assets_dir else output_parent / "assets"
    directory.mkdir(parents=True, exist_ok=True)
    allowed_hosts = _normalized_asset_hosts(additional_hosts)

    source_cache = {}
    hash_cache = {}
    downloaded_total = 0
    for asset in assets:
        cache_key = asset.url or asset.pointer or _asset_identity(asset)
        if cache_key in source_cache:
            _copy_asset_result(source_cache[cache_key], asset)
            continue
        source_cache[cache_key] = asset
        if not asset.url:
            asset.error = "公开页面未提供 HTTPS 下载 URL"
            continue

        temporary_path = None
        try:
            with _open_asset(asset.url, allowed_hosts) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else asset.url
                _validate_asset_target(final_url, allowed_hosts)
                headers = response.headers
                response_mime = (
                    headers.get_content_type()
                    if hasattr(headers, "get_content_type")
                    else headers.get("Content-Type", "").split(";", 1)[0]
                )
                if response_mime in {"text/html", "application/xhtml+xml"}:
                    raise ExtractionError(
                        "asset download", "asset response was an HTML page"
                    )
                declared_length = headers.get("Content-Length")
                if declared_length and int(declared_length) > max_asset_bytes:
                    raise ExtractionError(
                        "asset download", "asset exceeds the per-file size limit"
                    )
                header_name = (
                    headers.get_filename() if hasattr(headers, "get_filename") else None
                )
                asset.mime_type = response_mime or asset.mime_type
                filename = _safe_asset_filename(
                    header_name or asset.filename, asset.mime_type
                )
                handle, temporary_name = tempfile.mkstemp(
                    prefix=".asset-", dir=directory
                )
                os.close(handle)
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                asset_size = 0
                with temporary_path.open("wb") as output:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        asset_size += len(chunk)
                        downloaded_total += len(chunk)
                        if asset_size > max_asset_bytes:
                            raise ExtractionError(
                                "asset download", "asset exceeds the per-file size limit"
                            )
                        if downloaded_total > max_total_bytes:
                            raise ExtractionError(
                                "asset download", "assets exceed the total size limit"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                sha256 = digest.hexdigest()
                if sha256 in hash_cache:
                    temporary_path.unlink(missing_ok=True)
                    asset.local_path = hash_cache[sha256]
                else:
                    preferred = directory / filename
                    if preferred.is_file() and _sha256_file(preferred) == sha256:
                        temporary_path.unlink(missing_ok=True)
                        asset.local_path = preferred.resolve()
                    else:
                        destination = _unique_destination(directory, filename)
                        temporary_path.replace(destination)
                        asset.local_path = destination.resolve()
                    hash_cache[sha256] = asset.local_path
                asset.sha256 = sha256
                asset.size_bytes = asset_size
        except (ExtractionError, urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            asset.error = exc.message if isinstance(exc, ExtractionError) else type(exc).__name__

    manifest_assets = []
    for asset in assets:
        manifest_assets.append(
            {
                "message_index": asset.message_index,
                "part_index": asset.part_index,
                "kind": asset.kind,
                "original_filename": asset.filename,
                "local_path": (
                    _relative_markdown_path(asset.local_path, output_parent)
                    if asset.local_path
                    else None
                ),
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "sha256": asset.sha256,
                "status": "downloaded" if asset.local_path else "unavailable",
                "error": asset.error,
            }
        )
    manifest_path = directory / "assets.json"
    manifest_path.write_text(
        json.dumps({"version": 1, "assets": manifest_assets}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    downloaded = len({asset.local_path for asset in assets if asset.local_path})
    failures = len(assets) - downloaded
    if strict and failures:
        raise ExtractionError(
            "asset download", f"{failures} referenced asset(s) could not be archived"
        )
    return downloaded, failures, manifest_path


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


def rich_to_markdown(
    messages,
    roles=True,
    bold=False,
    output_parent=None,
    assets_requested=False,
):
    rendered = [
        (
            message.role,
            render_rich_message(
                message,
                output_parent=output_parent,
                assets_requested=assets_requested,
            ),
        )
        for message in messages
    ]
    return to_markdown(
        [(role, text) for role, text in rendered if text], roles=roles, bold=bold
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="提取公开 ChatGPT 分享对话并输出为 Markdown"
    )
    parser.add_argument("input", help="https://chatgpt.com/share/... 链接或已保存的 HTML 文件")
    parser.add_argument("-o", "--output", help="输出 .md 路径；默认打印到标准输出")
    summary_group = parser.add_mutually_exclusive_group()
    summary_group.add_argument(
        "--json-summary",
        action="store_true",
        help="以单行 JSON 输出成功或失败摘要；需要同时指定 -o",
    )
    summary_group.add_argument(
        "--quiet",
        action="store_true",
        help="不输出成功提示；错误仍写入标准错误；需要同时指定 -o",
    )
    parser.add_argument("--no-roles", action="store_true", help="不输出说话人标记")
    parser.add_argument("--bold-roles", action="store_true", help="加粗说话人标记")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="仅输出不含链接、载荷和正文的结构诊断",
    )
    parser.add_argument(
        "--download-assets",
        action="store_true",
        help="下载公开页面引用的图片、音频和文件",
    )
    parser.add_argument(
        "--assets-dir",
        help="附件目录；默认在 Markdown 同级创建 assets 文件夹",
    )
    parser.add_argument(
        "--asset-host",
        action="append",
        default=[],
        help="额外允许下载的 HTTPS 资源域名；可重复指定",
    )
    parser.add_argument(
        "--max-asset-mib",
        type=int,
        default=DEFAULT_MAX_ASSET_BYTES // (1024 * 1024),
        help="单个附件大小上限（MiB）",
    )
    parser.add_argument(
        "--max-total-assets-mib",
        type=int,
        default=DEFAULT_MAX_TOTAL_ASSET_BYTES // (1024 * 1024),
        help="全部附件累计下载上限（MiB）",
    )
    parser.add_argument(
        "--strict-assets",
        action="store_true",
        help="任一附件无法归档时让导出失败",
    )
    parser.add_argument(
        "--include-non-chat-roles",
        action="store_true",
        help="同时导出非用户/助手角色；仍跳过隐藏消息",
    )
    return parser


def _write_json_summary(payload) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    source = args.input

    try:
        if (args.json_summary or args.quiet) and not args.output:
            raise ExtractionError(
                "output", "--json-summary and --quiet require an output Markdown path"
            )
        if args.diagnose and (
            args.output or args.download_assets or args.json_summary or args.quiet
        ):
            raise ExtractionError(
                "output",
                "--diagnose cannot be combined with output, asset download, or summary modes",
            )
        if "://" in source:
            html = fetch_html(source)
        else:
            html = read_html_file(source)
        if args.diagnose:
            report = diagnose_html(
                html, include_non_chat_roles=args.include_non_chat_roles
            )
            sys.stdout.write(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            return 0 if report["status"] == "ok" else 2
        rich_messages = _extract_rich_messages(
            html, include_non_chat_roles=args.include_non_chat_roles
        )
        output_path = Path(args.output) if args.output else None
        if args.download_assets and output_path is None:
            raise ExtractionError(
                "output", "--download-assets requires an output Markdown path"
            )
        downloaded = 0
        asset_failures = 0
        manifest_path = None
        if args.download_assets:
            downloaded, asset_failures, manifest_path = download_asset_references(
                rich_messages,
                output_path,
                assets_dir=args.assets_dir,
                additional_hosts=args.asset_host,
                max_asset_bytes=args.max_asset_mib * 1024 * 1024,
                max_total_bytes=args.max_total_assets_mib * 1024 * 1024,
                strict=args.strict_assets,
            )
        markdown = rich_to_markdown(
            rich_messages,
            roles=not args.no_roles,
            bold=args.bold_roles,
            output_parent=output_path.resolve().parent if output_path else None,
            assets_requested=args.download_assets,
        )
        if args.output:
            try:
                output_path.write_text(markdown, encoding="utf-8")
            except OSError as exc:
                raise ExtractionError("output", "could not write the Markdown file") from exc
            if args.json_summary:
                _write_json_summary(
                    {
                        "assets_downloaded": downloaded,
                        "assets_unavailable": asset_failures,
                        "manifest": str(manifest_path) if manifest_path else None,
                        "messages": len(rich_messages),
                        "output": str(output_path),
                        "status": "ok",
                    }
                )
            elif not args.quiet:
                summary = f"已写入 {args.output}（共 {len(rich_messages)} 条消息"
                if args.download_assets:
                    summary += f"，归档 {downloaded} 个附件，{asset_failures} 个不可用"
                summary += "）"
                print(summary)
                if manifest_path is not None:
                    print(f"附件清单：{manifest_path}")
        else:
            sys.stdout.write(markdown)
        return 0
    except ExtractionError as exc:
        if args.json_summary:
            _write_json_summary(
                {
                    "error": exc.message,
                    "stage": exc.stage,
                    "status": "error",
                }
            )
        else:
            print(f"失败：{exc}", file=sys.stderr)
            if exc.stage == "fetch":
                print(POWERSHELL_HINT, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
