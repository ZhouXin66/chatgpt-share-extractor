---
name: chatgpt-share-extractor
description: Extract visible conversation messages from public https://chatgpt.com/share/... pages or saved ChatGPT share-page HTML and export them as Markdown. Use when a user asks to read, recover, archive, summarize, or convert a public ChatGPT shared conversation. Do not use for private conversations, authenticated account data, arbitrary webpages, or bypassing access controls.
---

# ChatGPT Share Extractor

Extract a public ChatGPT shared conversation with the bundled standard-library Python script. Preserve headings, tables, formulas, code blocks, and message order when the source page exposes them.

## Workflow

1. Confirm that the input is either a public `https://chatgpt.com/share/...` URL or a local HTML file saved from such a page.
2. Treat the share URL, downloaded HTML, and extracted Markdown as potentially sensitive. Do not echo the full URL or conversation into logs, issues, or unrelated files.
3. Resolve this skill's directory from the loaded `SKILL.md`, then run `scripts/extract_chatgpt_share.py` with the requested input and output path.
4. If direct URL fetching fails because the execution environment has no permitted network path, use an available sanctioned browser or web-fetch capability to save the public page as HTML. On Windows, `Invoke-WebRequest` is an optional fallback, not a requirement.
5. Run the extractor on the saved HTML and verify the reported visible-message count and output file.
6. If parsing fails, read [references/format-notes.md](references/format-notes.md) before changing the parser.

## Run the extractor

Use Python 3 and quote paths:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" "https://chatgpt.com/share/<id>" -o conversation.md
```

For a previously saved page:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" share.html -o conversation.md
```

Use `--no-roles` to omit role labels or `--bold-roles` to bold them. Without `-o`, the script writes Markdown to stdout; avoid stdout when the conversation may be sensitive or terminal output is recorded.

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
- Tell the user where the Markdown was written.
- Remove temporary HTML only when the user requested cleanup or the file was created solely as a disposable intermediate.

## Handle failures

- For `URL validation`, correct the URL; do not broaden the allowed host without user intent.
- For `fetch`, distinguish HTTP denial, timeout, and restricted network access before suggesting a fallback.
- For `page recognition`, check whether the file is an error, login, or bot-challenge page.
- For `payload decoding` or `conversation discovery`, read the technical reference and inspect only structural metadata by default.
- For `message extraction`, report that the share contained no visible non-empty messages.

Never include the raw payload, full share URL, or extracted conversation in a public bug report without the user's explicit approval.
