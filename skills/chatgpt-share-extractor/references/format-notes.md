# ChatGPT share-page format notes

Read this reference only when extraction fails or the share-page format appears to have changed.

## Processing stages

1. Fetch or read the HTML without authenticated account data.
2. Locate double-quoted `enqueue("...")` JavaScript string payloads.
3. Decode each JavaScript string.
4. Classify the decoded text as a JSON root or a typed stream control frame.
5. Resolve Promise control frames and the React Router flattened reference graph.
6. Locate conversation data by semantic fields.
7. Extract visible non-empty messages in conversation order.

Keep failures assigned to these stages. Do not collapse every structural error into "the page changed," because login pages, bot challenges, truncated files, and revoked shares can look similar.

## Flattened reference format

The observed format is a JSON array whose item at index `0` is the root reference. During resolution:

- A non-negative integer is an array reference.
- A negative integer represents an undefined-like value and resolves to `None`.
- An object key such as `_12` means that the real key string is stored at array index `12`.
- A two-item list shaped like `["P", index]` acts as a promise placeholder and resolves through `index`.
- Other strings, booleans, floating-point values, and null values are literals.

Create dictionary and ordinary-list containers in the resolver cache before visiting their children. This preserves legitimate parent/child back-references and self-referential containers. Reject out-of-range references, invalid object-key references, and unresolvable Promise-alias cycles; do not reject a graph merely because it contains a container cycle.

## Stream control frames

An enqueue value such as `P633:[{}]` is a Promise resolution frame, not malformed JSON. Parse it as:

- Frame kind: `P`.
- Promise target: `633`.
- JSON body: `[{}]`.

Resolve the frame body as its own flattened value and make the result available when the root graph encounters `["P", 633]`. A self-referential Promise marker without a matching frame is an unresolved deferred value and may resolve to `None` when it is not required for the visible conversation. Reject mutually recursive Promise aliases that have no concrete frame value.

Recognize structurally valid non-`P` control frames and count them in diagnostics, but do not invent semantics for unknown frame kinds.

## Conversation discovery

The historically observed path is:

```text
loaderData
└── routes/share.$shareId.($action)
    └── serverResponse
        └── data
```

Conversation data normally contains:

- `mapping`: node mapping or resolved node objects.
- `linear_conversation`: ordered nodes or node identifiers.
- `message.author.role`: message role.
- `message.content.parts`: message content.
- `message.metadata.is_visually_hidden_from_conversation`: hidden-message marker.

Try the known path first, then search resolved objects for a `mapping` object plus `linear_conversation` or message-bearing mapping values. Do not select an enqueue payload only because it is first or largest.

## Conversation order and visibility

Use `linear_conversation` when it exists. Entries may be node identifiers or resolved node objects. When it is absent, reconstruct the selected branch from `current_node` or `current_node_id` by following each node's `parent` link and reversing the resulting path. If no current node is exposed, choose the deepest valid parent chain. Do not silently use `mapping.values()` as conversation order.

By default export only visible `user` and `assistant` messages. Skip messages marked by `is_visually_hidden_from_conversation`, `is_hidden`, `hidden`, `hide_in_conversation`, or `is_context_message`. Include other roles only when the user explicitly requests them; hidden messages remain excluded.

## Rich message parts and attachments

Treat `message.content.parts` as typed parts rather than concatenating arbitrary objects as JSON. Preserve string parts and public text fields such as `text`, `caption`, and `transcript`. Recognize image, audio, video, file, and attachment parts from their content type and fields such as:

- `image_url`, `audio_url`, `download_url`, `asset_url`, `url`, or `src`.
- `asset_pointer`, `file_pointer`, or `sandbox_path`.
- `filename`, `file_name`, `mime_type`, `alt_text`, or `caption`.
- Attachment collections under the message, content, or metadata objects.

Deduplicate repeated structured references within a message and render them as lightweight blockquoted notices. Include the filename or public label when available. Leave ordinary Markdown links in message text unchanged. Do not download referenced files or invent a private endpoint for `file-service://` and `sandbox:` pointers.

## Failure triage

- **No enqueue payloads:** Check whether the input is an HTML error page, login redirect, consent page, bot challenge, or a newly redesigned share page.
- **JavaScript decoding:** The `enqueue` string literal itself could not be safely unescaped.
- **JSON decoding:** Decoded text was neither JSON nor a structurally valid control frame, or no JSON root was present.
- **Control frame decoding:** A typed frame was recognized but its JSON body was invalid.
- **Reference resolution:** JSON roots were found but their flattened references, object keys, or Promise aliases were invalid.
- **Decoded but no conversation:** Inspect key names and nesting. Prefer adding a semantic fallback over replacing the known fast path.
- **Conversation but no messages:** Check whether `linear_conversation` changed from nodes to IDs, content moved away from `parts`, or every message is hidden/empty.

Use `--diagnose` to print only structural counts such as enqueue values, frame kinds, array sizes, references, Promise markers, candidate conversations, visible-message count, attachment-reference count, and failure stage. Diagnostic output must never include the input URL, raw payload, message text, filenames, or attachment sources.

Never place raw HTML, full payloads, share IDs, or extracted text into public issues or fixtures. Build the smallest synthetic fixture that reproduces a parser problem.
