import ChatInterface from "@/components/ChatInterface";

export default async function ChatPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ conversation?: string | string[] }>;
}) {
  const [{ id }, { conversation }] = await Promise.all([params, searchParams]);
  const characterId = Number(id);
  const selectedId = typeof conversation === "string" ? Number(conversation) : undefined;

  if (!Number.isSafeInteger(characterId) || characterId < 1 ||
      (selectedId !== undefined && (!Number.isSafeInteger(selectedId) || selectedId < 1))) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-red-500">Invalid character ID</p>
      </div>
    );
  }

  return <ChatInterface key={`${characterId}:${selectedId ?? "latest"}`} characterId={characterId} selectedId={selectedId} />;
}

