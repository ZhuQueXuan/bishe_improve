// Only change the visible answer. Original content and API evidence stay intact.
export const visibleMessageContent = (message) => {
  const content = message.content || ''
  if (message.role !== 'assistant') return content

  const sourceHeading = '\n\n资料出处（原始内容尚待医学审核）：'
  const headingIndex = content.indexOf(sourceHeading)
  const answer = headingIndex === -1 ? content : content.slice(0, headingIndex)
  // The source panel already presents these locations. Remove only the exact
  // redundant graph-location wrapper, after matching its line (and any E ID)
  // to this response's public sources; keep medical text and citations intact.
  const graphSources = (message.sources || []).filter(source => source.kind === 'graph')
  const redundant = /[（(](?:\[(E\d{1,4})\][，,]\s*)?medical\.json\s*第(\d+)行[，,]\s*字段\s+(?:symptom|check|desc|prevent|acompany|cure_way|common_drug|recommand_drug|do_eat|not_eat|recommand_eat)[）)]/g
  return answer.replace(redundant, (whole, evidenceId, line) => {
    const verified = graphSources.some(source => source.line === Number(line) && (!evidenceId || source.evidence_id === evidenceId))
    return verified ? (evidenceId ? `[${evidenceId}]` : '') : whole
  }).trimEnd()
}
