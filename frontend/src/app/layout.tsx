import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import Script from "next/script";
import "./globals.css";
import Sidebar from "@/components/Sidebar";
import { I18nProvider } from "@/contexts/I18nContext";
import { AppAuthShell } from "@/contexts/AuthContext";

export const metadata: Metadata = {
  title: "opencai — Uncensored AI Companions",
  description: "Private AI character stories & illustrations across Web and Telegram Mini App.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="antialiased font-sans" suppressHydrationWarning>
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
        <AppAuthShell>
          <I18nProvider>
            <div className="app-viewport-shell flex flex-col md:flex-row overflow-hidden">
              <Suspense><Sidebar /></Suspense>
              <main className="flex-1 min-h-0 min-w-0 overflow-hidden">
                {children}
              </main>
            </div>
          </I18nProvider>
        </AppAuthShell>
      </body>
    </html>
  );
}
