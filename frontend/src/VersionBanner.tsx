import { useEffect, useState } from "react"

type VersionStatus = {
  running_sha: string | null
  remote_sha: string | null
  status: "current" | "update_available" | "offline" | "unverified_checkout"
  frontend_matches: boolean | null
}

export default function VersionBanner() {
  const frontendSha = import.meta.env.VITE_APP_SHA as string | undefined
  const [status, setStatus] = useState<VersionStatus | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!frontendSha) return
    let active = true
    const poll = async () => {
      if (document.hidden) return
      try {
        const response = await fetch(`/api/version?frontend_sha=${encodeURIComponent(frontendSha)}`, {
          signal: AbortSignal.timeout(12_000),
        })
        if (!response.ok) throw new Error("Version check failed")
        const result = await response.json() as VersionStatus
        if (!result || !["current", "update_available", "offline", "unverified_checkout"].includes(result.status)) throw new Error("Invalid version status")
        if (active) { setStatus(result); setFailed(false) }
      } catch {
        if (active) setFailed(true)
      }
    }
    void poll()
    const timer = window.setInterval(() => { void poll() }, 60_000)
    document.addEventListener("visibilitychange", poll)
    return () => { active = false; window.clearInterval(timer); document.removeEventListener("visibilitychange", poll) }
  }, [frontendSha])

  let message: string | null = null
  if (!frontendSha) message = "Version unverified. Start with ./scripts/dev."
  else if (status?.frontend_matches === false) message = "Frontend and backend versions differ. Restart ./scripts/dev to load the same revision."
  else if (status?.status === "update_available") message = "An update is available on origin/main. Restart ./scripts/dev to apply it."
  else if (status?.status === "offline") message = "Version unverified: origin/main could not be reached. You can keep using this local revision."
  else if (status?.status === "unverified_checkout") message = "Version unverified: this checkout is not a verified clean main launch."
  else if (failed) message = "Version unverified: the local version check failed."

  return message ? <p className="version-banner" role="status">{message}</p> : null
}
