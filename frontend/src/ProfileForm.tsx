import { useEffect, useState } from 'react'
import type { UserProfile } from './api'
import { fetchProfile, saveProfile, uploadResume } from './api'

const EMPTY_PROFILE: Omit<UserProfile, 'id' | 'user_code'> = {
  full_name: '',
  email: '',
  phone: '',
  location: 'Remote, US',
  linkedin_url: null,
  resume_path: null,
  resume_text: null,
  years_experience: 5,
  desired_salary: null,
  work_authorization: 'Authorized to work in US',
  available_start: 'Immediately',
}

export default function ProfileForm({ userCode }: { userCode: string }) {
  const [form, setForm] = useState<Omit<UserProfile, 'id' | 'user_code'>>(EMPTY_PROFILE)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    fetchProfile(userCode).then((p) => {
      if (p && p.full_name) {
        const { id, user_code, ...rest } = p as UserProfile & { id: number; user_code: string }
        void id; void user_code
        setForm(rest)
      }
    })
  }, [userCode])

  const set = (key: keyof typeof form, value: string | number | null) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  const handleSave = async () => {
    if (!form.full_name || !form.email || !form.phone) {
      setError('Name, email, and phone are required')
      return
    }
    setError('')
    setSaving(true)
    try {
      await saveProfile(form, userCode)
      setSaved(true)
    } catch {
      setError('Failed to save profile')
    } finally {
      setSaving(false)
    }
  }

  const handleResume = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const res = await uploadResume(file, userCode)
      setForm((prev) => ({ ...prev, resume_path: res.path, resume_text: res.text }))
      setSaved(false)
    } catch {
      setError('Failed to upload resume')
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="max-w-xl mx-auto">
      <h2 className="text-lg font-bold mb-4">Applicant Profile</h2>

      {error && (
        <div className="bg-red-500/20 text-red-400 px-3 py-2 rounded-lg text-sm mb-4">{error}</div>
      )}

      <div className="space-y-3">
        <Field label="Full Name *" value={form.full_name} onChange={(v) => set('full_name', v)} />
        <Field label="Email *" value={form.email} onChange={(v) => set('email', v)} type="email" />
        <Field label="Phone *" value={form.phone} onChange={(v) => set('phone', v)} type="tel" />
        {/* Available Start = always Immediately, Location = Remote US/Canada, Salary = adaptive, LinkedIn = none, Experience = adaptive (default 5), Work Auth = always authorized */}

        {/* Resume upload */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">Resume (PDF)</label>
          <div className="flex items-center gap-2">
            <label className="bg-slate-700 border border-slate-600 rounded-lg px-3 py-2 text-sm cursor-pointer hover:bg-slate-600">
              {uploading ? 'Uploading...' : form.resume_path ? 'Change file' : 'Upload PDF'}
              <input type="file" accept=".pdf" onChange={handleResume} className="hidden" disabled={uploading} />
            </label>
            {form.resume_path && (
              <span className="text-xs text-green-400">Uploaded</span>
            )}
          </div>
        </div>

        {/* Resume text preview */}
        {form.resume_text && (
          <div>
            <label className="block text-xs text-slate-400 mb-1">Resume Text (extracted)</label>
            <textarea
              value={form.resume_text}
              onChange={(e) => set('resume_text', e.target.value)}
              rows={4}
              className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm w-full resize-y"
            />
          </div>
        )}

        <button
          onClick={handleSave}
          disabled={saving}
          className={`w-full py-3 rounded-lg text-sm font-medium ${
            saved
              ? 'bg-green-600 text-white'
              : 'bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white'
          } disabled:opacity-50`}
        >
          {saving ? 'Saving...' : saved ? 'Saved!' : 'Save Profile'}
        </button>
      </div>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
}) {
  return (
    <div>
      <label className="block text-xs text-slate-400 mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm w-full"
      />
    </div>
  )
}
