import type { Metadata } from "next";
import "./globals.css";
import TopBar from "@/components/TopBar";

export const metadata: Metadata = {
  title: "Doc ↔ Script Consistency — s390x Analyzer",
  description:
    "AI-powered consistency dashboard comparing Linux on IBM Z wiki documentation with the automated build scripts.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <TopBar />
        <main className="mx-auto max-w-7xl px-4 pb-16">{children}</main>
      </body>
    </html>
  );
}
