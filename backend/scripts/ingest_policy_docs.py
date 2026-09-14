"""CLI entry point (spec-2-1): ingest every markdown policy document under
`backend/policy_documents/` into the Policy Store. A local/CI script, not an
HTTP route -- routes/dev.py's docstring already scopes that module to
order/refund-request seeding, not document pipelines.

Idempotent: safe to re-run any time, including against an already-ingested
document -- re-ingestion just supersedes that document's previously-active
chunks (db.replace_policy_document_chunks), it never errors or duplicates.

Requires DATABASE_URL and VOYAGE_API_KEY to be set (embeddings always call
Voyage AI, independent of LLM_PROVIDER -- see services/embeddings.py).

Run from the `backend/` directory with: python -m scripts.ingest_policy_docs
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import db
from services import policy_ingestion

POLICY_DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "policy_documents"


def main() -> int:
    db.run_migrations()

    paths = sorted(POLICY_DOCUMENTS_DIR.glob("*.md"))
    if not paths:
        print(f"No policy documents found under {POLICY_DOCUMENTS_DIR}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    failed_documents: list[str] = []
    for path in paths:
        # document_slug goes through the same slug convention as a clause
        # heading (services.policy_ingestion.slugify) so document_id always
        # matches citation_id's own {document_slug}#{clause_slug} shape,
        # even for a filename with spaces/uppercase/underscores.
        document_slug = policy_ingestion.slugify(path.stem)
        try:
            # Reading the file is inside this try too -- a bad encoding,
            # permissions error, or the file disappearing mid-run gets the
            # same structured "Failed to ingest ..." report as an
            # embedding/DB failure, never a raw traceback.
            document_text = path.read_text(encoding="utf-8")
            chunks = policy_ingestion.ingest_document(document_slug, document_text, now)
        except Exception as exc:
            # I/O & Edge-Case Matrix: "script reports which document failed
            # and exits non-zero" -- no DB writes for this document happened
            # (embedding-API failure) or none should have (malformed
            # document). One document's failure never blocks the rest of
            # the loop: every other document is its own independent
            # transaction (db.replace_policy_document_chunks), so a
            # document that succeeds stays persisted regardless of what
            # happens to any other document in this run.
            print(f"Failed to ingest {path.name} (document_slug={document_slug!r}): {exc}", file=sys.stderr)
            failed_documents.append(path.name)
            continue
        print(f"Ingested {path.name}: {len(chunks)} active chunk(s) under document_slug={document_slug!r}")

    if failed_documents:
        print(f"{len(failed_documents)} of {len(paths)} document(s) failed to ingest: {', '.join(failed_documents)}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
