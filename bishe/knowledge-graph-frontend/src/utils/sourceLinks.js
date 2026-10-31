import { API_BASE_URL } from '../config/api'

export const documentLink = (source) => {
  if (source.kind !== 'document' || !/^[a-f0-9]{64}$/.test(source.version_id || '') ||
      !Number.isInteger(source.page) || source.page < 1) return ''
  return `${API_BASE_URL}/documents/${source.version_id}/file/#page=${source.page}`
}

export const officialLink = (source) => {
  try {
    const url = new URL(source.source_url)
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}
