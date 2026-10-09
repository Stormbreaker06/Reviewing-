import { useState } from 'react'

// Step 5: one collapsible card per changed file.
// Shows the review badge, +/- deltas, and the issues the LLM found.
export default function FileReview({ file, owner, repo, prNumber }) {
  const [open, setOpen] = useState(false)

  // Defensive: a failed safe_review returns an error dict; older paths returned strings
  const review =
    typeof file.review === 'string' ? { status: 'error', message: file.review } : (file.review ?? {})
  const issues = Array.isArray(review.issues) ? review.issues : []
  const badge = review.status ?? 'error'
  const githubUrl = `https://github.com/${owner}/${repo}/pull/${prNumber}/files`

  return (
    <article className={`file-card status-${badge}`}>
      <button type="button" className="file-head" onClick={() => setOpen(!open)}>
        <span className={`badge badge-${badge}`}>{badge}</span>
        <span className="file-name">{file.filename}</span>
        <span className="file-kind">{file.status}</span>
        <span className="delta">
          <span className="add">+{file.additions ?? 0}</span>{' '}
          <span className="del">-{file.deletions ?? 0}</span>
        </span>
        <span className="chevron">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="file-body">
          {review.message && <p className="file-message">{review.message}</p>}
          {review.status === 'clean' && <p className="file-message clean-note">No issues found</p>}

          {issues.length > 0 && (
            <ul className="issues">
              {issues.map((issue, index) => (
                <li key={index} className="issue">
                  <div className="issue-top">
                    <span className={`sev sev-${String(issue.severity ?? '').toLowerCase()}`}>
                      {issue.severity ?? 'unknown'}
                    </span>
                    <strong>{issue.title}</strong>
                    {issue.line != null && issue.line !== '' && (
                      <span className="line">line {issue.line}</span>
                    )}
                  </div>
                  <p>{issue.description}</p>
                  {issue.fix && (
                    <p className="fix">
                      <span className="fix-label">Fix:</span> {issue.fix}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}

          <a className="gh-link" href={githubUrl} target="_blank" rel="noreferrer">
            View file on GitHub
          </a>
        </div>
      )}
    </article>
  )
}
