"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type RefundRequestSummary = {
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

function formatAmount(amountCents: number | null): string {
  if (amountCents === null) return "—";
  return `$${(amountCents / 100).toFixed(2)}`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
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

export default function ApprovalsPage() {
  const router = useRouter();
  const [reviewerIdentifier, setReviewerIdentifier] = useState("");
  const [queue, setQueue] = useState<RefundRequestSummary[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  const loadQueue = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/approvals/refund-requests`);
      if (!response.ok) throw new Error(await extractErrorMessage(response));
      setQueue((await response.json()) as RefundRequestSummary[]);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Couldn't reach the server.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadQueue();
  }, [loadQueue]);

  async function decide(refundRequestId: string, decision: "approve" | "deny") {
    const reviewer = reviewerIdentifier.trim();
    if (!reviewer) {
      setRowErrors((prev) => ({ ...prev, [refundRequestId]: "Enter your name above before deciding." }));
      return;
    }

    setDecidingId(refundRequestId);
    setRowErrors((prev) => {
      const next = { ...prev };
      delete next[refundRequestId];
      return next;
    });

    try {
      const response = await fetch(
        `${API_BASE_URL}/api/approvals/refund-requests/${encodeURIComponent(refundRequestId)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, reviewer_identifier: reviewer }),
        }
      );
      if (!response.ok) throw new Error(await extractErrorMessage(response));

      // The decided request is no longer escalated -- drop it from the
      // queue immediately rather than waiting on a full reload, then
      // reconcile with the server in the background.
      setQueue((prev) => prev.filter((request) => request.id !== refundRequestId));
      await loadQueue();
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [refundRequestId]: err instanceof Error ? err.message : "Couldn't record that decision.",
      }));
    } finally {
      setDecidingId(null);
    }
  }

  return (
    <main className="shell">
      <div className="page-header">
        <h1>Approval Queue</h1>
        <p>
          Escalated refund requests waiting on a human decision. Approving resumes the
          Agent Loop (it may still re-escalate); denying closes the case immediately. No
          auth -- staff use only.
        </p>
      </div>

      <div className="card panel toolbar">
        <input
          type="text"
          placeholder="Your name or staff id"
          value={reviewerIdentifier}
          onChange={(event) => setReviewerIdentifier(event.target.value)}
          className="input"
          style={{ flex: 1, minWidth: 200 }}
        />
      </div>

      <div className="toolbar">
        <button onClick={loadQueue} disabled={isLoading} className="btn btn-ghost btn-sm">
          {isLoading ? "Refreshing..." : "↻ Refresh"}
        </button>
      </div>

      {loadError && <p className="load-error">{loadError}</p>}

      <div className="card table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Order Reference</th>
              <th>Reason</th>
              <th>Amount</th>
              <th>Escalated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {queue.length === 0 && !isLoading && (
              <tr>
                <td colSpan={5}>
                  <div className="empty-row">No escalated refund requests -- the queue is empty.</div>
                </td>
              </tr>
            )}
            {queue.map((request) => (
              <tr key={request.id}>
                <td style={{ fontWeight: 600 }}>{request.order_reference}</td>
                <td>{request.reason || "—"}</td>
                <td>{formatAmount(request.amount_cents)}</td>
                <td>{formatDate(request.created_at)}</td>
                <td>
                  <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end", flexWrap: "wrap" }}>
                    <button
                      onClick={() => router.push(`/trajectory/${request.id}`)}
                      className="btn btn-secondary btn-sm"
                    >
                      <span aria-hidden="true">🔍</span> Trajectory
                    </button>
                    <button
                      onClick={() => decide(request.id, "deny")}
                      disabled={decidingId === request.id}
                      className="btn btn-secondary btn-sm"
                    >
                      {decidingId === request.id ? <span className="spinner" /> : "Deny"}
                    </button>
                    <button
                      onClick={() => decide(request.id, "approve")}
                      disabled={decidingId === request.id}
                      className="btn btn-primary btn-sm"
                    >
                      {decidingId === request.id ? <span className="spinner" /> : "Approve"}
                    </button>
                  </div>
                  {rowErrors[request.id] && (
                    <p className="form-error" style={{ textAlign: "right" }}>
                      {rowErrors[request.id]}
                    </p>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
