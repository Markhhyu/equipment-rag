const NODE_LABELS: Record<string, string> = {
  node_item_name_confirm: '确认设备型号',
  node_search_embedding: '检索知识库',
  node_search_embedding_hyde: '扩展检索问题',
  node_web_search_mcp: '补充外部资料',
  node_query_kg: '查询知识图谱',
  node_rrf: '融合检索结果',
  node_rerank: '重排参考资料',
  node_image_reasoning: '分析相关图片',
  node_answer_output: '组织最终回答',
  upload_file: '保存上传文件',
}

export function formatNodeName(value: string): string {
  return NODE_LABELS[value] ?? value.replace(/^node_/, '').replaceAll('_', ' ')
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function formatTime(value?: number | string): string {
  if (!value) return ''
  const numeric = typeof value === 'number' ? value * 1000 : value
  const date = new Date(numeric)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
