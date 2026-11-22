"use client";
import { SignIn } from "@clerk/nextjs";
import { IS_CLERK_CONFIGURED } from "@/contexts/AuthContext";

export default function SignInPage() {
  if (!IS_CLERK_CONFIGURED) {
    return <p className="p-6">Web sign-in is unavailable on this instance.</p>;
  }
  return (
    <div className="flex h-full items-center justify-center">
      <SignIn
        appearance={{
          elements: {
            rootBox: "mx-auto",
            card: "bg-[var(--card-bg)] border border-[var(--card-border)] shadow-2xl",
            headerTitle: "text-white",
            headerSubtitle: "text-[var(--muted)]",
            socialButtonsBlockButton: "border-[var(--card-border)] text-white hover:bg-white/5",
            dividerLine: "bg-[var(--card-border)]",
            dividerText: "text-[var(--muted)]",
            formFieldLabel: "text-[var(--muted)]",
            formFieldInput: "bg-[rgba(255,255,255,0.04)] border-[var(--card-border)] text-white",
            footerActionLink: "text-[var(--accent)]",
            formButtonPrimary: "bg-[var(--accent)] hover:opacity-90",
          },
        }}
      />
    </div>
  );
}
