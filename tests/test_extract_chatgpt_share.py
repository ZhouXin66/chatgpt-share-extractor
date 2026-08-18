import importlib.util
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock


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


class FakeAssetResponse(io.BytesIO):
    def __init__(self, body, url, content_type, filename):
        super().__init__(body)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


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

    def test_resolve_rejects_cycle(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "cyclic"):
            extractor.resolve([[0]])

    def test_uses_semantic_payload_instead_of_first_payload(self):
        irrelevant = enqueue_html(["not a conversation"])
        conversation = enqueue_html(flatten(sample_root()))
        messages = extractor.extract_messages(irrelevant + conversation)
        self.assertEqual(messages, [("user", "你好"), ("assistant", "你好！")])

    def test_missing_enqueue_is_page_recognition_error(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "page recognition"):
            extractor.extract_messages("<html><title>Login</title></html>")


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

    def test_renders_structured_text_and_assets_without_internal_json(self):
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
        self.assertIn("![Diagram]", rendered)
        self.assertIn("[data.csv]", rendered)
        self.assertNotIn('"content_type"', rendered)
        self.assertEqual(len(messages[0].assets), 2)

    def test_discovers_visible_markdown_image_and_sandbox_file_links(self):
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
        self.assertEqual(len(messages[0].assets), 2)
        self.assertEqual(messages[0].assets[0].kind, "image")
        self.assertEqual(messages[0].assets[1].pointer, "sandbox:/mnt/data/result.csv")
        self.assertEqual(extractor.render_rich_message(messages[0]), text)

    def test_discovers_asset_nested_in_multimodal_part(self):
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
        self.assertEqual(len(messages[0].assets), 1)
        self.assertEqual(messages[0].assets[0].filename, "nested.webp")


class AssetDownloadTests(unittest.TestCase):
    def test_downloads_asset_rewrites_markdown_and_writes_private_manifest(self):
        asset_url = "https://files.oaiusercontent.com/diagram.png?signature=secret"
        asset = extractor.AssetReference(
            kind="image",
            filename="diagram.png",
            message_index=0,
            part_index=1,
            url=asset_url,
            mime_type="image/png",
            alt_text="Diagram",
        )
        messages = [extractor.RichMessage("assistant", ["Image", asset])]
        response = FakeAssetResponse(
            b"synthetic-png", asset_url, "image/png", "../../diagram.png"
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "conversation.md"
            with mock.patch.object(extractor, "_open_asset", return_value=response):
                downloaded, failures, manifest_path = extractor.download_asset_references(
                    messages, output_path
                )
            self.assertEqual((downloaded, failures), (1, 0))
            self.assertTrue((Path(directory) / "assets" / "diagram.png").is_file())
            markdown = extractor.rich_to_markdown(
                messages,
                output_parent=output_path.parent,
                assets_requested=True,
            )
            self.assertIn("assets/diagram.png", markdown)
            manifest = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("signature=secret", manifest)
            self.assertIn('"status": "downloaded"', manifest)

    def test_size_limit_failure_cleans_partial_file(self):
        asset_url = "https://files.oaiusercontent.com/large.bin"
        asset = extractor.AssetReference(
            kind="file",
            filename="large.bin",
            message_index=0,
            part_index=0,
            url=asset_url,
        )
        messages = [extractor.RichMessage("assistant", [asset])]
        response = FakeAssetResponse(b"12345678", asset_url, "application/octet-stream", "large.bin")
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "conversation.md"
            with mock.patch.object(extractor, "_open_asset", return_value=response):
                downloaded, failures, _ = extractor.download_asset_references(
                    messages, output_path, max_asset_bytes=4
                )
            self.assertEqual((downloaded, failures), (0, 1))
            self.assertEqual(
                [path.name for path in (Path(directory) / "assets").iterdir()],
                ["assets.json"],
            )

    def test_unresolved_pointer_is_manifested_without_aborting(self):
        asset = extractor.AssetReference(
            kind="file",
            filename="report.pdf",
            message_index=0,
            part_index=0,
            pointer="file-service://file-example",
            mime_type="application/pdf",
        )
        messages = [extractor.RichMessage("assistant", [asset])]
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "conversation.md"
            downloaded, failures, manifest_path = extractor.download_asset_references(
                messages, output_path
            )
            self.assertEqual((downloaded, failures), (0, 1))
            self.assertIn(
                "附件未归档",
                extractor.rich_to_markdown(
                    messages,
                    output_parent=output_path.parent,
                    assets_requested=True,
                ),
            )
            self.assertTrue(manifest_path.is_file())

    def test_rejects_untrusted_or_non_https_asset_targets(self):
        with self.assertRaisesRegex(extractor.ExtractionError, "not allowed"):
            extractor._validate_asset_target(
                "https://example.com/file.png", extractor.DEFAULT_ASSET_HOSTS
            )
        with self.assertRaisesRegex(extractor.ExtractionError, "HTTPS"):
            extractor._validate_asset_target(
                "http://files.oaiusercontent.com/file.png",
                extractor.DEFAULT_ASSET_HOSTS,
            )
        with self.assertRaisesRegex(extractor.ExtractionError, "literal IP"):
            extractor._normalized_asset_hosts(["127.0.0.1"])

    def test_cli_downloads_asset_and_writes_relative_link(self):
        asset_url = "https://files.oaiusercontent.com/report.csv"
        asset_node = {
            "message": {
                "author": {"role": "assistant"},
                "content": {
                    "parts": [
                        "Download",
                        {
                            "content_type": "file_attachment",
                            "download_url": asset_url,
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
                            "mapping": {"asset": asset_node},
                            "linear_conversation": ["asset"],
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
            response = FakeAssetResponse(
                b"a,b\n1,2\n", asset_url, "text/csv", "report.csv"
            )
            with mock.patch.object(extractor, "_open_asset", return_value=response):
                code = extractor.main(
                    [str(input_path), "-o", str(output_path), "--download-assets"]
                )
            self.assertEqual(code, 0)
            self.assertIn(
                "assets/report.csv", output_path.read_text(encoding="utf-8")
            )
            self.assertTrue((Path(directory) / "assets" / "assets.json").is_file())


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


if __name__ == "__main__":
    unittest.main()
