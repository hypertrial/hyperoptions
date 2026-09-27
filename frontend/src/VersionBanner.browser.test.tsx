// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import VersionBanner from "./VersionBanner"

const SHA = "a".repeat(40)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it.each([
  ["update_available", true, "An update is available"],
  ["offline", true, "Version unverified: origin/main could not be reached"],
  ["current", false, "Frontend and backend versions differ"],
  ["unverified_checkout", true, "not a verified clean main launch"],
] as const)("shows %s or mismatch without restarting running code", async (status, frontendMatches, message) => {
  vi.stubEnv("VITE_APP_SHA", SHA)
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({
    running_sha: SHA, remote_sha: null, status, frontend_matches: frontendMatches,
  })))
  vi.stubGlobal("fetch", fetchMock)
  render(<VersionBanner />)
  expect((await screen.findByRole("status")).textContent).toContain(message)
  expect(fetchMock).toHaveBeenCalledWith(`/api/version?frontend_sha=${SHA}`, expect.anything())
})

it("hides the banner for a verified current frontend and backend", async () => {
  vi.stubEnv("VITE_APP_SHA", SHA)
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    running_sha: SHA, remote_sha: SHA, status: "current", frontend_matches: true,
  }))))
  render(<VersionBanner />)
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull())
})

it("polls for a newer revision without changing the running page", async () => {
  vi.useFakeTimers()
  vi.stubEnv("VITE_APP_SHA", SHA)
  let status = "current"
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({
    running_sha: SHA, remote_sha: status === "current" ? SHA : "b".repeat(40), status, frontend_matches: true,
  })))
  vi.stubGlobal("fetch", fetchMock)
  render(<VersionBanner />)
  await act(async () => { await Promise.resolve() })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  status = "update_available"
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.getByRole("status").textContent).toContain("Restart ./scripts/dev")
})
