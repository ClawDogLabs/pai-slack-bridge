// Session manager: maps Slack threads to Claude Code sessions
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync } from 'fs';
import { dirname, join } from 'path';
import { homedir } from 'os';
import { randomUUID } from 'crypto';

export interface SessionMapping {
  sessionId: string;
  channelId: string;
  threadTs: string;
  userId: string;
  createdAt: string;
  lastActivity: string;
  verbose?: boolean;
}

interface SessionStore {
  sessions: Record<string, SessionMapping>; // key: "channelId:threadTs"
}

// Use BRIDGE_DATA_DIR or default to ./data relative to project
const DATA_DIR = process.env.BRIDGE_DATA_DIR || join(dirname(dirname(Bun.main)), 'data');
const SESSIONS_PATH = join(DATA_DIR, 'sessions.json');

// Ensure data directory exists
if (!existsSync(DATA_DIR)) {
  mkdirSync(DATA_DIR, { recursive: true });
}

/**
 * Load sessions from disk
 */
function loadSessions(): SessionStore {
  try {
    if (existsSync(SESSIONS_PATH)) {
      const data = readFileSync(SESSIONS_PATH, 'utf-8');
      return JSON.parse(data);
    }
  } catch (error) {
    console.error('Error loading sessions:', error);
  }
  return { sessions: {} };
}

/**
 * Save sessions to disk
 */
function saveSessions(store: SessionStore): void {
  try {
    writeFileSync(SESSIONS_PATH, JSON.stringify(store, null, 2));
  } catch (error) {
    console.error('Error saving sessions:', error);
  }
}

/**
 * Create session key from channel and thread
 */
function sessionKey(channelId: string, threadTs: string): string {
  return `${channelId}:${threadTs}`;
}

/**
 * Get or create a session for a Slack thread
 */
export function getOrCreateSession(
  channelId: string,
  threadTs: string,
  userId: string
): { session: SessionMapping; isNew: boolean } {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);

  if (store.sessions[key]) {
    // Update last activity
    store.sessions[key].lastActivity = new Date().toISOString();
    saveSessions(store);
    return { session: store.sessions[key], isNew: false };
  }

  // Create new session
  const session: SessionMapping = {
    sessionId: randomUUID(),
    channelId,
    threadTs,
    userId,
    createdAt: new Date().toISOString(),
    lastActivity: new Date().toISOString(),
  };

  store.sessions[key] = session;
  saveSessions(store);

  return { session, isNew: true };
}

/**
 * Get session by thread
 */
export function getSession(channelId: string, threadTs: string): SessionMapping | null {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);
  return store.sessions[key] || null;
}

/**
 * Update session's last activity
 */
export function updateSessionActivity(channelId: string, threadTs: string): void {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);

  if (store.sessions[key]) {
    store.sessions[key].lastActivity = new Date().toISOString();
    saveSessions(store);
  }
}

/**
 * Update the sessionId for an existing thread (used when Claude returns its real session ID)
 */
export function updateSessionId(channelId: string, threadTs: string, newSessionId: string): void {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);

  if (store.sessions[key]) {
    store.sessions[key].sessionId = newSessionId;
    store.sessions[key].lastActivity = new Date().toISOString();
    saveSessions(store);
    console.log(`[Session] Updated session ID for ${key}: ${newSessionId}`);
  } else {
    console.warn(`[Session] Cannot update sessionId: no session found for ${key}`);
  }
}

/**
 * Look up a session by its sessionId (for bridge API)
 */
export function getSessionBySessionId(sessionId: string): SessionMapping | null {
  const store = loadSessions();
  for (const session of Object.values(store.sessions)) {
    if (session.sessionId === sessionId) {
      return session;
    }
  }
  return null;
}

/**
 * Update session ID with Claude Code's actual session ID
 * Called when Claude Code returns a different session_id than what we generated
 */
export function updateSessionId(channelId: string, threadTs: string, realSessionId: string): boolean {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);

  if (store.sessions[key]) {
    const oldId = store.sessions[key].sessionId;
    if (oldId !== realSessionId) {
      console.log(`[Session] Reconciling session ID: ${oldId} -> ${realSessionId}`);
      store.sessions[key].sessionId = realSessionId;
      saveSessions(store);
      return true;
    }
  }
  return false;
}

/**
 * Set verbose flag for a session
 */
export function setSessionVerbose(channelId: string, threadTs: string, verbose: boolean): boolean {
  const store = loadSessions();
  const key = sessionKey(channelId, threadTs);

  if (store.sessions[key]) {
    store.sessions[key].verbose = verbose;
    saveSessions(store);
    return true;
  }
  return false;
}

/**
 * Clean up old sessions (older than 24 hours of inactivity)
 */
