// Only a small Markdown table grammar; Vue renders every cell as text (no HTML).
export function messageBlocks(content) {
  const lines = content.split('\n')
  const blocks = []
  const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
  const isSeparator = line => line && cells(line).length >= 2 && cells(line).every(cell => /^:?-{3,}:?$/.test(cell))
  let prose = []
  const flush = () => {
    if (prose.length) blocks.push({ type: 'text', text: prose.join('\n') })
    prose = []
  }
  for (let index = 0; index < lines.length; index++) {
    if (lines[index].includes('|') && isSeparator(lines[index + 1])) {
      const header = cells(lines[index])
      if (header.length !== cells(lines[index + 1]).length) {
        prose.push(lines[index])
        continue
      }
      flush()
      const rows = []
      index++
      while (index + 1 < lines.length && lines[index + 1].includes('|') && cells(lines[index + 1]).length === header.length) {
        rows.push(cells(lines[++index]))
      }
      blocks.push({ type: 'table', header, rows })
    } else prose.push(lines[index])
  }
  flush()
  return blocks
}
