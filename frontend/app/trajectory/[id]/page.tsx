"use client";

import Link from "next/link";
import { Fragment, useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type TrajectoryEvent = {
  sequence_no: number;
  step_type: string;
  step_data: Record<string, unknown>;
  created_at: string;
};

type RefundRequest = {
  id: string;
  order_reference: string;
  reason: string;
  amount_cents: number | null;
  status: string;
  created_at: string;
};

type ErrorEnvelope = {
  error: { code: string; message: string; details?: unknown };
};

type LoadState =
  | { kind: "loading" }
  | { kind: "not_found" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; events: TrajectoryEvent[] };

// Fields known to hold integer cents -- rendered as dollars, matching the
// dashboard's own formatAmount() convention, instead of a raw integer.
const CENTS_FIELDS = new Set(["amount_cents"]);

function formatWords(raw: string): string {
  return raw
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function formatAmount(amountCents: number): string {
  return `$${(amountCents / 100).toFixed(2)}`;
}

function formatFieldValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number" && CENTS_FIELDS.has(key)) return formatAmount(value);
  if (Array.isArray(value)) return value.length === 0 ? "—" : value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

async function extractErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ErrorEnvelope;
    if (body?.error?.message) return body.error.message;
  } catch {
    // response body wasn't JSON -- fall back below
  }
  return "Something went wrong. Please try again.";
}

export default function TrajectoryPage() {
  const params = useParams<{ id: string }>();
  const refundRequestId = params.id;
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [refundRequest, setRefundRequest] = useState<RefundRequest | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const [trajectoryResponse, refundRequestsResponse] = await Promise.all([
        fetch(`${API_BASE_URL}/api/refund-requests/${encodeURIComponent(refundRequestId)}/trajectory`),
        fetch(`${API_BASE_URL}/api/dev/refund-requests`),
      ]);

      if (refundRequestsResponse.ok) {
        const all = (await refundRequestsResponse.json()) as RefundRequest[];
        setRefundRequest(all.find((r) => r.id === refundRequestId) ?? null);
      }

      if (trajectoryResponse.status === 404) {
        setState({ kind: "not_found" });
        return;
      }
      if (!trajectoryResponse.ok) {
        setState({ kind: "error", message: await extractErrorMessage(trajectoryResponse) });
        return;
      }
      const events = (await trajectoryResponse.json()) as TrajectoryEvent[];
      setState({ kind: "loaded", events });
    } catch {
      setState({ kind: "error", message: "Couldn't reach the server." });
    }
  }, [refundRequestId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="shell">
      <div className="page-header">
        <Link href="/dashboard" className="btn btn-ghost btn-sm" style={{ marginBottom: "1rem" }}>
          ← Back to Dashboard
        </Link>
        <h1>Refund Request Trajectory</h1>
        <p>
          Every step the Agent Loop took resolving <span className="mono">{refundRequestId}</span>,
          in order.
          {refundRequest && (
            <>
              {" "}Order <strong>{refundRequest.order_reference}</strong> ·{" "}
              <span className={`badge badge--${refundRequest.status}`}>{refundRequest.status}</span>
            </>
          )}
        </p>
      </div>

      <div className="toolbar">
        <button onClick={load} disabled={state.kind === "loading"} className="btn btn-ghost btn-sm">
          {state.kind === "loading" ? "Refreshing..." : "↻ Refresh"}
        </button>
      </div>

      {state.kind === "loading" && (
        <div className="card panel">
          <div className="empty-row">Loading trajectory…</div>
        </div>
      )}

      {state.kind === "not_found" && (
        <div className="card panel">
          <div className="empty-row">
            No refund request found with this id. Double-check the id and try again.
          </div>
        </div>
      )}

      {state.kind === "error" && <p className="load-error">{state.message}</p>}

      {state.kind === "loaded" && state.events.length === 0 && (
        <div className="card panel">
          <div className="empty-row">
            No trajectory events recorded yet — this request may still be resolving. Try refreshing.
          </div>
        </div>
      )}

      {state.kind === "loaded" && state.events.length > 0 && (
        <div className="timeline">
          {state.events.map((event) => (
            <div key={event.sequence_no} className="card step-card">
              <div className="step-card__header">
                <span className="step-card__seq">{event.sequence_no}</span>
                <span className="step-card__type">{formatWords(event.step_type)}</span>
                <span className="step-card__time">{formatTimestamp(event.created_at)}</span>
              </div>
              {Object.keys(event.step_data).length > 0 && (
                <dl className="step-data">
                  {Object.entries(event.step_data).map(([key, value]) => (
                    <Fragment key={key}>
                      <dt>{formatWords(key)}</dt>
                      <dd>{formatFieldValue(key, value)}</dd>
                    </Fragment>
                  ))}
                </dl>
              )}
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
