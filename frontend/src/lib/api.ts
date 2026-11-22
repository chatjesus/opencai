/**
 * API client for the opencai backend.
 */

// Use env var if set; fallback to "" (relative URL) so Next.js / nginx can proxy same-origin
const API_BASE = (process.env.NEXT_PUBLIC_API_URL !== undefined && process.env.NEXT_PUBLIC_API_URL !== "undefined")
  ? process.env.NEXT_PUBLIC_API_URL
  : "";

/** Build auth headers. Pass the Clerk or Telegram Mini App session token when available. */
export function authHeaders(token?: string | null): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string" && body.detail.trim()) {
      return body.detail;
    }
  } catch {
    // ignore non-JSON body
  }
  return fallback;
}

// ── Types ──

export interface CharacterCard {
  id: number;
  name: string;
  description: string;
  avatar_url: string;
  tags: string;
  category: string;
  message_count: number;
  star_count: number;
}

export interface Character {
  id: number;
  name: string;
  description: string;
  system_prompt: string;
  greeting: string;
  backstory?: string;
  avatar_url: string;
  tags: string;
  category: string;
  is_public: boolean;
  message_count: number;
  star_count: number;
  creator_id: number | null;
  can_edit?: boolean;
  created_at: string;
  updated_at: string;
}

export interface CharacterCreate {
  name: string;
  description: string;
  system_prompt: string;
  greeting: string;
  backstory?: string;
  avatar_url: string;
  tags: string;
  category: string;
  is_public: boolean;
}

export interface CharacterUpdate {
  name?: string;
  description?: string;
  system_prompt?: string;
  greeting?: string;
  backstory?: string;
  avatar_url?: string;
  tags?: string;
  category?: string;
  is_public?: boolean;
}

export interface Conversation {
  id: number;
  character_id: number;
  title: string;
  character_name: string;
  character_avatar: string;
  intimacy_level?: number;
  streak_days?: number;
  created_at: string;
  updated_at: string;
}

export interface ChatImage {
  url: string;
  alt: string;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  images?: ChatImage[];
  is_proactive?: boolean;
  created_at: string;
}

export interface ConversationDetail {
  id: number;
  character_id: number;
  character_name: string;
  character_avatar: string;
  title: string;
  messages: ChatMessage[];
  intimacy_level: number;
  streak_days: number;
  current_mood?: string | null;
  scenario_note?: string;
  created_at: string;
  updated_at: string;
}

export interface HealthStatus {
  status: string;
  ollama: string;
  models: string[];
  llm_provider?: string;
  llm_status?: string;
  llm_healthy?: boolean;
  llm_detail?: string | null;
  image_provider?: string;
  image_status?: string;
  image_healthy?: boolean;
  image_model?: string | null;
  image_detail?: string | null;
  telegram_webapp_enabled?: boolean;
}

export interface Memory {
  id: number;
  key: string;
  value: string;
  confidence: number;
  created_at: string;
}

export interface TelegramWebAppSession {
  access_token: string;
  token_type: string;
  user_id: string;
  telegram_user_id: string;
  linked: boolean;
  expires_in: number;
}

export interface IllustrateResponse {
  image: ChatImage;
  message_id: number;
  provider: string;
}

/** Operator-tunable config for running the adult companion product. */
export interface OpsConfig {
  nsfw_unlock_intimacy: number;
  intimacy_gain_multiplier: number;
  proactive_greeting_min_hours: number;
  daily_checkin_intimacy_bonus: number;
  nsfw_images_enabled: boolean;
  vip_only_explicit: boolean;
}

// ── API Functions ──

export async function fetchHealth(): Promise<HealthStatus> {
  const res = await fetch(`${API_BASE}/api/health`);
  if (!res.ok) throw new Error("Backend unreachable");
  return res.json();
}

export async function authenticateTelegramWebApp(initData: string): Promise<TelegramWebAppSession> {
  const res = await fetch(`${API_BASE}/api/auth/telegram/webapp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: initData }),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Telegram Mini App authentication failed"));
  }
  return res.json();
}

export async function generateTelegramBindCode(
  token?: string | null,
): Promise<{ code: string; expires_in: number }> {
  const res = await fetch(`${API_BASE}/api/auth/bind/code`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (res.status === 401) throw new Error("Please sign in on Web to generate a bind code");
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to generate bind code"));
  return res.json();
}

export async function redeemTelegramBindCode(
  code: string,
  token: string,
): Promise<TelegramWebAppSession> {
  const res = await fetch(`${API_BASE}/api/auth/telegram/bind`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ code }),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to redeem bind code"));
  }
  return res.json();
}

export async function fetchCharacters(
  category?: string,
  search?: string,
  locale?: string,
  token?: string | null,
): Promise<CharacterCard[]> {
  const params = new URLSearchParams();
  if (category && category !== "Featured") params.set("category", category);
  if (search) params.set("search", search);
  if (locale && locale !== "zh") params.set("locale", locale);
  params.set("limit", "300");
  const res = await fetch(`${API_BASE}/api/characters?${params}`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to fetch characters"));
  return res.json();
}

export async function fetchCharacter(
  id: number,
  locale?: string,
  token?: string | null,
): Promise<Character> {
  const params = new URLSearchParams();
  if (locale && locale !== "zh") params.set("locale", locale);
  const res = await fetch(`${API_BASE}/api/characters/${id}?${params}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error("Character not found");
  return res.json();
}

export async function createCharacter(
  data: CharacterCreate,
  token?: string | null,
): Promise<Character> {
  const res = await fetch(`${API_BASE}/api/characters`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(data),
  });
  if (res.status === 401) throw new Error("Please sign in to create a character");
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to create character"));
  return res.json();
}

