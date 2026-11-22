"use client";

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { ClerkProvider, SignInButton, UserButton, useAuth as useClerkAuth } from "@clerk/nextjs";
import {
  authenticateTelegramWebApp,
  redeemTelegramBindCode,
  type TelegramWebAppSession,
} from "@/lib/api";

declare global {
  interface TelegramWebAppBridge {
    initData?: string;
    initDataUnsafe?: {
      user?: {
        id?: number;
        username?: string;
        first_name?: string;
        language_code?: string;
      };
    };
    viewportHeight?: number;
    viewportStableHeight?: number;
    isExpanded?: boolean;
    ready?: () => void;
    expand?: () => void;
    onEvent?: (eventType: string, callback: () => void) => void;
    offEvent?: (eventType: string, callback: () => void) => void;
  }

  interface Window {
    Telegram?: {
      WebApp?: TelegramWebAppBridge;
    };
    TelegramWebviewProxy?: {
      postEvent?: (eventType: string, eventData: string) => void;
    };
  }
}

const CLERK_PUB_KEY = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY || "";
export const IS_CLERK_CONFIGURED = CLERK_PUB_KEY.startsWith("pk_");

const TG_SESSION_STORAGE_KEY = "opencai_tg_webapp_session";

interface StoredTelegramSession extends TelegramWebAppSession {
  expires_at_ms: number;
}

export interface AppAuthValue {
  getToken: () => Promise<string | null>;
  isSignedIn: boolean;
  isLoaded: boolean;
  authMode: "clerk" | "telegram" | "anonymous";
  userId: string | null;
  telegramUserId: string | null;
  isTelegramMiniApp: boolean;
  isLinked: boolean;
  telegramAuthError: string | null;
  clerkConfigured: boolean;
  redeemBindCode: (code: string) => Promise<TelegramWebAppSession>;
}

const AppAuthContext = createContext<AppAuthValue>({
  getToken: async () => null,
  isSignedIn: false,
  isLoaded: true,
  authMode: "anonymous",
  userId: null,
  telegramUserId: null,
  isTelegramMiniApp: false,
  isLinked: false,
  telegramAuthError: null,
  clerkConfigured: IS_CLERK_CONFIGURED,
  redeemBindCode: async () => {
    throw new Error("Telegram Mini App session is not active");
  },
});

/**
 * Extract Telegram launch initData from either `window.Telegram.WebApp.initData`
 * or the `#tgWebAppData=...` URL hash fragment, scrubbing it from location.hash
 * immediately so launch credentials never remain in history or address bars.
 */
function extractTelegramInitData(): { initData: string; detected: boolean } {
  if (typeof window === "undefined") {
    return { initData: "", detected: false };
  }

  const webApp = window.Telegram?.WebApp;
  if (webApp?.initData && webApp.initData.trim().length > 0) {
    return { initData: webApp.initData.trim(), detected: true };
  }

  const rawHash = window.location.hash;
  if (rawHash && rawHash.includes("tgWebAppData=")) {
    try {
      const hashParams = new URLSearchParams(rawHash.slice(1));
      const tgData = hashParams.get("tgWebAppData") || "";
      hashParams.delete("tgWebAppData");
      const remaining = hashParams.toString();
      const cleanUrl =
        window.location.pathname +
        window.location.search +
        (remaining ? `#${remaining}` : "");
      window.history.replaceState(null, "", cleanUrl);
      if (tgData.trim().length > 0) {
        return { initData: tgData.trim(), detected: true };
      }
    } catch {
      // ignore malformed hash
    }
  }

  const hasProxy = Boolean(window.TelegramWebviewProxy?.postEvent || webApp);
  return { initData: "", detected: hasProxy && Boolean(webApp?.initData) };
}

