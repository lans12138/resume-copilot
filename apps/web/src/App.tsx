import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom"
import { useState } from "react"
import { useAppStore } from "./state/session"
import { ModelModeBanner } from "./components/ModelModeBanner"
import { CandidatesPage } from "./pages/CandidatesPage"
import { CandidateDetailPage } from "./pages/CandidateDetailPage"
import { DocumentDetailPage } from "./pages/DocumentDetailPage"
import { DocumentReviewPage } from "./pages/DocumentReviewPage"
import { DocumentsPage } from "./pages/DocumentsPage"
import { EvaluationsPage } from "./pages/EvaluationsPage"
import { JobDetailPage } from "./pages/JobDetailPage"
import { JobsPage } from "./pages/JobsPage"
import { LoginPage } from "./pages/LoginPage"
import { MatchRunPage } from "./pages/MatchRunPage"
import { ApplicationRunPage } from "./pages/ApplicationRunPage"
import { ApprovalPage } from "./pages/ApprovalPage"
import { InterviewPage } from "./pages/InterviewPage"

export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 15_000 } } })

function AuthGuard() {
  const accessToken = useAppStore((state) => state.accessToken)
  const location = useLocation()
  return accessToken ? <Outlet /> : <Navigate to="/login" replace state={{ from: location.pathname }} />
}

function WorkspaceLayout() {
  const user = useAppStore((state) => state.currentUser)
  const clearSession = useAppStore((state) => state.clearSession)
  const navigate = useNavigate()
  function logout() { clearSession(); queryClient.clear(); navigate("/login", { replace: true }) }
  return <div className="workspace-shell">
    <header className="topbar">
      <NavLink className="brand" to="/jobs" aria-label="Resume Copilot 岗位工作台">
        <span className="brand-mark">RC</span><span><strong>Resume Copilot</strong><small>招聘协作工作台</small></span>
      </NavLink>
      <nav className="topbar-nav" aria-label="主导航">
        <NavLink to="/jobs">岗位</NavLink>
        <NavLink to="/documents">简历文档</NavLink>
        <NavLink to="/candidates">候选人</NavLink>
        <NavLink to="/evaluations">评测</NavLink>
      </nav>
      <div className="user-menu"><span><strong>{user?.username}</strong><small>{user?.role === "HIRING_MANAGER" ? "招聘主管" : user?.role}</small></span><button className="button button-ghost button-small" type="button" onClick={logout}>退出登录</button></div>
    </header>
    {/* PORT-005: the model mode is a property of the whole deployment, so it is stated
        once here rather than repeated on every page that shows a model result. */}
    <ModelModeBanner />
    <main className="workspace-content"><Outlet /></main>
  </div>
}

export function AppRoutes() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route element={<AuthGuard />}><Route element={<WorkspaceLayout />}>
      <Route path="/jobs" element={<JobsPage />} />
      <Route path="/jobs/:jobId" element={<JobDetailPage />} />
      <Route path="/match-runs/:runId" element={<MatchRunPage />} />
      <Route path="/application-runs/:runId" element={<ApplicationRunPage />} />
      <Route path="/approvals/:approvalId" element={<ApprovalPage />} />
      <Route path="/interviews/:interviewId" element={<InterviewPage />} />
      <Route path="/documents" element={<DocumentsPage />} />
      <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
      <Route path="/documents/:documentId/review" element={<DocumentReviewPage />} />
      <Route path="/candidates" element={<CandidatesPage />} />
      <Route path="/candidates/:profileId" element={<CandidateDetailPage />} />
      <Route path="/evaluations" element={<EvaluationsPage />} />
    </Route></Route>
    <Route path="*" element={<Navigate to="/jobs" replace />} />
  </Routes>
}

export function App() {
  const [client] = useState(() => queryClient)
  return <QueryClientProvider client={client}><BrowserRouter><AppRoutes /></BrowserRouter></QueryClientProvider>
}
