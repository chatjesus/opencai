import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { clerkMiddleware } from "@clerk/nextjs/server";

const hasClerkKeys = Boolean(
  process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY?.startsWith("pk_") &&
  process.env.CLERK_SECRET_KEY?.startsWith("sk_")
);

const clerkHandler = hasClerkKeys ? clerkMiddleware() : null;

export default function middleware(req: NextRequest, event: NextFetchEvent) {
  const path = req.nextUrl.pathname;
  if (!clerkHandler || path.startsWith("/api/") || path.startsWith("/uploads/")) {
    return NextResponse.next();
  }
  return clerkHandler(req, event);
}

export const config = {
  matcher: [
    // Skip Next.js internals and static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
  ],
};
