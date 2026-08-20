import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "chatgpt-share-extractor"
    / "scripts"
    / "extract_chatgpt_share.py"
)
SPEC = importlib.util.spec_from_file_location("extract_chatgpt_share", SCRIPT_PATH)
extractor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(extractor)


def flatten(root):
    """Build a small synthetic payload accepted by the reference resolver."""
    data = []

    def store(value):
        index = len(data)
        data.append(None)
        if isinstance(value, dict):
            encoded = {}
            for key, child in value.items():
                key_index = store(str(key))
                encoded[f"_{key_index}"] = (
                    store(child) if isinstance(child, (dict, list)) else child
                )
            data[index] = encoded
        elif isinstance(value, list):
            data[index] = [
                store(child) if isinstance(child, (dict, list)) else child
                for child in value
            ]
        else:
            data[index] = value
        return index

    assert store(root) == 0
    return data


def enqueue_html(data):
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded_string = json.dumps(serialized, ensure_ascii=False)
    return f"<script>window.__reactRouterContext.streamController.enqueue({encoded_string})</script>"


def enqueue_text(text):
    encoded_string = json.dumps(text, ensure_ascii=False)
    return f"<script>window.__reactRouterContext.streamController.enqueue({encoded_string})</script>"


def sample_root():
    user_node = {
        "message": {
            "author": {"role": "user"},
            "content": {"parts": ["你好"]},
            "metadata": {},
        }
    }
    assistant_node = {
        "message": {
            "author": {"role": "assistant"},
            "content": {"parts": ["你好！"]},
            "metadata": {},
        }
    }
    hidden_node = {
        "message": {
            "author": {"role": "system"},
            "content": {"parts": ["hidden"]},
            "metadata": {"is_visually_hidden_from_conversation": True},
        }
    }
    conversation = {
        "mapping": {
            "user-node": user_node,
            "assistant-node": assistant_node,
            "hidden-node": hidden_node,
        },
        "linear_conversation": [user_node, assistant_node, hidden_node],
    }
    return {
        "loaderData": {
            "routes/share.$shareId.($action)": {
                "serverResponse": {"data": conversation}
            }
        }
    }


class UrlValidationTests(unittest.TestCase):
    def test_accepts_supported_share_url_and_removes_fragment(self):
        actual = extractor.validate_share_url(
            "https://chatgpt.com/share/example-id?source=test#fragment"
        )
        self.assertEqual(
            actual, "https://chatgpt.com/share/example-id?source=test"
        )

    def test_rejects_http(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "only HTTPS"):
            extractor.validate_share_url("http://chatgpt.com/share/example-id")

    def test_rejects_other_hosts(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "only chatgpt.com"):
            extractor.validate_share_url("https://example.com/share/example-id")

    def test_rejects_non_share_path(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "share/<id>"):
            extractor.validate_share_url("https://chatgpt.com/c/example-id")

    def test_rejects_credentials(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "credentials"):
            extractor.validate_share_url(
                "https://user:secret@chatgpt.com/share/example-id"
            )

    def test_rejects_malformed_port(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "malformed"):
            extractor.validate_share_url(
                "https://chatgpt.com:not-a-port/share/example-id"
            )


class PayloadTests(unittest.TestCase):
    def test_resolve_rejects_out_of_range_reference(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "outside the payload"):
            extractor.resolve([[5]])

    def test_resolve_preserves_container_back_reference(self):
        data = [{"_1": 2}, "child", {"_3": 0}, "parent"]
        stats = {}
        root = extractor.resolve(data, stats=stats)
        self.assertIs(root["child"]["parent"], root)
        self.assertEqual(stats["container_back_references"], 1)

    def test_resolve_keeps_unresolved_self_promise_safe(self):
        stats = {}
        self.assertIsNone(extractor.resolve([["P", 0]], stats=stats))
        self.assertEqual(stats["unresolved_promises"], 1)

    def test_resolve_rejects_unresolvable_promise_alias_cycle(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "unresolvable cycle"):
            extractor.resolve([["P", 1], ["P", 0]])

    def test_applies_promise_control_frame_before_resolving_root(self):
        data = flatten(sample_root())
        key_index = len(data)
        data.append("deferred")
        marker_index = len(data)
        data.append(["P", marker_index])
        data[0][f"_{key_index}"] = marker_index
        html = enqueue_html(data) + enqueue_text(f"P{marker_index}:[{{}}]\n")
        messages = extractor.extract_messages(html)
        self.assertEqual(messages, [("user", "你好"), ("assistant", "你好！")])
        _, report = extractor._resolved_payload_stream(html)
        self.assertEqual(report["promise_frames"], 1)
        self.assertEqual(report["resolved_promises"], 1)
        self.assertEqual(report["unresolved_promises"], 0)

    def test_uses_semantic_payload_instead_of_first_payload(self):
        irrelevant = enqueue_html(["not a conversation"])
        conversation = enqueue_html(flatten(sample_root()))
        messages = extractor.extract_messages(irrelevant + conversation)
        self.assertEqual(messages, [("user", "你好"), ("assistant", "你好！")])

    def test_missing_enqueue_is_page_recognition_error(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "page recognition"):
            extractor.extract_messages("<html><title>Login</title></html>")

    def test_reports_json_decoding_separately(self):
        with self.assertRaises(extractor.ExtractionError) as raised:
            extractor.extract_messages(enqueue_text("not-json"))
        self.assertEqual(raised.exception.stage, "JSON decoding")

    def test_reports_javascript_decoding_separately(self):
        html = r'<script>window.__reactRouterContext.streamController.enqueue("\x")</script>'
        with self.assertRaises(extractor.ExtractionError) as raised:
            extractor.extract_messages(html)
        self.assertEqual(raised.exception.stage, "JavaScript decoding")

    def test_counts_malformed_control_frame_body(self):
        report = extractor.diagnose_html(enqueue_text("P7:not-json"))
        self.assertEqual(report["control_frame_decoding_failures"], 1)
        self.assertEqual(report["failure_stage"], "control frame decoding")

    def test_reports_reference_resolution_separately(self):
        with self.assertRaises(extractor.ExtractionError) as raised:
            extractor.extract_messages(enqueue_html([[5]]))
        self.assertEqual(raised.exception.stage, "reference resolution")

    def test_diagnostics_expose_only_structural_counts(self):
        report = extractor.diagnose_html(enqueue_html(flatten(sample_root())))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["visible_messages"], 2)
        self.assertNotIn("你好", serialized)
        self.assertNotIn("chatgpt.com/share", serialized)


