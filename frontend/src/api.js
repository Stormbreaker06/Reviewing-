// Every API call goes through here - one place to know where the backend lives.
// Override at build/dev time:  VITE_API_BASE=http://1.2.3.4:8000 npm run dev
const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

// GET /github/review/{owner}/{repo}/{pr}/review  ->  array of file entries
export async function fetchReview(owner, repo, prNumber) {
  const url =
    `${API_BASE}/github/review` +
    `/${encodeURIComponent(owner)}` +
    `/${encodeURIComponent(repo)}` +
    `/${encodeURIComponent(prNumber)}` +
    `/review`

  const response = await fetch(url)

  // FastAPI errors carry { detail: "..." }; surface them instead of "HTTP 500"
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      // body wasn't JSON - keep the status code
    }
    throw new Error(detail)
  }

  return response.json()
}

// "https://github.com/owner/repo/pull/123"  ->  { owner, repo, prNumber }
export function parsePrUrl(input) {
  const match = String(input).match(/github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/(\d+)/)
  if (!match) return null
  return {
    owner: match[1],
    repo: match[2].replace(/\.git$/, ''),
    prNumber: match[3],
  }
}
