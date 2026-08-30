# Literature 架构与不变量

## 分层

```text
PDF 输入
  |
  v
MinerU 服务适配层 --------> ocr/raw/mineru/（只读证据）
  |
  v
规范化器 -----------------> document.json（主数据）
  |                           source.md（投影）
  |                           assets/（资源）
  +----------+---------------+----------------+
             |                                |
             v                                v
      StructurePatch                    TranslationLayer
        可逆叠加                         block/cell ID 挂接
             |                                |
             +---------------+----------------+
                             v
                        派生导出器
                 EPUB / DOCX / HTML / MD+JSON
                             |
                             +----> 可选外部 TBL
```

## 必须长期保持的不变量

1. MinerU raw 文件在封存后不得修改；通过 `ocr/raw/manifest.json` 的 SHA-256 复核。
2. `document.json` 保存 MinerU 原始字段和资源映射，不以 Markdown 作为唯一事实来源。
3. block/cell ID 在同一份封存 raw 上重复规范化时保持稳定。
4. 结构校准只写可逆补丁；正文删除、合并和改写不得自动执行。
5. 翻译通过 block/cell ID 挂接，不把译文写回 source block。
6. 人工编辑的译文自动锁定；批量任务默认跳过锁定项。
7. 导出器不得读取 `source/origin.pdf` 生成内容。
8. API key、MinerU Token 和会话令牌不得进入项目、日志或导出。
9. 本地 API 只绑定 loopback；除健康检查外均需会话令牌。
10. TBL 只消费派生 EPUB/DOCX，不拥有或覆盖 Literature 文档图谱。

## UI 工作区

- **结构校对**：MinerU 式左/中/右联动。左侧章节与任务，中间 PDF 页及 bbox，右侧结构块、Markdown 投影和 JSON/raw。
- **阅读与翻译**：章节范围、原文、译文三栏；原页可返回；选段即时翻译可选择保存。
- **导出**：选择 source/translation/bilingual 和派生格式；展示验证结果与 TBL 接力入口。
