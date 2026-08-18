---
name: chatgpt-share-extractor
description: Extract visible conversation messages and referenced public images or files from public https://chatgpt.com/share/... pages or saved ChatGPT share-page HTML, and export a local Markdown archive. Use when a user asks to read, recover, archive, summarize, or convert a public ChatGPT shared conversation. Do not use for private conversations, authenticated account data, arbitrary webpages, or bypassing access controls.
---

# ChatGPT Share Extractor

Extract a public ChatGPT shared conversation with the bundled standard-library Python script. Preserve headings, tables, formulas, code blocks, the selected conversation branch, and visible referenced assets when the source page exposes them.

## Workflow

1. Confirm that the input is either a public `https://chatgpt.com/share/...` URL or a local HTML file saved from such a page.
2. Treat the share URL, downloaded HTML, and extracted Markdown as potentially sensitive. Do not echo the full URL or conversation into logs, issues, or unrelated files.
3. Resolve this skill's directory from the loaded `SKILL.md`, then run `scripts/extract_chatgpt_share.py` with the requested input and output path. For file exports, pass `--download-assets` unless the user explicitly requests text only, and pass `--json-summary` for a compact machine-readable result.
4. If direct URL fetching fails because the execution environment has no permitted network path, use an available sanctioned browser or web-fetch capability to save the public page as HTML. On Windows, `Invoke-WebRequest` is an optional fallback, not a requirement.
5. Run the extractor on the saved HTML and verify the reported visible-message and asset counts, output file, relative asset links, and `assets/assets.json` when assets were referenced.
6. If parsing fails, run the same input with `--diagnose`, inspect only its structural counts and failure stage, then read [references/format-notes.md](references/format-notes.md) before changing the parser.

## Run the extractor

Use Python 3 and quote paths:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" "https://chatgpt.com/share/<id>" -o conversation.md --download-assets --json-summary
```

For a previously saved page:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" share.html -o conversation.md --download-assets --json-summary
```

Use `--no-roles` to omit role labels or `--bold-roles` to bold them. Use `--assets-dir <path>` to override the default `assets/` folder beside the Markdown. Use `--strict-assets` only when the whole export must fail if any referenced asset is unavailable. Use `--asset-host <domain>` only with user intent when a legitimate public asset is hosted outside the built-in ChatGPT/OpenAI asset domains. Without `-o`, the script writes Markdown to stdout; avoid stdout when the conversation may be sensitive or terminal output is recorded.

Use `--json-summary` with `-o` for a single-line success or failure report that omits the source URL and conversation content. Use `--quiet` instead when no success output is needed; errors still go to standard error. Do not combine these mutually exclusive modes with `--diagnose`.

For safe structural diagnostics without exporting content:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" share.html --diagnose
```

Do not combine `--diagnose` with `-o` or `--download-assets`. Its JSON output intentionally omits the input URL, payload text, message text, filenames, and asset URLs.

Asset download is best-effort by default. Keep the Markdown export when an expired pointer or unavailable public URL prevents one asset from being archived, and report the failure in `assets.json`. Never request cookies, tokens, or credentials to retrieve it.

## Fetch fallback

Prefer a platform-native, policy-compliant fetch method. On Windows, this optional fallback saves the page for local parsing:

```powershell
Invoke-WebRequest -Uri "https://chatgpt.com/share/<id>" -UseBasicParsing -TimeoutSec 40 -UserAgent "Mozilla/5.0" |
  Select-Object -ExpandProperty Content |
  Out-File share.html -Encoding utf8
```

Do not request cookies, account tokens, or login credentials. Do not attempt to bypass authentication, bot protection, sandbox policy, or a revoked/private share.

## Validate the result

- Confirm the command exits with code `0`.
- Confirm the reported message count is plausible.
- Inspect only enough of the output to verify ordering and role labels.
- Confirm the Markdown uses relative paths for successfully archived assets.
- Confirm `assets.json` contains no original signed download URLs and records each unavailable asset.
- Tell the user where the Markdown was written.
- Remove temporary HTML only when the user requested cleanup or the file was created solely as a disposable intermediate.

## Handle failures

- For `URL validation`, correct the URL; do not broaden the allowed host without user intent.
- For `fetch`, distinguish HTTP denial, timeout, and restricted network access before suggesting a fallback.
- For `page recognition`, check whether the file is an error, login, or bot-challenge page.
- For `JavaScript decoding`, distinguish invalid string escaping from a changed enqueue wrapper.
- For `JSON decoding` or `control frame decoding`, distinguish serialized roots from typed stream frames.
- For `reference resolution`, inspect root sizes, Promise-frame counts, back-references, and unresolved Promise counts without printing payload content.
- For `conversation discovery`, inspect only semantic keys and structural metadata by default.
- For `message extraction`, report that the share contained no visible non-empty messages.
- For `asset download`, distinguish an unavailable public URL, disallowed host, expired link, and size-limit failure. Do not broaden the host allowlist without user intent.

Never include the raw payload, full share URL, or extracted conversation in a public bug report without the user's explicit approval.
