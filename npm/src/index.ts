export interface AutoDevConfig {
  platform: "claude-code" | "cursor" | "inline";
  workspace: string;
  pythonPath?: string;
}

export async function getAutoDevPath(): Promise<string> {
  return join(homedir(), ".config", "autodev", "venv", "bin", "autodev");
}

import { homedir } from "os";
import { join } from "path";
