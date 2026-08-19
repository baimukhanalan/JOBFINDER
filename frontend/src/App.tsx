import { useEffect, useState } from 'react'
import type { Job, Stats } from './api'
import {
  fetchJobs,
  fetchStats,
  generateCoverLetter,
  getUserCode,
  updateJobStatus,
} from './api'
import ProfileForm from './ProfileForm'

function cleanMarkdown(text: string): string {
  return text
    .replace(/\*\*/g, '')
    .replace(/\\\-/g, '-')
    .replace(/^-{3,}$/gm, '')
    .replace(/^#{1,6}\s/gm, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '')
    .replace(/`{1,3}/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{2,}/g, '\n')
    .replace(/^\s+/gm, '')
    .trim()
}

const STATUS_OPTIONS = ['new', 'saved', 'applied', 'interview', 'offer', 'rejected', 'skipped']
const STATUS_COLORS: Record<string, string> = {
  new: 'bg-blue-500/20 text-blue-400',
  saved: 'bg-yellow-500/20 text-yellow-400',
  applied: 'bg-purple-500/20 text-purple-400',
  interview: 'bg-green-500/20 text-green-400',
  offer: 'bg-emerald-500/20 text-emerald-300',
  rejected: 'bg-red-500/20 text-red-400',
  skipped: 'bg-gray-500/20 text-gray-400',
}

const EQUIPMENT_TABS = [
  { key: '', label: 'All' },
  { key: 'byod', label: 'BYOD' },
  { key: 'corporate', label: 'Corp Laptop' },
  { key: 'stipend', label: 'Stipend' },
  { key: 'unknown', label: 'Unknown' },
]
const EQUIPMENT_COLORS: Record<string, string> = {
  byod: 'bg-green-500/20 text-green-400',
  corporate: 'bg-blue-500/20 text-blue-400',
  stipend: 'bg-yellow-500/20 text-yellow-400',
  unknown: 'bg-gray-500/20 text-gray-400',
}
const EQUIPMENT_LABELS: Record<string, string> = {
  byod: 'BYOD',
  corporate: 'Corp',
  stipend: '$tip',
  unknown: '?',
}

const SPEED_TABS = [
  { key: '', label: 'All' },
  { key: 'fast', label: 'Fast' },
  { key: 'medium', label: 'Medium' },
  { key: 'slow', label: 'Slow' },
  { key: 'unknown', label: '?' },
]
const SPEED_COLORS: Record<string, string> = {
  fast: 'bg-emerald-500/20 text-emerald-400',
  medium: 'bg-yellow-500/20 text-yellow-400',
  slow: 'bg-red-500/20 text-red-400',
  unknown: 'bg-gray-500/20 text-gray-400',
}
const SPEED_LABELS: Record<string, string> = {
  fast: 'Fast',
  medium: 'Med',
  slow: 'Slow',
  unknown: '?',
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-slate-800 rounded-lg p-3">
      <div className="text-xl font-bold">{value}</div>
      <div className="text-xs text-slate-400">{label}</div>
    </div>
  )
}

type Tab = 'jobs' | 'profile'

function App() {
  const userCode = getUserCode()
  const [tab, setTab] = useState<Tab>(userCode ? 'profile' : 'jobs')
  const [jobs, setJobs] = useState<Job[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [total, setTotal] = useState(0)
  const [search, setSearch] = useState('')
  const [source, setSource] = useState('')
  const [country, setCountry] = useState('')
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(false)
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [coverLetterLoading, setCoverLetterLoading] = useState(false)
  const [equipment, setEquipment] = useState('')
  const [hiringSpeed, setHiringSpeed] = useState('')
  const [showFilters, setShowFilters] = useState(false)

  const loadJobs = async () => {
    setLoading(true)
    try {
      const data = await fetchJobs({ search, source, country, status, equipment, hiring_speed: hiringSpeed })
      setJobs(data.jobs)
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }

  const loadStats = async () => {
    const data = await fetchStats()
    setStats(data)
  }

  useEffect(() => {
    loadJobs()
    loadStats()
  }, [search, source, country, status, equipment, hiringSpeed])

  const handleStatusChange = async (jobId: number, newStatus: string) => {
    const updated = await updateJobStatus(jobId, newStatus)
    setJobs(jobs.map((j) => (j.id === jobId ? updated : j)))
    loadStats()
  }

  const handleCoverLetter = async (job: Job) => {
    setCoverLetterLoading(true)
    try {
      const res = await generateCoverLetter(job.id)
      const updated = { ...job, cover_letter: res.cover_letter }
      setJobs(jobs.map((j) => (j.id === job.id ? updated : j)))
      setSelectedJob(updated)
    } finally {
      setCoverLetterLoading(false)
    }
  }

  const activeFilters = [source, country, status].filter(Boolean).length

  return (
    <div className="min-h-screen bg-slate-900 p-3 md:p-6">
      <div className="max-w-7xl mx-auto">

        {/* Header + Tabs */}
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-xl md:text-3xl font-bold">JobFinder</h1>
          <div className="flex gap-1">
            {(['jobs', ...(userCode ? ['profile'] : [])] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${
                  tab === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'
                }`}
              >
                {t === 'jobs' ? 'Jobs' : 'Profile'}
              </button>
            ))}
          </div>
        </div>

        {tab === 'profile' && userCode && <ProfileForm userCode={userCode} />}

        {tab === 'jobs' && <>
        {/* Stats — горизонтальный скролл на мобилке */}
        {stats && (
          <div className="flex gap-2 mb-4 overflow-x-auto pb-1 -mx-3 px-3 md:mx-0 md:px-0 md:grid md:grid-cols-5">
            <div className="flex-shrink-0 md:flex-shrink"><StatCard label="Total" value={stats.total_jobs} /></div>
            <div className="flex-shrink-0 md:flex-shrink"><StatCard label="New" value={stats.new_jobs} /></div>
            <div className="flex-shrink-0 md:flex-shrink"><StatCard label="Applied" value={stats.applied} /></div>
            <div className="flex-shrink-0 md:flex-shrink"><StatCard label="Interviews" value={stats.interviews} /></div>
            <div className="flex-shrink-0 md:flex-shrink"><StatCard label="Offers" value={stats.offers} /></div>
          </div>
        )}

        {/* Filter tabs */}
        <div className="space-y-2 mb-3">
          <div className="flex gap-1.5 overflow-x-auto pb-1 -mx-3 px-3 md:mx-0 md:px-0">
            <span className="text-[10px] text-slate-500 self-center shrink-0 w-8">PC:</span>
            {EQUIPMENT_TABS.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setEquipment(tab.key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap shrink-0 ${
                  equipment === tab.key
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-800 text-slate-400 border border-slate-700'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <div className="flex gap-1.5 overflow-x-auto pb-1 -mx-3 px-3 md:mx-0 md:px-0">
            <span className="text-[10px] text-slate-500 self-center shrink-0 w-8">Hire:</span>
            {SPEED_TABS.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setHiringSpeed(tab.key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap shrink-0 ${
                  hiringSpeed === tab.key
                    ? 'bg-emerald-600 text-white'
                    : 'bg-slate-800 text-slate-400 border border-slate-700'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Search + Filter toggle */}
        <div className="flex gap-2 mb-3">
          <input
            type="text"
            placeholder="Search..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 flex-1 text-sm"
          />
          <button
            onClick={() => setShowFilters(!showFilters)}
            className={`px-3 py-2 rounded-lg text-sm border ${
              activeFilters > 0
                ? 'bg-blue-600 border-blue-500 text-white'
                : 'bg-slate-800 border-slate-700 text-slate-400'
            }`}
          >
            Filters{activeFilters > 0 ? ` (${activeFilters})` : ''}
          </button>
        </div>

        {/* Collapsible filters */}
        {showFilters && (
          <div className="grid grid-cols-3 gap-2 mb-4">
            <select
              value={source}
              onChange={(e) => setSource(e.target.value)}
              className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-sm"
            >
              <option value="">Source</option>
              <option value="indeed">Indeed</option>
              <option value="remoteok">RemoteOK</option>
              <option value="weworkremotely">WWR</option>
            </select>
            <select
              value={country}
              onChange={(e) => setCountry(e.target.value)}
              className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-sm"
            >
              <option value="">Country</option>
              <option value="US">US</option>
              <option value="CA">Canada</option>
            </select>
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-sm"
            >
              <option value="">Status</option>
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        )}

        {/* Results count */}
        <div className="text-slate-500 text-xs mb-3">{total} jobs</div>

        {/* Job List */}
        {loading ? (
          <div className="text-center py-12 text-slate-400">Loading...</div>
        ) : (
          <div className="space-y-2">
            {jobs.map((job) => (
              <div
                key={job.id}
                className="bg-slate-800 rounded-lg p-3 active:bg-slate-700 cursor-pointer border border-slate-700/50"
                onClick={() => setSelectedJob(job)}
              >
                {/* Mobile-first: всё вертикально */}
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <h3 className="font-semibold text-sm leading-tight truncate">{job.title}</h3>
                    <div className="text-slate-400 text-xs mt-0.5 truncate">
                      {job.company} · {job.country}
                    </div>
                  </div>
                  <div className="flex gap-1 shrink-0">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${SPEED_COLORS[job.hiring_speed] || SPEED_COLORS.unknown}`}>
                      {SPEED_LABELS[job.hiring_speed] || '?'}
                    </span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${EQUIPMENT_COLORS[job.equipment] || EQUIPMENT_COLORS.unknown}`}>
                      {EQUIPMENT_LABELS[job.equipment] || '?'}
                    </span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${STATUS_COLORS[job.status]}`}>
                      {job.status}
                    </span>
                  </div>
                </div>

                {job.salary_text && (
                  <div className="text-green-400 text-xs mt-1">{job.salary_text}</div>
                )}

                <div className="flex items-center justify-between mt-2">
                  <div className="flex gap-1 overflow-hidden">
                    <span className="text-[10px] bg-slate-700 px-1.5 py-0.5 rounded shrink-0">{job.source}</span>
                    {job.tags?.split(', ').slice(0, 2).map((tag) => (
                      <span key={tag} className="text-[10px] bg-slate-700 px-1.5 py-0.5 rounded truncate">{tag}</span>
                    ))}
                  </div>
                  <div className="flex gap-1.5 shrink-0" onClick={(e) => e.stopPropagation()}>
                    <select
                      value={job.status}
                      onChange={(e) => handleStatusChange(job.id, e.target.value)}
                      className="bg-slate-700 border border-slate-600 rounded px-1 py-0.5 text-[10px] max-w-20"
                    >
                      {STATUS_OPTIONS.map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="bg-blue-600 active:bg-blue-800 px-2 py-0.5 rounded text-[10px]"
                    >
                      Open
                    </a>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Job Detail — fullscreen на мобилке */}
        {selectedJob && (
          <div className="fixed inset-0 bg-black/70 z-50 flex items-end md:items-center justify-center">
            <div className="bg-slate-800 rounded-t-2xl md:rounded-xl w-full md:max-w-2xl max-h-[90vh] overflow-y-auto">

              {/* Drag handle на мобилке */}
              <div className="flex justify-center pt-2 pb-1 md:hidden">
                <div className="w-10 h-1 bg-slate-600 rounded-full" />
              </div>

              <div className="p-4 md:p-6">
                <div className="flex justify-between items-start mb-3">
                  <div className="flex-1 min-w-0 pr-4">
                    <h2 className="text-lg font-bold leading-tight">{selectedJob.title}</h2>
                    <p className="text-slate-400 text-sm mt-0.5">{selectedJob.company} · {selectedJob.country}</p>
                  </div>
                  <button
                    onClick={() => setSelectedJob(null)}
                    className="text-slate-400 active:text-white text-xl w-8 h-8 flex items-center justify-center rounded-full bg-slate-700"
                  >
                    x
                  </button>
                </div>

                {selectedJob.salary_text && (
                  <div className="text-green-400 text-sm mb-3">{selectedJob.salary_text}</div>
                )}

                {selectedJob.description && (
                  <div className="text-sm text-slate-300 mb-4 whitespace-pre-line leading-snug max-h-60 overflow-y-auto">
                    {cleanMarkdown(selectedJob.description.slice(0, 2000))}
                  </div>
                )}

                {/* Кнопки — full width на мобилке */}
                <div className="grid grid-cols-2 gap-2 mb-4">
                  <a
                    href={selectedJob.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="bg-blue-600 active:bg-blue-800 px-4 py-3 rounded-lg text-center text-sm font-medium"
                  >
                    Apply
                  </a>
                  <button
                    onClick={() => handleCoverLetter(selectedJob)}
                    disabled={coverLetterLoading}
                    className="bg-purple-600 active:bg-purple-800 disabled:opacity-50 px-4 py-3 rounded-lg text-sm font-medium"
                  >
                    {coverLetterLoading ? '...' : 'Cover Letter'}
                  </button>
                </div>

                <div className="mb-4" onClick={(e) => e.stopPropagation()}>
                  <select
                    value={selectedJob.status}
                    onChange={(e) => {
                      handleStatusChange(selectedJob.id, e.target.value)
                      setSelectedJob({ ...selectedJob, status: e.target.value })
                    }}
                    className="bg-slate-700 border border-slate-600 rounded-lg px-3 py-2 text-sm w-full"
                  >
                    {STATUS_OPTIONS.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </div>

                {selectedJob.cover_letter && (
                  <div className="bg-slate-900 rounded-lg p-3">
                    <h3 className="font-semibold text-sm mb-2">Cover Letter</h3>
                    <div className="text-xs whitespace-pre-wrap leading-relaxed">{selectedJob.cover_letter}</div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        </>}
      </div>
    </div>
  )
}

export default App
