import { useState } from 'react'
import PrForm from './components/PrForm'
import Summary from './components/Summary'
import FileReview from './components/FileReview'
import { fetchReview } from './api'
import './App.css'

// Step 6: the app shell that wires everything together.
// State lives here so the child components stay simple ("props in, UI out").
function App() {
  const [files, setFiles] = useState(null) // null = never reviewed yet
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [current, setCurrent] = useState(null) // {owner, repo, prNumber}

  async function handleReview({ owner, repo, prNumber }) {
    setLoading(true)
    setError(null)
    setFiles(null)
    setCurrent({ owner, repo, prNumber })
    try {
      const data = await fetchReview(owner, repo, prNumber)
      setFiles(Array.isArray(data) ? data : [])
    } catch (err) {
      setError(err.message) // network failure / FastAPI error detail
    } finally {
      setLoading(false) // always runs - success or failure
    }
  }

  return (
    <main className="shell">
      <header className="top">
        <h1>
          CodeTurtle <span>AI PR Reviewer</span>
        </h1>
        <p>Paste a pull request, get per-file findings from the review pipeline.</p>
      </header>

      <PrForm onSubmit={handleReview} loading={loading} />

      {loading && (
        <div className="loading">
          <div className="spinner" />
          <p>
            Reviewing
            {current ? ` ${current.owner}/${current.repo}#${current.prNumber}` : ''}…<br />
            The first request runs the full LLM pipeline (can take a while) - repeat requests
            are served from the cache.
          </p>
        </div>
      )}

      {error && (
        <div className="error-banner">
          <strong>Review failed:</strong> {error}
        </div>
      )}

      {files && !loading && (
        <>
          <Summary files={files} />
          <section className="file-list">
            {files.length === 0 && <p className="empty">No changed files found in this PR.</p>}
            {files.map((file) => (
              <FileReview
                key={file.filename}
                file={file}
                owner={current.owner}
                repo={current.repo}
                prNumber={current.prNumber}
              />
            ))}
          </section>
        </>
      )}
    </main>
  )
}

export default App