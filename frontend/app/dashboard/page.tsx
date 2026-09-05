"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Order = {
  id: string;
  order_reference: string;
  status: string;
  amount_cents: number;
  order_date: string;
  stripe_payment_intent_id: string | null;
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

type Tab = "orders" | "refunds";

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

export default function DashboardPage() {
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("orders");
  const [orders, setOrders] = useState<Order[]>([]);
  const [refundRequests, setRefundRequests] = useState<RefundRequest[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [orderReference, setOrderReference] = useState("");
  const [amountDollars, setAmountDollars] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadOrders = useCallback(async () => {
    const response = await fetch(`${API_BASE_URL}/api/dev/orders`);
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    setOrders((await response.json()) as Order[]);
  }, []);

  const loadRefundRequests = useCallback(async () => {
    const response = await fetch(`${API_BASE_URL}/api/dev/refund-requests`);
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    setRefundRequests((await response.json()) as RefundRequest[]);
  }, []);

  const loadAll = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      await Promise.all([loadOrders(), loadRefundRequests()]);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Couldn't reach the server.");
    } finally {
      setIsLoading(false);
    }
  }, [loadOrders, loadRefundRequests]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  async function handleCreateOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);

    const reference = orderReference.trim();
    const dollars = Number(amountDollars);
    if (!reference || !Number.isFinite(dollars) || dollars <= 0) {
      setCreateError("Enter an order reference and a positive dollar amount.");
      return;
    }

    setIsCreating(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/dev/orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          order_reference: reference,
          amount_cents: Math.round(dollars * 100),
        }),
      });
      if (!response.ok) throw new Error(await extractErrorMessage(response));

      setOrderReference("");
      setAmountDollars("");
      await loadOrders();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Couldn't create the order.");
    } finally {
      setIsCreating(false);
    }
  }

  function testInChat(reference: string) {
    router.push(`/chat?order=${encodeURIComponent(reference)}`);
  }

  return (
    <main className="shell">
      <div className="page-header">
        <h1>Dev Dashboard</h1>
        <p>
          Seed test orders with real Stripe test-mode PaymentIntents, then jump straight
          into the chat to test the refund flow. No auth -- dev use only.
        </p>
      </div>

      <div className="toolbar">
        <div className="tabs">
          <button
            className={`tab ${tab === "orders" ? "tab--active" : ""}`}
            onClick={() => setTab("orders")}
          >
            Orders
          </button>
          <button
            className={`tab ${tab === "refunds" ? "tab--active" : ""}`}
            onClick={() => setTab("refunds")}
          >
            Refund Requests
          </button>
        </div>
        <button onClick={loadAll} disabled={isLoading} className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }}>
          {isLoading ? "Refreshing..." : "↻ Refresh"}
        </button>
      </div>

      {loadError && <p className="load-error">{loadError}</p>}

      {tab === "orders" && (
        <>
          <form onSubmit={handleCreateOrder} className="card panel toolbar" style={{ marginBottom: "1.25rem" }}>
            <input
              type="text"
              placeholder="Order reference, e.g. ORD-1002"
              value={orderReference}
              onChange={(event) => setOrderReference(event.target.value)}
              disabled={isCreating}
              className="input"
              style={{ flex: 1, minWidth: 200 }}
            />
            <input
              type="number"
              step="0.01"
              min="0.01"
              placeholder="Amount (USD)"
              value={amountDollars}
              onChange={(event) => setAmountDollars(event.target.value)}
              disabled={isCreating}
              className="input"
              style={{ width: 140 }}
            />
            <button type="submit" disabled={isCreating} className="btn btn-primary">
              {isCreating ? <span className="spinner" /> : "+ Create test order"}
            </button>
            {createError && <p className="form-error" style={{ flexBasis: "100%" }}>{createError}</p>}
          </form>

          <div className="card table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Order Reference</th>
                  <th>Status</th>
                  <th>Amount</th>
                  <th>Order Date</th>
                  <th>Stripe PaymentIntent</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {orders.length === 0 && !isLoading && (
                  <tr>
                    <td colSpan={6}>
                      <div className="empty-row">No orders yet -- create one above.</div>
                    </td>
                  </tr>
                )}
                {orders.map((order) => (
                  <tr key={order.id}>
                    <td style={{ fontWeight: 600 }}>{order.order_reference}</td>
                    <td>
                      <span className={`badge badge--${order.status}`}>{order.status}</span>
                    </td>
                    <td>{formatAmount(order.amount_cents)}</td>
                    <td>{formatDate(order.order_date)}</td>
                    <td className="mono">{order.stripe_payment_intent_id ?? "—"}</td>
                    <td>
                      <button
                        onClick={() => testInChat(order.order_reference)}
                        className="btn btn-secondary btn-sm"
                      >
                        💬 Test in chat
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "refunds" && (
        <div className="card table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Order Reference</th>
                <th>Reason</th>
                <th>Amount</th>
                <th>Status</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {refundRequests.length === 0 && !isLoading && (
                <tr>
                  <td colSpan={6}>
                    <div className="empty-row">No refund requests yet -- submit one via the chat page.</div>
                  </td>
                </tr>
              )}
              {refundRequests.map((refundRequest) => (
                <tr key={refundRequest.id}>
                  <td style={{ fontWeight: 600 }}>{refundRequest.order_reference}</td>
                  <td>{refundRequest.reason || "—"}</td>
                  <td>{formatAmount(refundRequest.amount_cents)}</td>
                  <td>
                    <span className={`badge badge--${refundRequest.status}`}>{refundRequest.status}</span>
                  </td>
                  <td>{formatDate(refundRequest.created_at)}</td>
                  <td>
                    <button
                      onClick={() => router.push(`/trajectory/${refundRequest.id}`)}
                      className="btn btn-secondary btn-sm"
                    >
                      <span aria-hidden="true">🔍</span> Trajectory
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
