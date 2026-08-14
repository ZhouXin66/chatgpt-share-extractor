import importlib.util
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
