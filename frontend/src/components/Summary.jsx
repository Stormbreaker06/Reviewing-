// Step 4: one-line stats above the results.
// Reduces the file array to counts in a single pass (Array.reduce).
export default function Summary({ files }) {
  const stats = files.reduce(
    (acc, file) => {
      acc.total += 1
      const review = typeof file.review === 'string' ? { status: 'error' } : (file.review ?? {})
      if (review.status === 'reviewed') {
        acc.withIssues += 1
        for (const issue of review.issues ?? []) {
          const sev = String(issue.severity ?? '').toLowerCase()
          if (sev === 'high') acc.high += 1
          else if (sev === 'medium') acc.medium += 1
          else acc.low += 1 // low + anything unexpected
        }
      } else if (review.status === 'clean') acc.clean += 1
      else if (review.status === 'skipped') acc.skipped += 1
      else acc.errors += 1
      return acc
    },
    { total: 0, high: 0, medium: 0, low: 0, clean: 0, skipped: 0, errors: 0 },
  )

  return (
    <div className="summary">
      <span className="chip">{stats.total} files</span>
      <span className="chip chip-high">{stats.high} high</span>
      <span className="chip chip-medium">{stats.medium} medium</span>
      <span className="chip chip-low">{stats.low} low</span>
      <span className="chip chip-clean">{stats.clean} clean</span>
      <span className="chip chip-skip">{stats.skipped} skipped</span>
      {stats.errors > 0 && <span className="chip chip-error">{stats.errors} failed</span>}
    </div>
  )
}
