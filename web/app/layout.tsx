import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Lexington Coverage Portal",
    template: "%s | Lexington Coverage",
  },
  description:
    "Last-minute provider coverage scheduling for Lexington Services members and families.",
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_APP_URL ?? "https://coverage.lexingtonservices.com"
  ),
  // PWA manifest
  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "Lex Coverage",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#29ABE2",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body>{children}</body>
    </html>
  );
}
