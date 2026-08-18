# ChatGPT Share Extractor

将公开的 `https://chatgpt.com/share/...` 分享页或已保存的分享页 HTML 转换为按消息排列的 Markdown。项目同时提供独立 Python 命令行工具和可安装的 Codex skill。

> 非 OpenAI 官方项目。页面内部格式可能变化；请仅处理你有权访问和保存的内容。

## 功能

- 仅依赖 Python 3 标准库。
- 保留当前对话分支、Markdown、代码块、表格和公式文本。
- 可将公开页面引用的图片、音频和文件归档到 Markdown 同级的 `assets/`，并生成不含签名 URL 的 `assets.json`。
- 默认添加 `用户：`、`ChatGPT：` 等角色标签。
- 默认只输出可见的用户/助手消息，并过滤隐藏或上下文消息。
- 从多个页面载荷中按数据结构识别对话，不依赖“第一个”或“最大”载荷。
- 支持直接抓取公开链接或解析本地 HTML。

本工具不读取私人账号对话，不使用登录 Cookie，也不绕过访问控制或反机器人措施。

## 命令行使用

脚本位于：

```text
skills/chatgpt-share-extractor/scripts/extract_chatgpt_share.py
```

直接读取公开分享链接：

```bash
python skills/chatgpt-share-extractor/scripts/extract_chatgpt_share.py \
  "https://chatgpt.com/share/<id>" \
  -o conversation.md \
  --download-assets
```

解析已保存的 HTML：

```bash
python skills/chatgpt-share-extractor/scripts/extract_chatgpt_share.py \
  share.html \
  -o conversation.md \
  --download-assets
```

可选参数：

| 参数 | 作用 |
|---|---|
| `-o`, `--output` | 输出 Markdown 文件；省略时打印到标准输出 |
| `--no-roles` | 不输出角色标签 |
| `--bold-roles` | 将角色标签加粗 |
| `--download-assets` | 归档页面公开引用的图片、音频和文件 |
| `--assets-dir` | 指定附件目录；默认使用 Markdown 同级的 `assets/` |
| `--asset-host` | 显式增加一个允许下载的 HTTPS 资源域名，可重复指定 |
| `--max-asset-mib` | 设置单个附件的大小上限 |
| `--max-total-assets-mib` | 设置全部附件的累计下载上限 |
| `--strict-assets` | 任一附件不可用时让导出失败 |
| `--include-non-chat-roles` | 同时导出非用户/助手角色，隐藏消息仍会跳过 |

附件下载默认采用尽力而为策略。无法公开下载的附件会在 Markdown 和 `assets.json` 中标记，但不会阻止其余对话导出。工具不会为 `file-service://` 指针猜测私有接口，也不会请求 Cookie 或令牌。

如果当前沙盒不允许 Python 访问网络，可以用环境允许的浏览器或网页工具先保存 HTML。Windows 下也可选择：

```powershell
Invoke-WebRequest -Uri "https://chatgpt.com/share/<id>" -UseBasicParsing -TimeoutSec 40 -UserAgent "Mozilla/5.0" |
  Select-Object -ExpandProperty Content |
  Out-File share.html -Encoding utf8
```

网络权限由运行环境决定；PowerShell 不是必需工具，也不应被用于绕过沙盒策略。

## 安装为 Codex skill

发布到 GitHub 后，在 Codex 中调用 `$skill-installer` 并提供 skill 子目录 URL：

```text
Use $skill-installer to install:
https://github.com/ZhouXin66/chatgpt-share-extractor/tree/main/skills/chatgpt-share-extractor
```

也可以将 `skills/chatgpt-share-extractor` 复制到 Codex 支持的个人或仓库 skill 目录。安装后可显式调用：

```text
$chatgpt-share-extractor 把这个公开分享链接导出为 Markdown：<链接>
```

## 隐私与安全

分享 URL 相当于“持有链接即可访问”的凭证。下载的 HTML、附件和导出的 Markdown 可能包含完整对话、代码、密钥、个人信息或其他敏感内容。

- 不要将真实分享 URL、HTML 或导出结果提交到 Git。
- 不要将原始载荷粘贴到公开 Issue。
- 不要向工具提供账号 Cookie、访问令牌或登录凭证。
- 上传或转发 Markdown 前先检查其中的敏感信息。
- 测试只使用人工构造的数据。

本项目许可证仅适用于项目源码和文档。用户负责确保其有权访问、处理、保存和分享目标对话。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不访问网络，也不包含真实对话。

## 页面格式与故障排查

ChatGPT 分享页曾将对话数据放入 `enqueue("...")` 脚本载荷，并使用 React Router 的扁平引用数组。解析器优先检查已知路径，然后按 `mapping`、`linear_conversation` 和消息节点等语义字段查找对话。

错误信息会标注阶段：

- `URL validation`
- `fetch`
- `file input`
- `page recognition`
- `payload decoding`
- `conversation discovery`
- `message extraction`
- `asset download`
- `output`

页面改版时，请先根据阶段判断是网络、错误页、载荷编码还是对话结构发生变化。

## License

[MIT](LICENSE)
