const IS_ACP_MODE = process.argv?.includes('acp') ?? false

const MAX_CHUNK = 30000
const pendingQuestions = new Map()

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

async function sendMessage(fn$, message, sessionID = "") {
  try {
    const home = process.env.HOME || "~"
    if (sessionID) {
      await fn$`${home}/.local/bin/agent-notifier opencode-notify --session ${sessionID} -m ${message}`.quiet()
    } else {
      await fn$`${home}/.local/bin/agent-notifier notify -m ${message}`.quiet()
    }
  } catch {
    // agent-notifier not available, silently skip
  }
}

async function sendToolEvent(payload) {
  try {
    const home = process.env.HOME || "~"
    const { execFile } = require("child_process")
    await new Promise((resolve) => {
      const child = execFile(
        `${home}/.local/bin/agent-notifier`,
        ["opencode-tool-event", "--stdin"],
        { timeout: 2000 },
        () => resolve(),
      )
      child.stdin.end(JSON.stringify(payload))
    })
  } catch {
    // ACP may not be running; tool progress is best effort.
  }
}

async function sendQuestionAndWait(payload) {
  const home = process.env.HOME || "~"
  const { execFile } = require("child_process")
  const binary = `${home}/.local/bin/agent-notifier`
  await new Promise((resolve) => {
    const child = execFile(binary, ["opencode-question", "--stdin"], { timeout: 10000 }, () => resolve())
    child.stdin.end(JSON.stringify(payload))
  })

  const fs = require("fs")
  const answerFile = `/tmp/oc-question-answer-${payload.id}.json`
  const deadline = Date.now() + 30 * 60 * 1000
  while (Date.now() < deadline) {
    if (pendingQuestions.get(payload.id) === false) return null
    if (fs.existsSync(answerFile)) {
      try {
        const answer = JSON.parse(fs.readFileSync(answerFile, "utf8"))
        if (answer.submitted === true && Array.isArray(answer.answers) && answer.answers.length === payload.questions.length &&
            answer.answers.every((item) => Array.isArray(item) && item.length > 0)) {
          fs.unlinkSync(answerFile)
          return answer.answers
        }
      } catch {}
    }
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
  return null
}

async function replyQuestion(client, requestID, answers, directory) {
  try {
    const baseURL = process.env.AGENT_NOTIFIER_OPENCODE_URL || "http://127.0.0.1:4098"
    const url = new URL(`${baseURL}/question/${encodeURIComponent(requestID)}/reply`)
    if (directory) url.searchParams.set("directory", directory)
    const response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ answers }),
    })
    if (!response.ok) return false
    return true
  } catch {
    return false
  }
}

async function updateQuestionCard(requestID, answers) {
  const home = process.env.HOME || "~"
  const { execFile } = require("child_process")
  await new Promise((resolve) => {
    const child = execFile(
      `${home}/.local/bin/agent-notifier`,
      ["opencode-question-result", "--request", requestID, "--stdin"],
      { timeout: 10000 },
      () => resolve(),
    )
    child.stdin.end(JSON.stringify({ answers }))
  })
}

async function updateTerminalQuestionCard(requestID, answers, status = "terminal") {
  const home = process.env.HOME || "~"
  const { execFile } = require("child_process")
  await new Promise((resolve) => {
    const child = execFile(
      `${home}/.local/bin/agent-notifier`,
      ["opencode-question-result", "--request", requestID, "--status", status, "--stdin"],
      { timeout: 10000 },
      () => resolve(),
    )
    child.stdin.end(JSON.stringify({ answers }))
  })
}

