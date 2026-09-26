import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { Shell } from "./components/Shell"
import { JobProvider } from "./jobs/JobProvider"

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})

export default function ResearchApp() {
  return (
    <QueryClientProvider client={queryClient}>
      <JobProvider>
        <Shell />
      </JobProvider>
    </QueryClientProvider>
  )
}
