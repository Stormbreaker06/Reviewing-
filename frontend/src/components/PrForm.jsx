import { useState } from 'react'
import { parsePrUrl } from '../api'

// Step 3: the input form.
// It keeps its own field state and calls onSubmit({owner, repo, prNumber}).
// Paste a PR URL and it auto-fills the three fields for convenience.
export default function PrForm({ onSubmit, loading }) {
  const [owner, setOwner] = useState('')
  const [repo, setRepo] = useState('')
  const [prNumber, setPrNumber] = useState('')
  const [url, setUrl] = useState('')
  const [note, setNote] = useState('')

  function handleUrl(value) {
    setUrl(value)
    const parsed = parsePrUrl(value)
    if (parsed) {
      setOwner(parsed.owner)
      setRepo(parsed.repo)
      setPrNumber(parsed.prNumber)
      setNote('Detected from URL - feel free to edit the fields below')
    } else {
      setNote('')
    }
  }

  function handleSubmit(event) {
    event.preventDefault() // stop the page from reloading
    if (!owner.trim() || !repo.trim() || !prNumber.trim() || loading) return
    onSubmit({ owner: owner.trim(), repo: repo.trim(), prNumber: prNumber.trim() })
  }

  const ready = owner.trim() && repo.trim() && prNumber.trim()

  return (
    <form className="pr-form" onSubmit={handleSubmit}>
      <label className="url-field">
        <span>Paste a GitHub PR URL</span>
        <input
          type="text"
          value={url}
          placeholder="https://github.com/owner/repo/pull/123"
          onChange={(event) => handleUrl(event.target.value)}
        />
      </label>
      {note && <p className="hint">{note}</p>}

      <div className="field-row">
        <label>
          <span>Owner</span>
          <input
            value={owner}
            placeholder="octocat"
            onChange={(event) => setOwner(event.target.value)}
          />
        </label>
        <label>
          <span>Repository</span>
          <input
            value={repo}
            placeholder="hello-world"
            onChange={(event) => setRepo(event.target.value)}
          />
        </label>
        <label>
          <span>PR number</span>
          <input
            type="number"
            min="1"
            value={prNumber}
            placeholder="1347"
            onChange={(event) => setPrNumber(event.target.value)}
          />
        </label>
      </div>

      <button type="submit" disabled={!ready || loading}>
        {loading ? 'Reviewing…' : 'Review PR'}
      </button>
    </form>
  )
}