class RichMessageTests(unittest.TestCase):
    def test_reconstructs_selected_parent_chain_without_mapping_order_fallback(self):
        system = {
            "id": "system",
            "parent": None,
            "message": {
                "author": {"role": "system"},
                "content": {"parts": ["context"]},
                "metadata": {},
            },
        }
        user = {
            "id": "user",
            "parent": "system",
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["Question"]},
                "metadata": {},
            },
        }
        answer_a = {
            "id": "answer-a",
            "parent": "user",
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["Old branch"]},
                "metadata": {},
            },
        }
        answer_b = {
            "id": "answer-b",
            "parent": "user",
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["Selected branch"]},
                "metadata": {},
            },
        }
        data = {
            "mapping": {
                "answer-a": answer_a,
                "system": system,
                "answer-b": answer_b,
                "user": user,
            },
            "current_node": "answer-b",
        }
        self.assertEqual(
            extractor.extract_messages_from_data(data),
            [("user", "Question"), ("assistant", "Selected branch")],
        )

    def test_filters_non_chat_roles_unless_explicitly_included(self):
        tool_node = {
            "message": {
                "author": {"role": "tool"},
                "content": {"parts": ["tool output"]},
                "metadata": {},
            }
        }
        conversation = sample_root()["loaderData"][
            "routes/share.$shareId.($action)"
        ]["serverResponse"]["data"]
        conversation["linear_conversation"].insert(1, tool_node)
        self.assertNotIn(
            ("tool", "tool output"), extractor.extract_messages_from_data(conversation)
        )
        self.assertIn(
            ("tool", "tool output"),
            extractor.extract_messages_from_data(
                conversation, include_non_chat_roles=True
            ),
        )

    def test_renders_structured_text_and_attachment_notices_without_internal_json(self):
        node = {
            "message": {
                "author": {"role": "assistant"},
                "content": {
                    "parts": [
                        {"content_type": "text", "text": "Result"},
                        {
                            "content_type": "image_asset_pointer",
                            "image_url": "https://files.oaiusercontent.com/diagram.png",
                            "filename": "diagram.png",
                            "mime_type": "image/png",
                            "alt_text": "Diagram",
                        },
                    ]
                },
                "metadata": {
                    "attachments": [
                        {
                            "download_url": "https://files.oaiusercontent.com/data.csv",
                            "filename": "data.csv",
                            "mime_type": "text/csv",
                        }
                    ]
                },
            }
        }
        messages = extractor.extract_rich_messages_from_data(
            {"mapping": {"node": node}, "linear_conversation": ["node"]}
        )
        rendered = extractor.render_rich_message(messages[0])
        self.assertIn("Result", rendered)
        self.assertIn("原对话包含图片：diagram.png", rendered)
        self.assertIn("原对话包含文件：data.csv", rendered)
        self.assertIn("本导出仅保留附件提示", rendered)
        self.assertNotIn('"content_type"', rendered)
        self.assertEqual(len(messages[0].attachments), 2)

    def test_preserves_visible_markdown_links_without_extra_notices(self):
        text = (
            "![Plot](https://files.oaiusercontent.com/plot.png)\n\n"
            "[Download](sandbox:/mnt/data/result.csv)"
        )
        node = {
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": [text]},
                "metadata": {},
            }
        }
        messages = extractor.extract_rich_messages_from_data(
            {"mapping": {"node": node}, "linear_conversation": ["node"]}
        )
        self.assertEqual(messages[0].attachments, [])
        self.assertEqual(extractor.render_rich_message(messages[0]), text)

    def test_discovers_attachment_nested_in_multimodal_part(self):
        node = {
            "message": {
                "author": {"role": "assistant"},
                "content": {
                    "parts": [
                        {
                            "content_type": "multimodal_text",
                            "parts": [
                                {
                                    "content_type": "image_asset_pointer",
                                    "image_url": "https://files.oaiusercontent.com/nested.webp",
                                    "filename": "nested.webp",
                                }
                            ],
                        }
                    ]
                },
                "metadata": {},
            }
        }
        messages = extractor.extract_rich_messages_from_data(
            {"mapping": {"node": node}, "linear_conversation": ["node"]}
        )
        self.assertEqual(len(messages[0].attachments), 1)
        self.assertEqual(messages[0].attachments[0].filename, "nested.webp")


