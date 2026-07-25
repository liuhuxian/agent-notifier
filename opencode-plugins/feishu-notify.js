const IS_ACP_MODE = process.argv?.includes('acp') ?? false

const MAX_CHUNK = 30000

function buildPermissionMessage(props) {
  const meta = props?.metadata || {}
  const filepath = meta?.filepath || ""
  const permType = props?.permission || "unknown"
  const approvalId = (props?.id || "").slice(-8)

  const lines = ["**opencode 权限请求**\n"]
  lines.push(`- 类型: \`${permType}\``)
  if (filepath) lines.push(`- 路径: \`${filepath}\``)
  if (approvalId) lines.push(`- ID: \`${approvalId}\``)
  return lines.join("\n")
}

function buildCompletionHeader(sessionID, directory) {
  const sessionId = (sessionID || "").slice(-8)
  const lines = ["**opencode: 回合完成**\n"]
  if (directory) lines.push(`- 目录: \`${directory}\``)
  if (sessionId) lines.push(`- 会话: \`${sessionId}\``)
  return lines.join("\n")
}

function buildErrorMessage(props) {
  const error = props?.error || props?.message || ""
  const lines = ["**opencode: 错误**\n"]
  if (error) lines.push(`- 详情: \`${error}\``)
  return lines.join("\n")
}

function extractFinalAssistantText(messages) {
  if (!Array.isArray(messages)) return ""
  for (let i = messages.length - 1; i >= 0; i--) {
    const entry = messages[i]
    const info = entry?.info
    if (!info || info.role !== "assistant") continue
    const parts = Array.isArray(entry.parts) ? entry.parts : []
    const chunks = []
    for (const part of parts) {
      if (!part || part.type !== "text") continue
      if (part.synthetic === true) continue
      if (typeof part.text === "string" && part.text.length > 0) {
        chunks.push(part.text)
      }
    }
    return chunks.join("").trim()
  }
  return ""
}

function chunkText(text, maxLen) {
  if (!text) return []
  if (text.length <= maxLen) return [text]
  const chunks = []
  let remaining = text
  while (remaining.length > maxLen) {
    let cut = remaining.lastIndexOf("\n", maxLen)
    if (cut <= 0) cut = maxLen
    chunks.push(remaining.slice(0, cut))
    remaining = remaining.slice(cut).replace(/^\n/, "")
  }
  if (remaining.length > 0) chunks.push(remaining)
  return chunks
}

async function sendMessage(fn$, message) {
  try {
    const home = process.env.HOME || "~"
    await fn$`${home}/.local/bin/agent-notifier notify -m ${message}`.quiet()
  } catch {
    // agent-notifier not available, silently skip
  }
}

async function sendCompletionNotification(fn$, client, event, directory) {
  const props = event?.properties || {}
  const sessionID = props.sessionID
  const header = buildCompletionHeader(sessionID, directory)

  let finalText = ""
  if (sessionID && client?.session?.messages) {
    try {
      const query = directory ? { directory } : undefined
      const res = await client.session.messages({
        path: { id: sessionID },
        query,
      })
      const data = res?.data ?? res
      finalText = extractFinalAssistantText(data)
    } catch {
      finalText = ""
    }
  }

  if (!finalText) {
    await sendMessage(fn$, header)
    return
  }

  const body = `${header}\n**结果:**\n\n${finalText}`
  const chunks = chunkText(body, MAX_CHUNK)
  if (chunks.length === 1) {
    await sendMessage(fn$, chunks[0])
    return
  }
  for (let i = 0; i < chunks.length; i++) {
    const tag = `[${i + 1}/${chunks.length}]`
    const payload = i === 0 ? `${tag}\n${chunks[i]}` : `${tag}\n${chunks[i]}`
    await sendMessage(fn$, payload)
  }
}

export const FeishuNotify = async ({ $, client, directory }) => {
  return {
    event: async ({ event }) => {
      const type = event?.type
      if (!type) return

      if (IS_ACP_MODE && (type === "session.idle" || type === "permission.asked")) {
        return
      }

      if (type === "session.idle") {
        await sendCompletionNotification($, client, event, directory)
        return
      }

      let message = null
      if (type === "session.error") {
        message = buildErrorMessage(event?.properties)
      } else if (type === "permission.asked") {
        message = buildPermissionMessage(event?.properties)
      }

      if (!message) return
      await sendMessage($, message)
    },
  }
}
