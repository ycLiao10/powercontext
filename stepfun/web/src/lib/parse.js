/**
 * 文档解析：把用户粘贴的文本或上传的文件切成待向量的文档片段。
 *
 * 支持的输入格式（每行一条，用空行或 `---` 分隔也可以）：
 *   - JSONL：每行一个 JSON，含 id/title/content/metadata
 *   - 纯文本：按空行分段；`# 标题` 会被识别为该段的标题
 */

/** 从纯文本或 JSONL 解析出文档数组。 */
export function parseDocuments(rawText, sourceName = 'pasted') {
  const text = String(rawText || '').trim();
  if (!text) return [];

  const lines = text.split('\n');
  const looksJsonl = lines
    .filter((l) => l.trim())
    .every((l) => {
      const t = l.trim();
      return t.startsWith('{') && t.endsWith('}');
    });

  if (looksJsonl) {
    return lines
      .filter((l) => l.trim())
      .map((l, i) => {
        const obj = JSON.parse(l);
        return {
          id: String(obj.id || `${sourceName}-${i + 1}`),
          title: obj.title || obj.id || `${sourceName}-${i + 1}`,
          content: String(obj.content ?? ''),
          metadata: obj.metadata || {},
        };
      })
      .filter((d) => d.content.trim());
  }

  // 纯文本：按空行分段
  const blocks = text.split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  return blocks.map((block, i) => {
    const blockLines = block.split('\n');
    let title = '';
    let bodyLines = blockLines;
    if (blockLines[0].trim().startsWith('#')) {
      title = blockLines[0].trim().replace(/^#+\s*/, '');
      bodyLines = blockLines.slice(1);
    }
    const content = bodyLines.join('\n').trim();
    return {
      id: `${sourceName}-${i + 1}`,
      title: title || content.slice(0, 24) || `文档 ${i + 1}`,
      content,
      metadata: { source: sourceName },
    };
  }).filter((d) => d.content);
}

/** 读取用户选择的文件为文本。 */
export async function readFile(file) {
  return await file.text();
}
