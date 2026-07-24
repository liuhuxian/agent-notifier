function buildPermissionMessage(props) {
  const meta = props?.metadata || {}
  const filepath = meta?.filepath || ""
  const permType = props?.permission || "unknown"
  const approvalId = (props?.id || "").slice(-8)

  const lines = ["opencode 权限请求"]
  lines.push(`类型: ${permType}`)
  if (filepath) lines.push(`路径: ${filepath}`)
  if (approvalId) lines.push(`ID: ${approvalId}`)
  return lines.join("\n")
}

function buildCompletionMessage(props, directory) {
  const sessionId = (props?.sessionID || "").slice(-8)
  const lines = ["opencode: turn 完成"]
  if (directory) lines.push(`目录: ${directory}`)
  if (sessionId) lines.push(`会话: ${sessionId}`)
  return lines.join("\n")
}

function buildErrorMessage(props) {
  const error = props?.error || props?.message || ""
  const lines = ["opencode: 错误"]
  if (error) lines.push(`详情: ${error}`)
  return lines.join("\n")
}

export const FeishuNotify = async ({ $, directory }) => {
  return {
    event: async ({ event }) => {
      const type = event?.type
      if (!type) return

      let message = null
      if (type === "session.idle") {
        message = buildCompletionMessage(event?.properties, directory)
      } else if (type === "session.error") {
        message = buildErrorMessage(event?.properties)
      } else if (type === "permission.asked") {
        message = buildPermissionMessage(event?.properties)
      }

      if (!message) return

      try {
        await $`cc-connect send -m ${message}`.quiet()
      } catch {
        // cc-connect not available, silently skip
      }
    },
  }
}