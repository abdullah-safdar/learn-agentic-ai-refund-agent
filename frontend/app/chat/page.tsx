"use client";

import { useState, type FormEvent } from "react";

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
  type: "confirmation" | "clarification";
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

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

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
    <main style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 1rem" }}>
      <h1 style={{ fontSize: "1.25rem", marginBottom: "1rem" }}>Refund Request Chat</h1>

      <div
        style={{
          border: "1px solid #ddd",
          borderRadius: 8,
          minHeight: 320,
          padding: "1rem",
          marginBottom: "1rem",
          background: "#fff",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
        aria-live="polite"
      >
        {messages.length === 0 && (
          <p style={{ color: "#888" }}>
            Tell us about the order you&apos;d like refunded -- e.g. &quot;Refund order
            #ORD-1234, wrong size&quot;.
          </p>
        )}

        {messages.map((message) => (
          <div
            key={message.id}
            style={{
              alignSelf: message.role === "customer" ? "flex-end" : "flex-start",
              maxWidth: "85%",
              background:
                message.role === "customer"
                  ? "#daf1ff"
                  : message.role === "agent" && message.isError
                    ? "#ffe1e1"
                    : "#eee",
              borderRadius: 8,
              padding: "0.5rem 0.75rem",
            }}
          >
            <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{message.text}</p>
            {message.role === "agent" && message.refundRequest && (
              <dl
                style={{
                  margin: "0.5rem 0 0",
                  fontSize: "0.85rem",
                  color: "#333",
                  display: "grid",
                  gridTemplateColumns: "auto 1fr",
                  columnGap: "0.5rem",
                  rowGap: "0.15rem",
                }}
              >
                <dt>Request ID</dt>
                <dd>{message.refundRequest.id}</dd>
                <dt>Order</dt>
                <dd>{message.refundRequest.order_reference}</dd>
                <dt>Reason</dt>
                <dd>{message.refundRequest.reason}</dd>
                <dt>Amount</dt>
                <dd>{formatAmount(message.refundRequest.amount_cents)}</dd>
                <dt>Status</dt>
                <dd>{message.refundRequest.status}</dd>
                {message.deduplicated && (
                  <>
                    <dt>Note</dt>
                    <dd>Matched an existing recent request</dd>
                  </>
                )}
              </dl>
            )}
          </div>
        ))}
      </div>

      <form onSubmit={handleSubmit} style={{ display: "flex", gap: "0.5rem" }}>
        <input
          type="text"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Describe your refund request..."
          disabled={isSubmitting}
          style={{
            flex: 1,
            padding: "0.6rem 0.75rem",
            borderRadius: 6,
            border: "1px solid #ccc",
          }}
        />
        <button
          type="submit"
          disabled={isSubmitting || !input.trim()}
          style={{
            padding: "0.6rem 1.2rem",
            borderRadius: 6,
            border: "none",
            background: "#0070f3",
            color: "#fff",
            cursor: isSubmitting ? "not-allowed" : "pointer",
          }}
        >
          {isSubmitting ? "Sending..." : "Send"}
        </button>
      </form>
    </main>
  );
}
