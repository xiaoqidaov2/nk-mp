/**
 * The in-flight Nike bind attempt id is the only thing persisted on the client.
 * It is namespaced per user so a browser session that switches accounts can
 * never resume the previous user's code entry, and the code/phone themselves
 * stay in component memory only.
 */
const ATTEMPT_KEY_PREFIX = "nike_mp_nike_auth_attempt:";

export function readNikeAttemptId(userId: string): string | null {
  try {
    return sessionStorage.getItem(ATTEMPT_KEY_PREFIX + userId);
  } catch {
    return null;
  }
}

export function writeNikeAttemptId(userId: string, attemptId: string): void {
  try {
    sessionStorage.setItem(ATTEMPT_KEY_PREFIX + userId, attemptId);
  } catch {
    // Storage may be unavailable (private mode); the flow still works in memory.
  }
}

export function clearNikeAttemptId(userId: string): void {
  try {
    sessionStorage.removeItem(ATTEMPT_KEY_PREFIX + userId);
  } catch {
    // Ignore unavailable storage.
  }
}

/** Called on logout: drops every attempt id, never the bound credential. */
export function clearAllNikeAttemptIds(): void {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(ATTEMPT_KEY_PREFIX)) sessionStorage.removeItem(key);
    }
  } catch {
    // Ignore unavailable storage.
  }
}
