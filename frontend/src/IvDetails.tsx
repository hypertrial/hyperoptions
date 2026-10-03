import { unsignedPercentTenths } from "./format"
import { IV_DETAILS_UNAVAILABLE, IV_MODEL_NOTE, IV_SENSITIVITY_NOTE, ivCalculationFacts, type IvRow } from "./ivFacts"

export default function IvDetails({ row, contractLabel }: { row: IvRow; contractLabel: string }) {
  const available = row.iv_pct_tenths != null
  const value = unsignedPercentTenths(row.iv_pct_tenths)
  const action = available ? "Details" : "Why unavailable?"
  return (
    <details className="iv-details">
      <summary aria-label={`IV details for ${contractLabel}: ${value} · ${action}`}>
        <span className="iv-midpoint font-mono">{value}</span> · {action}
      </summary>
      <div className="iv-details-content">
        {row.iv_details ? (
          <>
            <dl className="iv-facts">
              {ivCalculationFacts(row).map(({ label, value }) => (
                <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
              ))}
            </dl>
            <p>{IV_MODEL_NOTE}</p>
            <p>{IV_SENSITIVITY_NOTE}</p>
          </>
        ) : <p>{IV_DETAILS_UNAVAILABLE}</p>}
      </div>
    </details>
  )
}
