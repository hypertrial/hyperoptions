import { spawnSync } from "node:child_process"
import { readFileSync, readdirSync } from "node:fs"
import { fileURLToPath } from "node:url"

const root = fileURLToPath(new URL("../", import.meta.url))

function snapshot() {
  const names = [
    "openapi.json",
    ...readdirSync(new URL("../src/generated/", import.meta.url))
      .filter((name) => name.endsWith(".ts"))
      .map((name) => `src/generated/${name}`),
  ]
  return new Map(names.map((name) => [name, readFileSync(new URL(`../${name}`, import.meta.url))]))
}

const before = snapshot()
const generated = spawnSync("npm", ["run", "generate:api"], { cwd: root, stdio: "inherit" })
if (generated.status !== 0) process.exit(generated.status ?? 1)

const after = snapshot()
const changed = [...new Set([...before.keys(), ...after.keys()])]
  .filter((name) => {
    const left = before.get(name)
    const right = after.get(name)
    return !left || !right || !left.equals(right)
  })
if (changed.length) {
  console.error(`Generated API contracts are stale: ${changed.join(", ")}`)
  process.exitCode = 1
}
