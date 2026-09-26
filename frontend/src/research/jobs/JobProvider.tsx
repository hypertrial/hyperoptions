import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react"
import { useSearchParams } from "react-router-dom"
import { ApiError, getJson, postJson } from "../api/client"
import type { Job } from "../api/types"
import { jobPollInterval } from "../lib/leaderboard"

const JOB_KEY = "stocksweeper.job"

type Notice = { tone: "success" | "error" | "info"; text: string }

type JobContextValue = {
  job: Job | null
  busy: boolean
  notice: Notice | null
  start: (path: string, body: unknown) => Promise<void>
  dismiss: () => void
}

const JobContext = createContext<JobContextValue | null>(null)

export function JobProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [jobId, setJobId] = useState<string | null>(() => sessionStorage.getItem(JOB_KEY))
  const [starting, setStarting] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const handledJob = useRef<string | null>(null)
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJson<Job>(`/api/jobs/${jobId}`),
    enabled: jobId != null,
    refetchInterval: (query) => jobPollInterval(query.state.data?.state),
    retry: false,
  })

  useEffect(() => {
    if (!(job.error instanceof ApiError) || job.error.status !== 404) return
    sessionStorage.removeItem(JOB_KEY)
    setJobId(null)
    setNotice({ tone: "error", text: job.error.message })
  }, [job.error])

  useEffect(() => {
    const current = job.data
    if (!current || current.state === "queued" || current.state === "running") return
    if (handledJob.current === current.id) return
    handledJob.current = current.id
    sessionStorage.removeItem(JOB_KEY)
    if (current.state === "failed") {
      setNotice({ tone: "error", text: current.error ?? "The job failed." })
      return
    }
    if (current.run_id) {
      const next = new URLSearchParams(params)
      next.set("run", current.run_id)
      next.delete("page")
      setParams(next, { replace: true })
    }
    void queryClient.invalidateQueries({ queryKey: ["runs"] })
    void queryClient.invalidateQueries({ queryKey: ["status"] })
    void queryClient.invalidateQueries({ queryKey: ["overview"] })
    void queryClient.invalidateQueries({ queryKey: ["leaderboard"] })
    const text =
      current.kind === "data"
        ? "Data is updated. Run a backtest so rankings use the new bars."
        : "Backtest finished."
    setNotice({ tone: "success", text })
  }, [job.data, params, queryClient, setParams])

  async function start(path: string, body: unknown) {
    setNotice(null)
    setStarting(true)
    try {
      const created = await postJson<Job>(path, body)
      sessionStorage.setItem(JOB_KEY, created.id)
      handledJob.current = null
      setJobId(created.id)
    } catch (exc) {
      const text = exc instanceof Error ? exc.message : "request failed"
      setNotice({ tone: "error", text })
    } finally {
      setStarting(false)
    }
  }

  const terminal = job.data?.state === "succeeded" || job.data?.state === "failed"
  const busy = starting || (jobId != null && !terminal && !job.isError)
  const value: JobContextValue = {
    job: job.data ?? null,
    busy,
    notice,
    start,
    dismiss: () => setNotice(null),
  }
  return <JobContext.Provider value={value}>{children}</JobContext.Provider>
}

export function useJob(): JobContextValue {
  const value = useContext(JobContext)
  if (!value) throw new Error("useJob must be used inside JobProvider")
  return value
}