function loadCachedTelegramSession(): StoredTelegramSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(TG_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredTelegramSession;
    if (!parsed.access_token || !parsed.expires_at_ms) return null;
    if (Date.now() >= parsed.expires_at_ms - 30_000) {
      sessionStorage.removeItem(TG_SESSION_STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function saveCachedTelegramSession(session: TelegramWebAppSession): StoredTelegramSession {
  const stored: StoredTelegramSession = {
    ...session,
    expires_at_ms: Date.now() + session.expires_in * 1000,
  };
  try {
    sessionStorage.setItem(TG_SESSION_STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // ignore storage quota errors
  }
  return stored;
}

function useTelegramViewportAndSession() {
  const [tgSession, setTgSession] = useState<StoredTelegramSession | null>(null);
  const [isTelegramMiniApp, setIsTelegramMiniApp] = useState(false);
  const [tgLoading, setTgLoading] = useState(true);
  const [telegramAuthError, setTelegramAuthError] = useState<string | null>(null);
  const launch = useRef<ReturnType<typeof extractTelegramInitData> | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const syncAppHeight = () => {
      const tgHeight =
        window.Telegram?.WebApp?.viewportHeight ||
        window.Telegram?.WebApp?.viewportStableHeight;
      const vvHeight = window.visualViewport?.height;
      const heightPx = tgHeight && tgHeight > 120 ? tgHeight : vvHeight || window.innerHeight;
      if (heightPx && heightPx > 120) {
        document.documentElement.style.setProperty("--app-height", `${Math.round(heightPx)}px`);
      }
    };

    syncAppHeight();
    window.addEventListener("resize", syncAppHeight);
    window.visualViewport?.addEventListener("resize", syncAppHeight);
    window.Telegram?.WebApp?.onEvent?.("viewportChanged", syncAppHeight);

    return () => {
      window.removeEventListener("resize", syncAppHeight);
      window.visualViewport?.removeEventListener("resize", syncAppHeight);
      window.Telegram?.WebApp?.offEvent?.("viewportChanged", syncAppHeight);
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;

    launch.current ??= extractTelegramInitData();
    const { initData, detected } = launch.current;
    if (detected) {
      setIsTelegramMiniApp(true);
      try {
        window.Telegram?.WebApp?.ready?.();
        window.Telegram?.WebApp?.expand?.();
        window.TelegramWebviewProxy?.postEvent?.("web_app_ready", "{}");
        window.TelegramWebviewProxy?.postEvent?.("web_app_expand", "{}");
      } catch {
        // ignore host bridge errors
      }
    }

    const cached = loadCachedTelegramSession();
    const launchedUser = window.Telegram?.WebApp?.initDataUnsafe?.user?.id;
    if (cached && (!initData || String(launchedUser) === cached.telegram_user_id)) {
      setTgSession(cached);
      setIsTelegramMiniApp(true);
      setTgLoading(false);
      return;
    }

    if (!initData) {
      setTgLoading(false);
      return;
    }

    let cancelled = false;

    authenticateTelegramWebApp(initData)
      .then((res) => {
        if (cancelled) return;
        const saved = saveCachedTelegramSession(res);
        setTgSession(saved);
        setIsTelegramMiniApp(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setTelegramAuthError(
          err instanceof Error ? err.message : "Telegram Mini App verification failed"
        );
      })
      .finally(() => {
        if (!cancelled) setTgLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const redeemBindCode = useCallback(
    async (code: string): Promise<TelegramWebAppSession> => {
      const current = tgSession || loadCachedTelegramSession();
      if (!current?.access_token) {
        throw new Error("Open inside Telegram Mini App first to link this session");
      }
      const updated = await redeemTelegramBindCode(code, current.access_token);
      const saved = saveCachedTelegramSession(updated);
      setTgSession(saved);
      return updated;
    },
    [tgSession]
  );

  return {
    tgSession,
    isTelegramMiniApp,
    tgLoading,
    telegramAuthError,
    redeemBindCode,
  };
}

function ClerkBackedAuthBridge({ children }: { children: React.ReactNode }) {
  const {
    getToken: getClerkToken,
    isSignedIn: clerkSignedIn,
    isLoaded: clerkLoaded,
    userId: clerkUserId,
  } = useClerkAuth();
  const { tgSession, isTelegramMiniApp, tgLoading, telegramAuthError, redeemBindCode } =
    useTelegramViewportAndSession();

  const getToken = useCallback(async (): Promise<string | null> => {
    if (isTelegramMiniApp) return tgSession?.access_token ?? null;
    return clerkSignedIn ? getClerkToken() : null;
  }, [isTelegramMiniApp, tgSession, clerkSignedIn, getClerkToken]);

  const value = useMemo<AppAuthValue>(() => {
    if (clerkSignedIn && !isTelegramMiniApp) {
      return {
        getToken,
        isSignedIn: true,
        isLoaded: Boolean(clerkLoaded) && !tgLoading,
        authMode: "clerk",
        userId: clerkUserId ?? null,
        telegramUserId: tgSession?.telegram_user_id ?? null,
        isTelegramMiniApp,
        isLinked: Boolean(tgSession?.linked),
        telegramAuthError,
        clerkConfigured: true,
        redeemBindCode,
      };
    }
    if (tgSession) {
      return {
        getToken,
        isSignedIn: true,
        isLoaded: !tgLoading,
        authMode: "telegram",
        userId: tgSession.user_id,
        telegramUserId: tgSession.telegram_user_id,
        isTelegramMiniApp: true,
        isLinked: tgSession.linked,
        telegramAuthError,
        clerkConfigured: true,
        redeemBindCode,
      };
    }
    return {
      getToken,
      isSignedIn: false,
      isLoaded: !tgLoading && (isTelegramMiniApp || Boolean(clerkLoaded)),
      authMode: "anonymous",
      userId: null,
      telegramUserId: null,
      isTelegramMiniApp,
      isLinked: false,
      telegramAuthError,
      clerkConfigured: true,
      redeemBindCode,
    };
  }, [
    clerkLoaded,
    clerkSignedIn,
    clerkUserId,
    getToken,
    isTelegramMiniApp,
    redeemBindCode,
    telegramAuthError,
    tgLoading,
    tgSession,
  ]);

  return <AppAuthContext.Provider value={value}>{children}</AppAuthContext.Provider>;
}

function StandaloneAuthBridge({ children }: { children: React.ReactNode }) {
  const { tgSession, isTelegramMiniApp, tgLoading, telegramAuthError, redeemBindCode } =
    useTelegramViewportAndSession();

  const getToken = useCallback(async (): Promise<string | null> => {
    return tgSession?.access_token ?? null;
  }, [tgSession]);

  const value = useMemo<AppAuthValue>(() => {
    if (tgSession) {
      return {
        getToken,
        isSignedIn: true,
        isLoaded: !tgLoading,
        authMode: "telegram",
        userId: tgSession.user_id,
        telegramUserId: tgSession.telegram_user_id,
        isTelegramMiniApp: true,
        isLinked: tgSession.linked,
        telegramAuthError,
        clerkConfigured: false,
        redeemBindCode,
      };
    }
    return {
      getToken,
      isSignedIn: false,
      isLoaded: !tgLoading,
      authMode: "anonymous",
      userId: null,
      telegramUserId: null,
      isTelegramMiniApp,
      isLinked: false,
      telegramAuthError,
      clerkConfigured: false,
      redeemBindCode,
    };
  }, [getToken, isTelegramMiniApp, redeemBindCode, telegramAuthError, tgLoading, tgSession]);

  return <AppAuthContext.Provider value={value}>{children}</AppAuthContext.Provider>;
}

export function AppAuthShell({ children }: { children: React.ReactNode }) {
  if (IS_CLERK_CONFIGURED) {
    return (
      <ClerkProvider>
        <ClerkBackedAuthBridge>{children}</ClerkBackedAuthBridge>
      </ClerkProvider>
    );
  }
  return <StandaloneAuthBridge>{children}</StandaloneAuthBridge>;
}

export function useAppAuth(): AppAuthValue {
  return useContext(AppAuthContext);
}

export function AccountStatusControl({
  compact = false,
  accountLabel = "My Account",
  signInLabel = "Sign In",
}: {
  compact?: boolean;
  accountLabel?: string;
  signInLabel?: string;
}) {
  const auth = useAppAuth();

  if (auth.authMode === "telegram") {
    if (compact) {
      return (
        <Link
          href="/settings"
          title={`Telegram #${auth.telegramUserId || ""}`}
          className="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold text-white"
          style={{ background: "linear-gradient(135deg, #2AABEE, #229ED9)" }}
        >
          TG
        </Link>
      );
    }
    return (
      <Link
        href="/settings"
        className="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-white/5 transition-colors"
        style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--card-border)" }}
      >
        <span
          className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold text-white flex-shrink-0"
          style={{ background: "linear-gradient(135deg, #2AABEE, #229ED9)" }}
        >
          TG
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-medium truncate" style={{ color: "var(--foreground)" }}>
            {auth.isLinked ? "Telegram · 已绑定" : "Telegram Mini App"}
          </div>
          <div className="text-[10px] truncate" style={{ color: "var(--muted)" }}>
            {auth.isLinked ? "Web 同步已开启" : "点击绑定 Web 账号"}
          </div>
        </div>
      </Link>
    );
  }

  if (auth.clerkConfigured) {
    if (compact) {
      return auth.isSignedIn ? (
        <UserButton appearance={{ elements: { avatarBox: "w-6 h-6" } }} />
      ) : (
        <Link
          href="/sign-in"
          title={signInLabel}
          className="w-7 h-7 rounded-full flex items-center justify-center text-xs"
          style={{ background: "rgba(232,96,122,0.16)", color: "var(--accent)" }}
        >
          →
        </Link>
      );
    }
    return (
      <div
        className="flex items-center gap-2.5 px-3 py-2 rounded-lg"
        style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--card-border)" }}
      >
        {auth.isSignedIn ? (
          <>
            <UserButton
              appearance={{
                elements: {
                  avatarBox: "w-6 h-6",
                  userButtonPopoverCard: "bg-[var(--card-bg)] border border-[var(--card-border)]",
                },
              }}
            />
            <span className="text-[11px] truncate" style={{ color: "var(--muted)" }}>
              {accountLabel}
            </span>
          </>
        ) : (
          <Link
            href="/sign-in"
            className="text-[11px] font-medium transition-colors hover:text-white"
            style={{ color: "var(--accent)" }}
          >
            {signInLabel}
          </Link>
        )}
      </div>
    );
  }

  if (compact) {
    return (
      <Link
        href="/settings"
        title="Private Mode"
        className="w-7 h-7 rounded-full flex items-center justify-center text-[10px]"
        style={{ background: "rgba(255,255,255,0.06)", color: "var(--muted)" }}
      >
        ⚙️
      </Link>
    );
  }

  return (
    <Link
      href="/settings"
      className="flex items-center gap-2 px-3 py-2 rounded-lg text-[11px]"
      style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--card-border)", color: "var(--muted)" }}
    >
      <span>🔒</span>
      <span className="truncate">Local / Private Mode</span>
    </Link>
  );
}

export function InlineSignInCTA({ label = "登录" }: { label?: string }) {
  const auth = useAppAuth();
  if (auth.clerkConfigured && !auth.isTelegramMiniApp) {
    return (
      <SignInButton mode="modal">
        <button
          type="button"
          className="ml-auto shrink-0 px-3 py-1 rounded-lg text-xs font-semibold transition-colors"
          style={{ background: "var(--accent)", color: "#fff" }}
        >
          {label}
        </button>
      </SignInButton>
    );
  }
  return (
    <Link
      href="/settings"
      className="ml-auto shrink-0 px-3 py-1 rounded-lg text-xs font-semibold transition-colors"
      style={{ background: "var(--accent)", color: "#fff" }}
    >
      设置
    </Link>
  );
}