export async function updateCharacter(
  id: number,
  updates: CharacterUpdate,
  token?: string | null,
): Promise<Character> {
  const res = await fetch(`${API_BASE}/api/characters/${id}`, {
    method: "PUT",
    headers: authHeaders(token),
    body: JSON.stringify(updates),
  });
  if (res.status === 401) throw new Error("Please sign in to edit this character");
  if (res.status === 403) throw new Error("Only the character creator can edit this card");
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to update character"));
  return res.json();
}

export async function fetchCategories(token?: string | null): Promise<string[]> {
  const res = await fetch(`${API_BASE}/api/characters/categories`, { headers: authHeaders(token) });
  if (!res.ok) return ["Featured"];
  return res.json();
}

export async function createConversation(
  characterId: number,
  token?: string | null,
): Promise<Conversation> {
  const res = await fetch(`${API_BASE}/api/chat/conversations`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ character_id: characterId }),
  });
  if (!res.ok) throw new Error("Failed to create conversation");
  return res.json();
}

export async function fetchConversation(
  id: number,
  token?: string | null,
): Promise<ConversationDetail> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${id}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Conversation not found"));
  return res.json();
}

export async function updateConversationContext(
  conversationId: number,
  scenarioNote: string,
  token?: string | null,
): Promise<{
  id: number;
  scenario_note: string;
  current_mood: string | null;
  intimacy_level: number;
  streak_days: number;
}> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}/context`, {
    method: "PUT",
    headers: authHeaders(token),
    body: JSON.stringify({ scenario_note: scenarioNote }),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to save scenario context"));
  }
  return res.json();
}

export async function illustrateConversation(
  conversationId: number,
  token?: string | null,
  promptHint?: string,
): Promise<IllustrateResponse> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}/illustrate`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(promptHint ? { prompt_hint: promptHint } : {}),
  });
  if (res.status === 401) {
    throw new Error("请先登录（或在已验证的 Telegram Mini App 中打开）后再生成插画 / Sign in required to generate illustrations");
  }
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to generate illustration"));
  }
  return res.json();
}

/**
 * Tell the backend the user just opened this chat. If they've been away long
 * enough, the character proactively reaches out and the greeting is returned
 * (and persisted server-side). Returns { greeting: null } otherwise.
 */
export async function checkinConversation(
  id: number,
  token?: string | null,
): Promise<{ greeting: string | null; message_id?: number; checkin_reward?: number; intimacy_level?: number }> {
  try {
    const res = await fetch(`${API_BASE}/api/chat/conversations/${id}/checkin`, {
      method: "POST",
      headers: authHeaders(token),
    });
    if (!res.ok) return { greeting: null };
    return res.json();
  } catch {
    return { greeting: null };
  }
}

/**
 * Draw the once-per-day "surprise" (gacha) for a conversation.
 */
export async function drawSurprise(
  conversationId: number,
  token?: string | null,
): Promise<{ available: boolean; rarity?: string; intimacy_bonus?: number; intimacy_level?: number; message?: string; message_id?: number }> {
  try {
    const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}/surprise`, {
      method: "POST",
      headers: authHeaders(token),
    });
    if (!res.ok) return { available: false };
    return res.json();
  } catch {
    return { available: false };
  }
}

export async function fetchConversations(
  characterId?: number,
  token?: string | null,
): Promise<Conversation[]> {
  const params = new URLSearchParams();
  if (characterId) params.set("character_id", String(characterId));
  const res = await fetch(`${API_BASE}/api/chat/conversations?${params}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to load saves"));
  return res.json();
}

export async function deleteConversation(
  id: number,
  token?: string | null,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok && res.status !== 404) {
    throw new Error(`Delete failed: ${res.status}`);
  }
}

export interface IntimacyUpdate {
  level: number;
  gained: number;
  tier: string;
  tier_en: string;
  tier_unlocked: boolean;
  unlocked_tier_name: string | null;
  next_threshold: number;
}

