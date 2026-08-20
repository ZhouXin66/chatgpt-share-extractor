---
name: chatgpt-share-extractor
description: Extract visible conversation text from public https://chatgpt.com/share/... pages or saved ChatGPT share-page HTML and export Markdown, preserving formatting, the selected branch, and notices that images or files existed. Use when a user asks to read, recover, archive, summarize, or convert a public ChatGPT shared conversation, including requests that mention its images or files. Export attachment notices rather than file bytes. Do not use for private conversations, authenticated account data, arbitrary webpages, or bypassing access controls.
---

# ChatGPT Share Extractor

Export the visible text of a public ChatGPT shared conversation with the bundled standard-library Python script. Preserve headings, tables, formulas, code blocks, the selected conversation branch, and lightweight attachment notices.

## Workflow

1. Confirm that the input is a public `https://chatgpt.com/share/...` URL or a local HTML file saved from such a page.
2. Treat the share URL, downloaded HTML, and extracted Markdown as potentially sensitive. Do not echo the full URL or conversation into logs, issues, or unrelated files.
3. Resolve this skill's directory from the loaded `SKILL.md`, then run `scripts/extract_chatgpt_share.py` with the requested input and output path. Pass `--json-summary` for a compact machine-readable result.
4. If direct URL fetching fails because the execution environment has no permitted network path, use an available sanctioned browser or web-fetch capability to save the public page as HTML.
5. Verify the reported message and attachment counts, output ordering, role labels, and attachment notices.
6. If parsing fails, run the same input with `--diagnose`, inspect only its structural counts and failure stage, then read [references/format-notes.md](references/format-notes.md) before changing the parser.

## Run the extractor

Use Python 3 and quote paths:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" "https://chatgpt.com/share/<id>" -o conversation.md --json-summary
```

For a previously saved page:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" share.html -o conversation.md --json-summary
```

Use `--no-roles` to omit role labels, `--bold-roles` to bold them, or `--include-non-chat-roles` when the user explicitly requests other visible roles. Without `-o`, the script writes Markdown to stdout; avoid stdout when the conversation may be sensitive or terminal output is recorded.

Use `--json-summary` with `-o` for a single-line success or failure report that omits the source URL and conversation content. Use `--quiet` instead when no success output is needed. Do not combine either mode with `--diagnose`.

For safe structural diagnostics without exporting content:

```bash
python "<skill-directory>/scripts/extract_chatgpt_share.py" share.html --diagnose
```

## Handle attachments

Represent structured image, audio, video, and file references as blockquoted notices. Include the filename or public label when available. Preserve ordinary Markdown links already present in message text, but do not fetch linked or referenced files.

Public share data may expose only attachment metadata or an internal pointer rather than file bytes. Do not request cookies, tokens, credentials, or private endpoints to retrieve those bytes. If the user requests a complete media archive, explain this limitation and ask them to provide the original files separately.

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
- Inspect only enough output to verify ordering, role labels, and attachment notices.
- Tell the user where the Markdown was written and how many attachments were marked.
- Remove temporary HTML only when the user requested cleanup or the file was created solely as a disposable intermediate.

Keep errors assigned to their reported stage. Never include the raw payload, full share URL, or extracted conversation in a public bug report without the user's explicit approval.
