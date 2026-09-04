import Link from "next/link";

export default function HomePage() {
  return (
    <main className="shell">
      <section className="hero">
        <span className="hero__eyebrow">⚡ Built from scratch, no agent framework</span>
        <h1>AI Payment Refund Agent</h1>
        <p>
          A hand-built agent loop that resolves customer refund requests end-to-end --
          policy checks, order lookup, and a real Stripe test-mode refund, all
          inspectable along the way.
        </p>
      </section>

      <div className="link-cards">
        <Link href="/chat" className="card link-card">
          <span className="link-card__icon">💬</span>
          <h3>Refund Chat</h3>
          <p>Describe an order and a reason in plain language and let the agent resolve it.</p>
          <span className="link-card__arrow">Start a conversation &rarr;</span>
        </Link>

        <Link href="/dashboard" className="card link-card">
          <span className="link-card__icon">🗂️</span>
          <h3>Dev Dashboard</h3>
          <p>Seed test orders with real Stripe test PaymentIntents and inspect refund requests.</p>
          <span className="link-card__arrow">Open dashboard &rarr;</span>
        </Link>
      </div>
    </main>
  );
}
