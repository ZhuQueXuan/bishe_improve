// Only change the visible answer. Original content and API evidence stay intact.
export const visibleMessageContent = (message) => {
  const content = message.content || ''
  if (message.role !== 'assistant') return content

  const sourceHeading = '\n\n资料出处（原始内容尚待医学审核）：'
  const headingIndex = content.indexOf(sourceHeading)
  const answer = headingIndex === -1 ? content : content.slice(0, headingIndex)
  return answer
    .replace(/[ \t]*\[\s*E\d+(?:\s*[,，、]\s*E\d+)*\s*\]/gi, '')
    .trimEnd()
}
