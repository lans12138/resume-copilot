import { create } from "zustand"
import type { CurrentUser, JobStatus } from "../api/types"

const SESSION_KEY = "resume-copilot.session"
interface StoredSession { token: string; user: CurrentUser }

function loadSession(): StoredSession | null {
  if (typeof window === "undefined") return null
  const raw = window.sessionStorage.getItem(SESSION_KEY)
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<StoredSession>
    return value.token && value.user ? value as StoredSession : null
  } catch {
    window.sessionStorage.removeItem(SESSION_KEY)
    return null
  }
}

const initialSession = loadSession()
interface AppState {
  accessToken: string | null
  currentUser: CurrentUser | null
  jobStatusFilter: JobStatus | "ALL"
  sidebarOpen: boolean
  setSession: (token: string, user: CurrentUser, persist: boolean) => void
  clearSession: () => void
  setJobStatusFilter: (status: JobStatus | "ALL") => void
  setSidebarOpen: (open: boolean) => void
}

export const useAppStore = create<AppState>((set) => ({
  accessToken: initialSession?.token ?? null,
  currentUser: initialSession?.user ?? null,
  jobStatusFilter: "ALL",
  sidebarOpen: false,
  setSession: (token, user, persist) => {
    if (persist) window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token, user }))
    else window.sessionStorage.removeItem(SESSION_KEY)
    set({ accessToken: token, currentUser: user })
  },
  clearSession: () => {
    window.sessionStorage.removeItem(SESSION_KEY)
    set({ accessToken: null, currentUser: null })
  },
  setJobStatusFilter: (jobStatusFilter) => set({ jobStatusFilter }),
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
}))