class AttachmentNoticeCliTests(unittest.TestCase):
    def test_notice_without_filename_uses_type_only(self):
        notice = extractor.AttachmentNotice(
            kind="image",
            message_index=0,
            part_index=0,
            source="file-service://file-example",
        )
        message = extractor.RichMessage("user", [notice])
        self.assertEqual(
            extractor.render_rich_message(message),
            "> [原对话包含图片；本导出仅保留附件提示]",
        )

    def test_cli_marks_attachment_without_creating_assets_directory(self):
        attachment_node = {
            "message": {
                "author": {"role": "assistant"},
                "content": {
                    "parts": [
                        "Download",
                        {
                            "content_type": "file_attachment",
                            "download_url": "https://files.oaiusercontent.com/report.csv",
                            "filename": "report.csv",
                            "mime_type": "text/csv",
                        },
                    ]
                },
                "metadata": {},
            }
        }
        root = {
            "loaderData": {
                "routes/share.$shareId.($action)": {
                    "serverResponse": {
                        "data": {
                            "mapping": {"attachment": attachment_node},
                            "linear_conversation": ["attachment"],
                        }
                    }
                }
            }
        }
        html = enqueue_html(flatten(root))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            output_path = Path(directory) / "conversation.md"
            input_path.write_text(html, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = extractor.main(
                    [str(input_path), "-o", str(output_path), "--json-summary"]
                )
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["attachments"], 1)
            self.assertNotIn("assets_downloaded", report)
            self.assertNotIn("manifest", report)
            self.assertIn(
                "原对话包含文件：report.csv",
                output_path.read_text(encoding="utf-8"),
            )
            self.assertFalse((Path(directory) / "assets").exists())


class MarkdownAndCliTests(unittest.TestCase):
    def test_markdown_options(self):
        messages = [("user", "Hello"), ("assistant", "World")]
        self.assertIn("用户：\nHello", extractor.to_markdown(messages))
        self.assertIn("**ChatGPT**：", extractor.to_markdown(messages, bold=True))
        self.assertEqual(
            extractor.to_markdown(messages, roles=False), "Hello\n\nWorld\n"
        )

    def test_cli_reads_synthetic_html_and_writes_markdown(self):
        html = enqueue_html(flatten(sample_root()))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            output_path = Path(directory) / "conversation.md"
            input_path.write_text(html, encoding="utf-8")
            code = extractor.main([str(input_path), "-o", str(output_path)])
            self.assertEqual(code, 0)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "用户：\n你好\n\nChatGPT：\n你好！\n",
            )

    def test_cli_quiet_suppresses_success_output(self):
        html = enqueue_html(flatten(sample_root()))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            output_path = Path(directory) / "conversation.md"
            input_path.write_text(html, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = extractor.main(
                    [str(input_path), "-o", str(output_path), "--quiet"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(output_path.is_file())

    def test_cli_json_summary_is_compact_and_content_free(self):
        html = enqueue_html(flatten(sample_root()))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            output_path = Path(directory) / "conversation.md"
            input_path.write_text(html, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = extractor.main(
                    [str(input_path), "-o", str(output_path), "--json-summary"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().count("\n"), 1)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["messages"], 2)
            self.assertEqual(report["attachments"], 0)
            self.assertEqual(report["output"], str(output_path))
            self.assertNotIn("manifest", report)
            self.assertNotIn("你好", stdout.getvalue())

    def test_cli_json_summary_reports_safe_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            output_path = Path(directory) / "conversation.md"
            input_path.write_text("<html>not a share</html>", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = extractor.main(
                    [str(input_path), "-o", str(output_path), "--json-summary"]
                )
            self.assertEqual(code, 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["stage"], "page recognition")
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(str(input_path), stdout.getvalue())

    def test_summary_modes_require_an_output_path(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = extractor.main(["unused.html", "--json-summary"])
        self.assertEqual(code, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["stage"], "output")

    def test_cli_diagnose_prints_safe_json_without_writing_markdown(self):
        html = enqueue_html(flatten(sample_root()))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "share.html"
            input_path.write_text(html, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = extractor.main([str(input_path), "--diagnose"])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["visible_messages"], 2)
            self.assertNotIn("你好", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