export interface StreakUpdate {
  streak_days: number;
  broken: boolean;
  milestone_toast: string | null;
  intimacy_bonus: number;
}

export interface ToolExecuting {
  name: string;
  args: Record<string, string>;
}

export interface ToolResult {
  name: string;
  success: boolean;
  output: string;
}

/**
 * Send a message and stream the AI response via SSE.
 */
export async function sendMessageStream(
  conversationId: number,
  content: string,
  onChunk: (text: string) => void,
  onDone: () => void,
  onError: (error: string) => void,
  locale?: string,
  token?: string | null,
  onImage?: (image: ChatImage) => void,
  onGeneratingImage?: () => void,
  onIntimacy?: (update: IntimacyUpdate) => void,
  onStreak?: (update: StreakUpdate) => void,
  onToolExecuting?: (tool: ToolExecuting) => void,
  onToolResult?: (result: ToolResult) => void,
  onToolFollowup?: (text: string) => void,
  onStoryEvent?: (event: unknown) => void,
  onVoice?: (url: string) => void,
): Promise<void> {
  try {
    const params = new URLSearchParams();
    if (locale && locale !== "zh") params.set("locale", locale);
    const res = await fetch(
      `${API_BASE}/api/chat/conversations/${conversationId}/messages?${params}`,
      {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({ content, client_hour: new Date().getHours() }),
      }
    );

    if (!res.ok) {
      onError(await readErrorDetail(res, "Failed to send message"));
      return;
    }

    const reader = res.body?.getReader();
    if (!reader) {
      onError("No response stream");
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data: ")) continue;
        const jsonStr = trimmed.slice(6);
        try {
          const data = JSON.parse(jsonStr);
          if (data.content) onChunk(data.content);
          if (data.generating_image) onGeneratingImage?.();
          if (data.image) onImage?.(data.image as ChatImage);
          if (data.intimacy) onIntimacy?.(data.intimacy as IntimacyUpdate);
          if (data.streak) onStreak?.(data.streak as StreakUpdate);
          if (data.tool_executing) onToolExecuting?.(data.tool_executing as ToolExecuting);
          if (data.tool_result) onToolResult?.(data.tool_result as ToolResult);
          if (data.tool_followup) onToolFollowup?.(data.tool_followup as string);
          if (data.story_event) onStoryEvent?.(data.story_event);
          if (data.voice?.url) onVoice?.(data.voice.url);
          if (data.done) { onDone(); return; }
          if (data.error) { onError(data.error); return; }
        } catch {
          // skip malformed JSON
        }
      }
    }
    onDone();
  } catch (err) {
    onError(err instanceof Error ? err.message : "Unknown error");
  }
}

/** Resolve image URLs — prefix with API_BASE if relative */
export function resolveImageUrl(url: string): string {
  if (url.startsWith("http")) return url;
  return `${API_BASE}${url}`;
}

// ── Character Card Import ──

export async function importCharacterCard(
  card: object,
  token?: string | null,
): Promise<Character> {
  const res = await fetch(`${API_BASE}/api/characters/import`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(card),
  });
  if (res.status === 401) throw new Error("Please sign in to import a character");
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to import character card"));
  return res.json();
}

// ── Memory ("她记得你什么") ──

export async function fetchMemories(
  characterId: number,
  token?: string | null,
): Promise<Memory[]> {
  const res = await fetch(`${API_BASE}/api/memory?character_id=${characterId}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401) throw new Error("Please sign in");
  if (!res.ok) throw new Error("Failed to fetch memories");
  return res.json();
}

export async function deleteMemory(
  id: number,
  token?: string | null,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/memory/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (res.status === 401) throw new Error("Please sign in");
  if (!res.ok && res.status !== 404) throw new Error(`Delete failed: ${res.status}`);
}

// ── Operator admin: ops-config ──

export function adminHeaders(adminToken?: string | null, token?: string | null): HeadersInit {
  const h = authHeaders(token);
  if (adminToken) h["X-Admin-Token"] = adminToken;
  return h;
}

export async function fetchOpsConfig(adminToken?: string | null, token?: string | null): Promise<OpsConfig> {
  const res = await fetch(`${API_BASE}/api/admin/ops-config`, {
    headers: adminHeaders(adminToken, token),
  });
  if (res.status === 403) throw new Error("Admin token required or invalid");
  if (!res.ok) throw new Error("Failed to fetch ops config");
  return res.json();
}

export async function updateOpsConfig(
  updates: Partial<OpsConfig>,
  adminToken?: string | null,
  token?: string | null,
): Promise<OpsConfig> {
  const res = await fetch(`${API_BASE}/api/admin/ops-config`, {
    method: "PUT",
    headers: adminHeaders(adminToken, token),
    body: JSON.stringify(updates),
  });
  if (res.status === 403) throw new Error("Admin token required or invalid");
  if (!res.ok) throw new Error("Failed to update ops config");
  return res.json();
}
