import Link from "next/link";

export default function HomePage() {
  return (
    <main style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 1rem" }}>
      <h1>AI Payment Refund Agent</h1>
      <p>
        <Link href="/chat">Start a refund request via chat &rarr;</Link>
      </p>
    </main>
  );
}
