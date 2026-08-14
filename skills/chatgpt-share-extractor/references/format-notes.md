# ChatGPT share-page format notes

Read this reference only when extraction fails or the share-page format appears to have changed.

## Processing stages

1. Fetch or read the HTML without authenticated account data.
2. Locate double-quoted `enqueue("...")` JavaScript string payloads.
3. Decode the JavaScript string and then its JSON value.
4. Resolve the React Router flattened reference array.
5. Locate conversation data by semantic fields.
6. Extract visible non-empty messages in conversation order.

Keep failures assigned to these stages. Do not collapse every structural error into "the page changed," because login pages, bot challenges, truncated files, and revoked shares can look similar.

## Flattened reference format

The observed format is a JSON array whose item at index `0` is the root reference. During resolution:

- A non-negative integer is an array reference.
- A negative integer represents an undefined-like value and resolves to `None`.
- An object key such as `_12` means that the real key string is stored at array index `12`.
- A two-item list shaped like `["P", index]` acts as a promise placeholder and resolves through `index`.
- Other strings, booleans, floating-point values, and null values are literals.

Reject out-of-range references, invalid object-key references, and cycles. These usually indicate a changed format, the wrong payload, or truncated input.

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

## Failure triage

- **No enqueue payloads:** Check whether the input is an HTML error page, login redirect, consent page, bot challenge, or a newly redesigned share page.
- **No decodable payloads:** Inspect payload counts and lengths, not their contents. Check whether quoting or chunking changed.
- **Decoded but no conversation:** Inspect key names and nesting. Prefer adding a semantic fallback over replacing the known fast path.
- **Conversation but no messages:** Check whether `linear_conversation` changed from nodes to IDs, content moved away from `parts`, or every message is hidden/empty.

Never place raw HTML, full payloads, share IDs, or extracted text into public issues or fixtures. Build the smallest synthetic fixture that reproduces a parser problem.