function compactValue(value, maxLen = 12000) {
  try {
    const text = typeof value === "string" ? value : JSON.stringify(value ?? {})
    return text.length > maxLen ? `${text.slice(0, maxLen)}…` : value
  } catch {
    return {}
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
    await sendMessage(fn$, header, sessionID)
    return
  }

  const body = `${header}\n**结果**:\n\n${finalText}`
  const chunks = chunkText(body, MAX_CHUNK)
  if (chunks.length === 1) {
    await sendMessage(fn$, chunks[0], sessionID)
    return
  }
  for (let i = 0; i < chunks.length; i++) {
    const tag = `[${i + 1}/${chunks.length}]`
    const payload = i === 0 ? `${tag}\n${chunks[i]}` : `${tag}\n${chunks[i]}`
    await sendMessage(fn$, payload, sessionID)
  }
}

export const FeishuNotify = async ({ $, client, directory }) => {
  return {
    "tool.execute.before": async ({ tool, sessionID, callID }, output) => {
      await sendToolEvent({
        phase: "start",
        sessionID,
        callID,
        tool,
        title: `OpenCode: ${tool}`,
        args: compactValue(output?.args),
      })
    },

    "tool.execute.after": async ({ tool, sessionID, callID, args }, output) => {
      await sendToolEvent({
        phase: "complete",
        sessionID,
        callID,
        tool,
        args: compactValue(args),
        output: compactValue(output?.output),
        error: output?.metadata?.error || undefined,
      })
    },

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
          ...(Array.isArray(props.always) && props.always.length > 0
            ? ["--allow-always"] : []),
        ], { stdio: "ignore" })
        // cc-connect executes the generated command, which now resolves the
        // request through the current session-scoped OpenCode API. The file is
        // retained only for compatibility with older command configurations.
        return
      }

      if (type === "question.asked") {
        const props = event?.properties || {}
        const payload = {
          id: props.id || "",
          sessionID: props.sessionID || "",
          questions: Array.isArray(props.questions) ? props.questions : [],
        }
        pendingQuestions.set(payload.id, true)
        void (async () => {
          try {
            const answers = await sendQuestionAndWait(payload)
            if (answers) {
              try { require("fs").writeFileSync(`/tmp/oc-question-feishu-${payload.id}.marker`, "1") } catch {}
              const replied = await replyQuestion(client, payload.id, answers, directory)
              if (replied) {
                await updateQuestionCard(payload.id, answers)
              }
            }
          } finally {
            pendingQuestions.delete(payload.id)
          }
        })()
        return
      }

      if (type === "question.replied" || type === "question.rejected") {
        const props = event?.properties || {}
        const requestID = props.requestID || ""
        if (!requestID) return
        pendingQuestions.set(requestID, false)
        const fs = require("fs")
        const marker = `/tmp/oc-question-feishu-${requestID}.marker`
        if (fs.existsSync(marker)) {
          try { fs.unlinkSync(marker) } catch {}
          return
        }
        const answers = type === "question.replied" && Array.isArray(props.answers)
          ? props.answers : []
        await updateTerminalQuestionCard(
          requestID,
          answers,
          type === "question.rejected" ? "rejected" : "terminal",
        )
        return
      }

      if (type === "permission.replied") {
        const props = event?.properties || {}
        const pid = props.requestID || ""
        const sid = props.sessionID || props.sessionId || ""
        const home = process.env.HOME || "~"
        if (pid) {
          const fs = require("fs")
          const feishuMarker = `/tmp/oc-perm-feishu-${pid}.marker`
          const sessionMarker = sid
            ? `/tmp/oc-perm-feishu-session-${sid}.marker`
            : ""
          if (
            fs.existsSync(feishuMarker) ||
            (sessionMarker && fs.existsSync(sessionMarker))
          ) {
            for (const marker of [feishuMarker, sessionMarker]) {
              if (marker) try { fs.unlinkSync(marker) } catch {}
            }
            return
          }
          try {
            await $`${home}/.local/bin/agent-notifier opencode-reply-result --perm "${pid}" neutral`.quiet()
          } catch {}
        }
        return
      }

      if (type === "session.error") {
        const message = buildErrorMessage(event?.properties)
        await sendMessage($, message, event?.properties?.sessionID || "")
        return
      }
    },
  }
}
