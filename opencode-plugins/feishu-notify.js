const IS_ACP_MODE = process.argv?.includes('acp') ?? false

const MAX_CHUNK = 30000

function buildPermissionMessage(props) {
  const meta = props?.metadata || {}
  const filepath = meta?.filepath || ""
  const permType = props?.permission || "unknown"
  const approvalId = (props?.id || "").slice(-8)

  const lines = ["OpenCode 权限请求"]
  lines.push(`**类型**: ${permType}`)
  if (filepath) lines.push(`**路径**: ${filepath}`)
  if (approvalId) lines.push(`**ID**: ${approvalId}`)
  return lines.join("\n")
}

function buildCompletionHeader(sessionID, directory) {
  const sessionId = (sessionID || "").slice(-8)
  const lines = ["OpenCode 回合已完成"]
  if (sessionId) lines.push(`**会话**: ${sessionId}`)
  if (directory) lines.push(`**目录**: ${directory}`)
  return lines.join("\n")
}

function buildErrorMessage(props) {
  const error = props?.error || props?.message || ""
  const lines = ["OpenCode 错误"]
  if (error) lines.push(`**详情**: ${error}`)
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

  const body = `${header}\n**结果**:\n\n${finalText}`
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
        const sid = event?.properties?.sessionID || ""
        const acpFlag = `/tmp/oc-acp-active-${sid}`
        const fs = require("fs")
        if (fs.existsSync(acpFlag)) {
          try { fs.unlinkSync(acpFlag) } catch {}
          return
        }
        await sendCompletionNotification($, client, event, directory)
        return
      }

      if (type === "permission.asked") {
        const props = event?.properties || {}
        const sid = props.sessionID || ""
        const route = require("fs").existsSync(`/tmp/oc-acp-active-${sid}`) ? "acp" : "default"
        const patterns = (props?.patterns || []).join(", ")
        const pid = props.id || ""
        const permType = props.permission || "unknown"
        const filepath = props?.metadata?.filepath || ""
        const home = process.env.HOME || "~"
        const replyFile = `/tmp/oc-perm-reply-${pid}.json`
        const fs = require("fs")
        try { fs.unlinkSync(replyFile) } catch {}
        require("child_process").spawn(`${home}/.local/bin/agent-notifier`, [
          "opencode-permission",
          "--route", route,
          "--session", sid,
          "--perm", pid,
          "--type", permType,
          "--path", filepath,
          "--pattern", patterns,
        ], { stdio: "ignore" })
        const interval = setInterval(async () => {
          try {
            const reply = fs.readFileSync(replyFile, "utf8").trim()
            if (reply !== "once" && reply !== "reject") return
            clearInterval(interval)
            try { fs.unlinkSync(replyFile) } catch {}
            await fetch(`http://127.0.0.1:4098/permission/${pid}/reply`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ reply }),
            })
          } catch {}
        }, 1000)
        setTimeout(() => clearInterval(interval), 120000)
        return
      }

      if (type === "permission.replied") {
        const props = event?.properties || {}
        const pid = props.requestID || ""
        const home = process.env.HOME || "~"
        if (pid) {
          try {
            await $`${home}/.local/bin/agent-notifier opencode-reply-result --perm "${pid}" neutral`.quiet()
          } catch {}
        }
        return
      }

      if (type === "session.error") {
        const message = buildErrorMessage(event?.properties)
        await sendMessage($, message)
        return
      }
    },
  }
}
