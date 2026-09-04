"use client";

import { Suspense, useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type RefundRequestSummary = {
  id: string;
  order_reference: string;
  reason: string;
  amount_cents: number | null;
  status: string;
};

type ChatSubmissionResponse = {
  type: "confirmation" | "clarification" | "escalated";
  message: string;
  refund_request?: RefundRequestSummary | null;
  deduplicated?: boolean | null;
};

type ErrorEnvelope = {
  error: { code: string; message: string; details?: unknown };
};

type ChatMessage =
  | { id: string; role: "customer"; text: string }
  | {
      id: string;
      role: "agent";
      text: string;
      refundRequest?: RefundRequestSummary | null;
      deduplicated?: boolean | null;
      isError?: boolean;
    };

function formatAmount(amountCents: number | null): string {
  if (amountCents === null) return "not specified";
  return `$${(amountCents / 100).toFixed(2)}`;
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
}

function statusBadgeClass(status: string): string {
  return `badge badge--${status}`;
}

function ChatPageInner() {
  const searchParams = useSearchParams();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    const orderReference = searchParams.get("order");
    if (orderReference) {
      setInput(`I'd like a refund for order ${orderReference}.`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = input.trim();
    if (!text || isSubmitting) return;

    setMessages((prev) => [...prev, { id: newId(), role: "customer", text }]);
    setInput("");
    setIsSubmitting(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/chat/refund-requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (!response.ok) {
        let errorMessage = "Something went wrong. Please try again.";
        try {
          const body = (await response.json()) as ErrorEnvelope;
          if (body?.error?.message) errorMessage = body.error.message;
        } catch {
          // response body wasn't JSON -- fall back to the generic message
        }
        if (response.status === 429) {
          errorMessage =
            "You're submitting requests too quickly. Please wait a moment and try again.";
        }
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: "agent", text: errorMessage, isError: true },
        ]);
        return;
      }

      const data = (await response.json()) as ChatSubmissionResponse;
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: "agent",
          text: data.message,
          refundRequest: data.refund_request,
          deduplicated: data.deduplicated,
        },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: "agent",
          text: "Couldn't reach the server. Please check your connection and try again.",
          isError: true,
        },
      ]);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="shell">
      <div className="page-header">
        <h1>Refund Request Chat</h1>
        <p>Describe your order and the reason -- the agent takes it from there.</p>
      </div>

      <div className="card chat-card">
        <div className="chat-header">
          <span className="chat-header__avatar">🤖</span>
          <div>
            <div className="chat-header__title">Refund Assistant</div>
            <div className="chat-header__subtitle">Order lookup &middot; policy check &middot; Stripe refund</div>
          </div>
        </div>

        <div className="chat-body">
          {messages.length === 0 && (
            <p className="chat-empty">
              Tell us about the order you&apos;d like refunded -- e.g. &quot;Refund order
              #ORD-1234, wrong size&quot;.
            </p>
          )}

          {messages.map((message) => (
            <div
              key={message.id}
              className={`bubble-row bubble-row--${message.role}`}
            >
              <div
                className={`bubble ${
                  message.role === "customer"
                    ? "bubble--customer"
                    : message.role === "agent" && message.isError
                      ? "bubble--error"
                      : "bubble--agent"
                }`}
              >
                <p>{message.text}</p>
                {message.role === "agent" && message.refundRequest && (
                  <dl className="bubble-meta">
                    <dt>Request ID</dt>
                    <dd className="mono">{message.refundRequest.id}</dd>
                    <dt>Order</dt>
                    <dd>{message.refundRequest.order_reference}</dd>
                    <dt>Reason</dt>
                    <dd>{message.refundRequest.reason || "—"}</dd>
                    <dt>Amount</dt>
                    <dd>{formatAmount(message.refundRequest.amount_cents)}</dd>
                    <dt>Status</dt>
                    <dd>
                      <span className={statusBadgeClass(message.refundRequest.status)}>
                        {message.refundRequest.status}
                      </span>
                    </dd>
                    {message.deduplicated && (
                      <>
                        <dt>Note</dt>
                        <dd>Matched an existing recent request</dd>
                      </>
                    )}
                  </dl>
                )}
              </div>
            </div>
          ))}
        </div>

        <form onSubmit={handleSubmit} className="chat-form">
          <input
            type="text"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Describe your refund request..."
            disabled={isSubmitting}
            className="input"
          />
          <button type="submit" disabled={isSubmitting || !input.trim()} className="btn btn-primary">
            {isSubmitting ? <span className="spinner" /> : "Send"}
          </button>
        </form>
      </div>
    </main>
  );
}

export default function ChatPage() {
  return (
    <Suspense fallback={null}>
      <ChatPageInner />
    </Suspense>
  );
}
