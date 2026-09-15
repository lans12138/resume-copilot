import { useMutation } from "@tanstack/react-query"
import { useState, type FormEvent } from "react"
import { Navigate, useLocation, useNavigate } from "react-router-dom"
import { api } from "../api/client"
import { ErrorNotice } from "../components/Feedback"
import { useAppStore } from "../state/session"

export function LoginPage() {
  const token = useAppStore((state) => state.accessToken)
  const setSession = useAppStore((state) => state.setSession)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [persist, setPersist] = useState(true)
  const navigate = useNavigate()
  const location = useLocation()
  const mutation = useMutation({
    mutationFn: async () => {
      const issued = await api.login(username, password)
      const user = await api.me(issued.access_token)
      return { token: issued.access_token, user }
    },
    onSuccess: ({ token: nextToken, user }) => {
      setSession(nextToken, user, persist)
      const from = (location.state as { from?: string } | null)?.from
      navigate(from?.startsWith("/") ? from : "/jobs", { replace: true })
    },
  })

  if (token) return <Navigate to="/jobs" replace />
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); mutation.mutate() }

  return <main className="login-shell">
    <section className="login-story" aria-labelledby="login-heading">
      <p className="eyebrow">Resume Copilot</p>
      <h1 id="login-heading">把招聘判断，沉淀为团队共识。</h1>
      <p>从岗位版本到候选人评估，在一个可追踪、可解释的工作台里推进。</p>
      <div className="story-metric"><strong>01</strong><span>岗位先行<br />清晰定义人才标准</span></div>
    </section>
    <section className="login-panel" aria-label="账号登录">
      <div><p className="eyebrow">欢迎回来</p><h2>登录工作台</h2><p className="muted">使用已开通的企业账号继续。</p></div>
      {mutation.error ? <ErrorNotice error={mutation.error} /> : null}
      <form className="form-stack" onSubmit={submit}>
        <label htmlFor="username">用户名</label>
        <input id="username" name="username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus />
        <label htmlFor="password">密码</label>
        <input id="password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        <label className="check-row"><input type="checkbox" checked={persist} onChange={(event) => setPersist(event.target.checked)} />仅在本次浏览器会话中保持登录</label>
        <button className="button button-primary button-wide" type="submit" disabled={mutation.isPending}>{mutation.isPending ? "正在登录…" : "进入工作台"}</button>
      </form>
      <small className="security-note">凭证不会保存在本地长期存储中。</small>
    </section>
  </main>
}
