import { flexRender } from "@tanstack/react-table"
import { getCoreRowModel, useLegacyTable, type LegacyColumnDef } from "@tanstack/react-table/legacy"
import { cn } from "../lib/utils"

type ColumnGroup = { label: string; span: number; className?: string }

type DataTableProps<T extends Record<string, unknown>> = {
  data: T[]
  columns: LegacyColumnDef<T>[]
  caption: string
  onSort?: (id: string) => void
  canSort?: (id: string) => boolean
  sort?: string
  order?: "asc" | "desc"
  groups?: ColumnGroup[]
  rowClassName?: (row: T) => string
  empty?: string
}

export function DataTable<T extends Record<string, unknown>>({
  data,
  columns,
  caption,
  onSort,
  canSort,
  sort,
  order,
  groups,
  rowClassName,
  empty = "No strategies match these filters.",
}: DataTableProps<T>) {
  const table = useLegacyTable({ data, columns, getCoreRowModel: getCoreRowModel() })
  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-card">
      <table className="w-full min-w-[720px] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-muted text-left text-xs tracking-wide text-muted-foreground uppercase">
          {groups ? (
            <tr>
              {groups.map((group) => (
                <th key={group.label || "leading"} scope="colgroup" colSpan={group.span} className={cn("px-3 py-2 font-medium", group.className)}>
                  {group.label}
                </th>
              ))}
            </tr>
          ) : null}
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => {
                const id = header.column.id
                const active = sort === id
                const sortable = Boolean(onSort) && (canSort ? canSort(id) : true)
                const ariaSort = !sortable ? undefined : active ? (order === "asc" ? "ascending" : "descending") : "none"
                return (
                  <th key={header.id} scope="col" aria-sort={ariaSort} className="sticky top-0 z-10 bg-muted px-3 py-2 font-medium">
                    {sortable ? (
                      <button type="button" className="inline-flex min-h-6 items-center hover:text-foreground" onClick={() => onSort?.(id)}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {active ? (order === "asc" ? " ↑" : " ↓") : ""}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.length === 0 ? (
            <tr>
              <td className="px-3 py-8 text-muted-foreground" colSpan={columns.length}>
                {empty}
              </td>
            </tr>
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr key={row.id} className={cn("border-t border-border", rowClassName?.(row.original))}>
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-2 align-top">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
