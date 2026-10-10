/** Public send-error text and full correlation identifiers for the classic toast. */
export function sendFailureToastDetail(
  error: unknown,
  conversationId: string,
  responseStatus: number,
): string {
  const failure = error && typeof error === 'object'
    ? error as Record<string, unknown> : {};
  const body = failure.body && typeof failure.body === 'object'
    ? failure.body as Record<string, unknown> : {};
  const bodyError = body.error && typeof body.error === 'object'
    ? body.error as Record<string, unknown> : {};
  const text = (value: unknown): string => typeof value === 'string' ? value.trim() : '';
  // The typed transport composes public 500 detail + Request ID in message.
  // Choosing body.error.message first loses that occurrence handle.
  const detail = (responseStatus === 500
    ? text(failure.message) || text(bodyError.message) || text(body.message)
    : text(bodyError.message) || text(body.message) || text(failure.message)
  ) || 'Send failed';
  const lines = [detail];
  const requestId = text(failure.requestId);
  if (requestId && !detail.includes(`Request ID: ${requestId}`)) {
    lines.push(`Request ID: ${requestId}`);
  }
  if (conversationId && !detail.includes(`Conversation ID: ${conversationId}`)) {
    lines.push(`Conversation ID: ${conversationId}`);
  }
  if (!responseStatus) lines.push('Draft kept; retry is safe');
  return lines.join('\n');
}