export function cleanupOldSessions(maxAgeHours = 24): number {
  const store = loadSessions();
  const cutoff = Date.now() - (maxAgeHours * 60 * 60 * 1000);
  let removed = 0;

  for (const key of Object.keys(store.sessions)) {
    const session = store.sessions[key];
    const lastActivity = new Date(session.lastActivity).getTime();

    if (lastActivity < cutoff) {
      delete store.sessions[key];
      removed++;
    }
  }

  if (removed > 0) {
    saveSessions(store);
  }

  return removed;
}

/**
 * Get a per-thread CWD for Windows session isolation.
 * On Windows, --resume doesn't work (Claude CLI bug #27463), so we use
 * --continue which resumes the most recent session in the CWD. Each thread
 * gets its own CWD to prevent cross-talk between concurrent threads.
 * Returns empty string on non-Windows (use default CWD).
 */
export function getThreadCwd(channelId: string, threadTs: string): string {
  if (process.platform !== 'win32') return '';
  const baseCwd = process.env.BRIDGE_DEFAULT_CWD || join(homedir(), '.claude');
  const threadDir = join(baseCwd, '.bridge-threads', `${channelId}-${threadTs}`);
  if (!existsSync(threadDir)) {
    mkdirSync(threadDir, { recursive: true });
  }
  return threadDir;
}

/**
 * Encode a CWD path to Claude Code's project directory name format.
 * Claude replaces ':', '/', '\', and '.' with '-'.
 * e.g. C:\Users\foo\.bar -> C--Users-foo--bar
 */
function cwdToProjectDirName(cwd: string): string {
  return cwd.replace(/[:/\\.]/g, '-');
}

const UUID_JSONL = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$/i;

/**
 * Ensure sessions-index.json exists and is up-to-date in Claude's project
 * directory for the given CWD. Called before --continue so Claude can find
 * the previous session. Sessions-index.json is not created by Claude when a
 * new per-thread CWD is used (bug #23614), causing --continue to start fresh.
 */
export function ensureSessionsIndex(cwd: string): void {
  try {
    const paiDir = process.env.PAI_DIR || join(homedir(), '.claude');
    const projectDirName = cwdToProjectDirName(cwd);
    const projectDir = join(paiDir, 'projects', projectDirName);

    if (!existsSync(projectDir)) return; // No project dir yet — first session hasn't run

    const indexPath = join(projectDir, 'sessions-index.json');

    // Read existing index or start fresh
    let indexData: { entries: Record<string, unknown>[] } = { entries: [] };
    if (existsSync(indexPath)) {
      try {
        indexData = JSON.parse(readFileSync(indexPath, 'utf-8'));
      } catch {
        indexData = { entries: [] };
      }
    }

    const indexedIds = new Set(indexData.entries.map((e: Record<string, unknown>) => e.sessionId as string));

    // Find .jsonl files not in the index
    const jsonlFiles = readdirSync(projectDir)
      .filter(f => UUID_JSONL.test(f))
      .filter(f => !indexedIds.has(f.replace('.jsonl', '')));

    if (jsonlFiles.length === 0) return; // Nothing to add

    for (const file of jsonlFiles) {
      const filePath = join(projectDir, file);
      const sessionId = file.replace('.jsonl', '');
      const stat = statSync(filePath);

      // Parse minimal metadata from JSONL (first user prompt + timestamps)
      let firstPrompt = 'No prompt';
      let created: string | null = null;
      let modified: string | null = null;
      try {
        const lines = readFileSync(filePath, 'utf-8').split('\n').filter(Boolean);
        for (const line of lines) {
          try {
            const data = JSON.parse(line);
            if (!created && data.timestamp) created = data.timestamp;
            if (data.timestamp) modified = data.timestamp;
            if (data.type === 'user' && firstPrompt === 'No prompt') {
              const content = data.message?.content;
              if (typeof content === 'string' && content.trim()) {
                firstPrompt = content.slice(0, 80) + (content.length > 80 ? '...' : '');
              }
            }
          } catch { /* skip bad lines */ }
        }
      } catch { /* skip unreadable files */ }

      const now = new Date(stat.mtimeMs).toISOString();
      indexData.entries.push({
        sessionId,
        fullPath: filePath,
        fileMtime: stat.mtimeMs,
        firstPrompt,
        customTitle: null,
        summary: firstPrompt,
        messageCount: 0,
        created: created || new Date(stat.ctimeMs).toISOString(),
        modified: modified || now,
        gitBranch: 'main',
        projectPath: join(paiDir, 'projects'),
        isSidechain: false,
      });
    }

    // Sort newest first
    indexData.entries.sort((a, b) =>
      ((b.modified as string) || '').localeCompare((a.modified as string) || '')
    );

    writeFileSync(indexPath, JSON.stringify(indexData, null, 2), 'utf-8');
    console.log(`[Session] Created/updated sessions-index.json in ${projectDirName} (${jsonlFiles.length} new session(s))`);
  } catch (error) {
    // Best-effort — don't crash the message handler
    console.warn('[Session] ensureSessionsIndex failed:', error);
  }
}
