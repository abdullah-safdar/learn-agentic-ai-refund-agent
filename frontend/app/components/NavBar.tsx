"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/chat", label: "Chat" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/approvals", label: "Approvals" },
];

export default function NavBar() {
  const pathname = usePathname();

  return (
    <nav className="navbar">
      <Link href="/" className="navbar__brand">
        <span className="navbar__brand-mark">R</span>
        Refund Agent
      </Link>
      <div className="navbar__links">
        {LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={`navbar__link ${pathname === link.href ? "navbar__link--active" : ""}`}
          >
            {link.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
