// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import WatchButton from "./WatchButton"

afterEach(cleanup)

it("shows the disabled contract reason in visible desktop text", () => {
  render(<WatchButton
    contractLabel="IREN 2026-09-18 $50 strike"
    watchKey={null}
    watchabilityReason="Adjusted option root"
    state={undefined}
    onWatch={vi.fn()}
  />)

  const button = screen.getByRole("button", { name: /Adjusted option root/ })
  expect(button.hasAttribute("disabled")).toBe(true)
  expect(screen.getByText("Adjusted option root").classList.contains("watch-action-note")).toBe(true)
})
