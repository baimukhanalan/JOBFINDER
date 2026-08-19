import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

export interface Job {
  id: number
  title: string
  company: string
  url: string
  salary_min: number | null
  salary_max: number | null
  salary_text: string | null
  location: string
  country: string
  source: string
  description: string | null
  tags: string | null
  score: number | null
  equipment: string
  hiring_speed: string
  status: string
  cover_letter: string | null
  applied_at: string | null
  created_at: string
}

export interface Stats {
  total_jobs: number
  new_jobs: number
  applied: number
  interviews: number
  offers: number
  by_source: Record<string, number>
  by_country: Record<string, number>
}

export async function fetchJobs(params: Record<string, string | number | undefined>) {
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== '')
  )
  const res = await api.get('/jobs', { params: clean })
  return res.data as { jobs: Job[]; total: number }
}

export async function fetchStats() {
  const res = await api.get('/stats')
  return res.data as Stats
}

export async function updateJobStatus(jobId: number, status: string) {
  const res = await api.patch(`/jobs/${jobId}/status`, { status })
  return res.data as Job
}

export async function generateCoverLetter(jobId: number, resumeSummary: string = '') {
  const res = await api.post(`/jobs/${jobId}/cover-letter`, { resume_summary: resumeSummary })
  return res.data as { cover_letter: string }
}

// === User code from URL ===

export function getUserCode(): string | null {
  const params = new URLSearchParams(window.location.search)
  return params.get('u')
}

// === Auto-Apply API ===

export interface UserProfile {
  id?: number
  user_code?: string
  full_name: string
  email: string
  phone: string
  location: string
  linkedin_url: string | null
  resume_path: string | null
  resume_text: string | null
  years_experience: number
  desired_salary: string | null
  work_authorization: string
  available_start: string
}

export interface QueueEntry {
  id: number
  job_id: number
  status: string
  priority: number
  attempts: number
  error_message: string | null
  title: string
  company: string
  url: string
  created_at: string | null
}

export interface QuestionEntry {
  id: number
  question_text: string
  answer_text: string
  question_type: string
  times_used: number
}

export async function fetchProfile(userCode: string) {
  const res = await api.get('/apply/profile', { params: { u: userCode } })
  return res.data as UserProfile | null
}

export async function saveProfile(data: Omit<UserProfile, 'id' | 'user_code'>, userCode: string) {
  const res = await api.post('/apply/profile', data, { params: { u: userCode } })
  return res.data as UserProfile
}

export async function uploadResume(file: File, userCode: string) {
  const form = new FormData()
  form.append('file', file)
  const res = await api.post('/apply/resume', form, { params: { u: userCode } })
  return res.data as { path: string; text: string }
}

export async function fetchQueue(userCode: string) {
  const res = await api.get('/apply/queue', { params: { u: userCode } })
  return res.data as QueueEntry[]
}

export async function addToQueue(jobIds: number[], userCode: string) {
  const res = await api.post('/apply/queue', { job_ids: jobIds }, { params: { u: userCode } })
  return res.data as { added: number; total_requested: number }
}

export async function addAllToQueue(userCode: string) {
  const res = await api.post('/apply/queue/all', null, { params: { u: userCode } })
  return res.data as { added: number }
}

export async function removeFromQueue(queueId: number, userCode: string) {
  await api.delete(`/apply/queue/${queueId}`, { params: { u: userCode } })
}

export async function fetchQueueStats(userCode: string) {
  const res = await api.get('/apply/queue/stats', { params: { u: userCode } })
  return res.data as Record<string, number>
}

export async function fetchQuestions() {
  const res = await api.get('/apply/questions')
  return res.data as QuestionEntry[]
}

export async function addQuestion(question_text: string, answer_text: string) {
  const res = await api.post('/apply/questions', { question_text, answer_text })
  return res.data as QuestionEntry
}

export async function deleteQuestion(questionId: number) {
  await api.delete(`/apply/questions/${questionId}`)
}

